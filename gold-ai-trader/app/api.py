"""Threaded HTTP API and static dashboard host (standard library only)."""
from __future__ import annotations

import hmac
import json
import logging
import mimetypes
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from .auth import AuthManager, COOKIE_NAME, parse_cookies
from .broker import BrokerError
from .config import APP_NAME, APP_VERSION, Config
from .ea import EAProtocolError
from .engine import EngineError, TradingEngine
from .settings import SettingsError
from .util import iso

LOGGER = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_BODY = 256 * 1024
CSV_MAX_BODY = 8 * 1024 * 1024
EA_MAX_BODY = 2 * 1024 * 1024
PUBLIC_PATHS = {"/healthz", "/api/auth", "/api/login", "/api/logout"}
CSRF_HEADER = "X-AURUM-CSRF"
CSRF_VALUE = "1"


class AurumHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: Tuple[str, int], engine: TradingEngine, config: Config) -> None:
        self.engine = engine
        self.config = config
        self.auth = AuthManager(config.dashboard_password, config.session_secret, config.cookie_secure)
        self.started_at = iso()
        super().__init__(address, AurumRequestHandler)


class AurumRequestHandler(BaseHTTPRequestHandler):
    server: AurumHTTPServer
    protocol_version = "HTTP/1.1"
    server_version = "AURUM/1.0"

    # ------------------------------------------------------------------
    # HTTP plumbing
    # ------------------------------------------------------------------
    def log_message(self, fmt: str, *args: Any) -> None:
        LOGGER.debug("%s - %s", self.address_string(), fmt % args)

    def _send(self, status: int, body: bytes, content_type: str = "application/json; charset=utf-8",
              extra_headers: Optional[Dict[str, str]] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; font-src 'self'; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'self'",
        )
        if extra_headers:
            for key, value in extra_headers.items():
                self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: Any, extra_headers: Optional[Dict[str, str]] = None) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._send(status, body, extra_headers=extra_headers)

    def _error(self, status: int, message: str, code: str = "error") -> None:
        self._json(status, {"ok": False, "error": message, "code": code})

    def _read_json(self, max_body: int = MAX_BODY) -> Dict[str, Any]:
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError:
            raise ValueError("Invalid Content-Length")
        if length < 0 or length > max_body:
            raise ValueError("Request body too large")
        raw = self.rfile.read(length) if length else b"{}"
        if not raw:
            return {}
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ValueError("Body must be valid JSON")
        if not isinstance(payload, dict):
            raise ValueError("Body must be a JSON object")
        return payload

    def _authenticated(self) -> bool:
        if not self.server.auth.enabled:
            return True
        cookies = parse_cookies(self.headers.get("Cookie"))
        return self.server.auth.valid(cookies.get(COOKIE_NAME))

    def _client_ip(self) -> str:
        return str(self.client_address[0])

    # ------------------------------------------------------------------
    # Methods
    # ------------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        if path == "/healthz":
            self._json(HTTPStatus.OK, {"status": "ok", "app": APP_NAME, "version": APP_VERSION, "time": iso()})
            return
        if path.startswith("/api/"):
            if path == "/api/auth":
                self._json(HTTPStatus.OK, {
                    "authenticated": self._authenticated(),
                    "auth_enabled": self.server.auth.enabled,
                    "app": APP_NAME,
                    "version": APP_VERSION,
                })
                return
            if not self._authenticated():
                self._error(HTTPStatus.UNAUTHORIZED, "Authentication required.", "unauthorized")
                return
            self._handle_api_get(path, query)
            return
        self._serve_static(path)

    def do_HEAD(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/healthz":
            self._json(HTTPStatus.OK, {"status": "ok"})
            return
        self._error(HTTPStatus.METHOD_NOT_ALLOWED, "Method not allowed.", "method_not_allowed")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/ea/poll":
            self._handle_ea_poll()
            return
        try:
            payload = self._read_json(CSV_MAX_BODY if path == "/api/backtest/csv" else MAX_BODY)
        except ValueError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc), "bad_request")
            return
        # Body is consumed before rejecting, so keep-alive connections are not
        # left with unread bytes that could be mistaken for the next request.
        if self.headers.get(CSRF_HEADER) != CSRF_VALUE:
            self._error(HTTPStatus.FORBIDDEN, "Missing CSRF header.", "csrf")
            return

        if path == "/api/login":
            self._handle_login(payload)
            return
        if path == "/api/logout":
            self._json(HTTPStatus.OK, {"ok": True}, {"Set-Cookie": self.server.auth.clear_cookie_header()})
            return
        if not self._authenticated():
            self._error(HTTPStatus.UNAUTHORIZED, "Authentication required.", "unauthorized")
            return
        self._handle_api_post(path, payload)

    # ------------------------------------------------------------------
    # Public login
    # ------------------------------------------------------------------
    def _handle_ea_poll(self) -> None:
        engine = self.server.engine
        expected = self.server.config.ea_token
        supplied = self.headers.get("X-EA-Token", "")
        if not expected or engine.ea is None:
            self._error(HTTPStatus.FORBIDDEN, "MT4 EA bridge is not configured on the dashboard.", "ea_disabled")
            return
        if not hmac.compare_digest(str(supplied), str(expected)):
            self._error(HTTPStatus.FORBIDDEN, "Invalid MT4 EA token.", "ea_auth")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._error(HTTPStatus.BAD_REQUEST, "Invalid Content-Length.", "bad_request")
            return
        if length <= 0 or length > EA_MAX_BODY:
            self._error(HTTPStatus.BAD_REQUEST, "EA snapshot is empty or too large.", "bad_request")
            return
        raw = self.rfile.read(length)
        try:
            body = raw.decode("utf-8", "replace")
            response = engine.ea.handle_poll(body)
        except EAProtocolError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc), "ea_protocol")
            return
        except Exception:
            LOGGER.exception("MT4 EA poll failed")
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "EA poll failed.", "ea_error")
            return
        payload = response.encode("utf-8")
        self._send(HTTPStatus.OK, payload, "text/plain; charset=utf-8")

    def _handle_login(self, payload: Dict[str, Any]) -> None:
        auth = self.server.auth
        if not auth.enabled:
            self._json(HTTPStatus.OK, {"ok": True, "auth_enabled": False})
            return
        ip = self._client_ip()
        allowed, retry_after = auth.login_allowed(ip)
        if not allowed:
            self._error(HTTPStatus.TOO_MANY_REQUESTS, f"Too many attempts. Try again in {retry_after}s.", "rate_limited")
            return
        if not auth.check_password(str(payload.get("password", ""))):
            auth.record_failure(ip)
            self.server.engine.store.log("WARN", "login_failed", "Dashboard login failed.", {"ip": ip})
            self._error(HTTPStatus.UNAUTHORIZED, "Incorrect password.", "bad_password")
            return
        auth.clear_failures(ip)
        token = auth.issue()
        self.server.engine.store.log("INFO", "login", "Dashboard operator signed in.")
        self._json(HTTPStatus.OK, {"ok": True, "auth_enabled": True}, {"Set-Cookie": auth.cookie_header(token)})

    # ------------------------------------------------------------------
    # Authenticated API
    # ------------------------------------------------------------------
    def _handle_api_get(self, path: str, query: Dict[str, list]) -> None:
        engine = self.server.engine
        try:
            if path in {"/api/state", "/api/dashboard"}:
                self._json(HTTPStatus.OK, {"ok": True, "state": engine.dashboard_state()})
                return
            if path == "/api/bootstrap":
                self._json(HTTPStatus.OK, {"ok": True, **engine.bootstrap()})
                return
            if path == "/api/candles":
                timeframe = (query.get("timeframe") or [engine.settings.get("timeframe", "M5")])[0]
                try:
                    limit = int((query.get("limit") or ["220"])[0])
                except ValueError:
                    limit = 220
                self._json(HTTPStatus.OK, {"ok": True, **engine.candles(timeframe, limit)})
                return
            if path == "/api/trades":
                try:
                    limit = int((query.get("limit") or ["100"])[0])
                except ValueError:
                    limit = 100
                limit = max(1, min(limit, 500))
                from .metrics import public_trade

                rows = engine.store.recent_trades(limit=limit, mode=engine.trade_mode)
                self._json(HTTPStatus.OK, {"ok": True, "trades": [public_trade(t) for t in rows], "mode": engine.mode})
                return
            if path == "/api/logs":
                try:
                    limit = int((query.get("limit") or ["100"])[0])
                except ValueError:
                    limit = 100
                self._json(HTTPStatus.OK, {"ok": True, "logs": engine.store.logs(limit)})
                return
            if path == "/api/backtest/latest":
                latest = engine.store.latest_backtest()
                self._json(HTTPStatus.OK, {"ok": True, "backtest": latest})
                return
            if path == "/api/health":
                self._json(HTTPStatus.OK, {
                    "ok": True,
                    "app": APP_NAME,
                    "version": APP_VERSION,
                    "mode": engine.mode,
                    "connected": engine.connected,
                    "data_dir": str(self.server.config.data_dir),
                })
                return
            self._error(HTTPStatus.NOT_FOUND, "Unknown API route.", "not_found")
        except EngineError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc), "engine_error")
        except BrokerError as exc:
            self._error(HTTPStatus.BAD_GATEWAY, str(exc), "broker_error")
        except Exception:
            LOGGER.exception("Unhandled GET API error")
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "Internal server error.", "internal")

    def _handle_api_post(self, path: str, payload: Dict[str, Any]) -> None:
        engine = self.server.engine
        try:
            if path == "/api/bot/start":
                engine.start_bot()
                self._ok_state()
                return
            if path == "/api/bot/pause":
                engine.pause_bot()
                self._ok_state()
                return
            if path == "/api/analyze":
                state = engine.analyze_now()
                self._json(HTTPStatus.OK, {"ok": True, "state": state})
                return
            if path == "/api/halt":
                engine.emergency_halt()
                self._ok_state()
                return
            if path == "/api/halt/clear":
                engine.clear_halt()
                self._ok_state()
                return
            if path == "/api/positions/close":
                ticket = str(payload.get("ticket", "")).strip()
                result = engine.close_position(ticket)
                self._json(HTTPStatus.OK, {"ok": True, "result": result, "state": engine.dashboard_state()})
                return
            if path == "/api/positions/close-all":
                result = engine.close_all_positions()
                self._json(HTTPStatus.OK, {"ok": True, "result": result, "state": engine.dashboard_state()})
                return
            if path == "/api/mode":
                state = engine.switch_mode(str(payload.get("mode", "")))
                self._json(HTTPStatus.OK, {"ok": True, "connection": state, "state": engine.dashboard_state()})
                return
            if path == "/api/reconnect":
                connection = engine.reconnect()
                self._json(HTTPStatus.OK, {"ok": True, "connection": connection, "state": engine.dashboard_state()})
                return
            if path == "/api/live/arm":
                result = engine.arm_live(str(payload.get("phrase", "")))
                self._json(HTTPStatus.OK, {"ok": True, **result, "state": engine.dashboard_state()})
                return
            if path == "/api/live/disarm":
                result = engine.disarm_live()
                self._json(HTTPStatus.OK, {"ok": True, **result, "state": engine.dashboard_state()})
                return
            if path == "/api/settings":
                updates = payload.get("settings") or payload
                if not isinstance(updates, dict):
                    raise EngineError("Settings payload must be an object.")
                engine.update_settings(updates)
                self._ok_state()
                return
            if path == "/api/reset-paper":
                if str(payload.get("confirm", "")) != "RESET PAPER":
                    raise EngineError("Type RESET PAPER to confirm a paper account reset.")
                balance = payload.get("balance")
                result = engine.reset_paper(float(balance) if balance is not None else None)
                self._json(HTTPStatus.OK, {"ok": True, **result, "state": engine.dashboard_state()})
                return
            if path == "/api/backtest":
                try:
                    days = int(payload.get("days", 30))
                except (TypeError, ValueError):
                    raise EngineError("Days must be a number.")
                result = engine.run_backtest(days)
                self._json(HTTPStatus.OK, {"ok": True, "backtest": result, "state": engine.dashboard_state()})
                return
            if path == "/api/backtest/csv":
                try:
                    offset = int(payload.get("utc_offset_minutes", 120))
                    spread = float(payload.get("spread", 0.20))
                    commission = float(payload.get("commission_per_lot", 7.0))
                    slippage = float(payload.get("slippage", 0.02))
                except (TypeError, ValueError):
                    raise EngineError("UTC offset, spread, commission and slippage must be numeric.")
                result = engine.run_csv_backtest(
                    str(payload.get("csv", "")),
                    filename=str(payload.get("filename", "upload.csv")),
                    timeframe=str(payload.get("timeframe", "M5")),
                    utc_offset_minutes=offset,
                    spread=spread,
                    commission_per_lot=commission,
                    slippage=slippage,
                )
                self._json(HTTPStatus.OK, {"ok": True, "backtest": result, "state": engine.dashboard_state()})
                return
            self._error(HTTPStatus.NOT_FOUND, "Unknown API route.", "not_found")
        except EngineError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc), "engine_error")
        except BrokerError as exc:
            self._error(HTTPStatus.BAD_GATEWAY, str(exc), "broker_error")
        except SettingsError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc), "settings_error")
        except Exception:
            LOGGER.exception("Unhandled POST API error")
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "Internal server error.", "internal")

    def _ok_state(self) -> None:
        self._json(HTTPStatus.OK, {"ok": True, "state": self.server.engine.dashboard_state()})

    # ------------------------------------------------------------------
    # Static files
    # ------------------------------------------------------------------
    def _serve_static(self, path: str) -> None:
        relative = "index.html" if path in {"", "/"} else path.lstrip("/")
        if "/" in relative or ".." in relative:
            self._error(HTTPStatus.NOT_FOUND, "Not found.", "not_found")
            return
        file_path = (STATIC_DIR / relative).resolve()
        try:
            if file_path.parent != STATIC_DIR or not file_path.is_file():
                raise FileNotFoundError
            body = file_path.read_bytes()
        except FileNotFoundError:
            self._send(HTTPStatus.NOT_FOUND, b"Not found", "text/plain; charset=utf-8")
            return
        content_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in {"application/javascript", "image/svg+xml"}:
            content_type += "; charset=utf-8"
        headers = {"Cache-Control": "no-cache"}
        self._send(HTTPStatus.OK, body, content_type, headers)


def create_server(config: Config, engine: TradingEngine) -> AurumHTTPServer:
    try:
        server = AurumHTTPServer((config.host, config.port), engine, config)
    except OSError as exc:
        if getattr(exc, "errno", None) == 48 or "Address already in use" in str(exc):
            raise SystemExit(f"Port {config.port} is already in use. Set AURUM_PORT to another port.")
        raise
    return server

