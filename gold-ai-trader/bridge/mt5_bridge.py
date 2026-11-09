#!/usr/bin/env python3
"""AURUM AI — MetaTrader 5 Windows bridge.

The official MetaTrader5 Python package only works on Windows alongside a
running MT5 terminal. This small, token-authenticated HTTP bridge lets the
AURUM dashboard run in Docker/Linux while MT5 stays where it belongs.

Security model
--------------
* Binds to 127.0.0.1 by default. Use an SSH tunnel or private VPN to reach it.
* Every route requires ``Authorization: Bearer <BRIDGE_TOKEN>``.
* Real-account orders require BOTH ``BRIDGE_ALLOW_LIVE=true`` and the
  ``X-Live-Confirm: YES`` header sent by the armed dashboard.
* Demo-account orders are allowed when the token is valid; no real capital is
  at risk on a demo account.
* Idempotency keys defend against duplicate requests within one bridge process.
"""
from __future__ import annotations

import argparse
import hmac
import json
import logging
import math
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

try:
    import MetaTrader5 as mt5  # type: ignore
except ImportError:  # pragma: no cover - exercised on non-Windows hosts
    mt5 = None  # type: ignore

BRIDGE_VERSION = "1.0.0"
ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger("aurum.bridge")
MAX_BODY = 64 * 1024
MAGIC_DEFAULT = 20261101
IDEMPOTENCY_LIMIT = 400
TF_SECONDS = {"M5": 300, "M15": 900, "M30": 1800, "H1": 3600}


class BridgeError(RuntimeError):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class LiveDenied(BridgeError):
    def __init__(self, message: str) -> None:
        super().__init__(message, 403)


def _load_env(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key and key.replace("_", "").isalnum():
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            os.environ.setdefault(key, value)


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "y"}


def _iso(epoch: Optional[float]) -> Optional[str]:
    if epoch is None:
        return None
    try:
        return datetime.fromtimestamp(float(epoch), tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    except (ValueError, OSError, OverflowError):
        return None


def _round_price(value: float, digits: int) -> float:
    return round(float(value), max(0, min(8, digits)))


def _account_type(info: Any) -> str:
    mode = int(getattr(info, "trade_mode", 0) or 0)
    if mt5 is not None:
        if mode == getattr(mt5, "ACCOUNT_TRADE_MODE_REAL", 2):
            return "real"
        if mode == getattr(mt5, "ACCOUNT_TRADE_MODE_CONTEST", 1):
            return "contest"
    return "demo" if mode == 0 else "contest" if mode == 1 else "real" if mode == 2 else "unknown"


@dataclass
class BridgeConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    token: str = ""
    symbol: str = "XAUUSD"
    login: str = ""
    password: str = ""
    server: str = ""
    terminal_path: str = ""
    allow_live: bool = False
    magic: int = MAGIC_DEFAULT
    log_level: str = "INFO"

    @classmethod
    def from_env(cls, env_path: Optional[Path] = None) -> "BridgeConfig":
        _load_env(env_path or Path(os.environ.get("BRIDGE_ENV_FILE", ROOT / "bridge.env")))
        try:
            port = int(_env("BRIDGE_PORT", "8765"))
        except ValueError:
            port = 8765
        try:
            magic = int(_env("BRIDGE_MAGIC", str(MAGIC_DEFAULT)))
        except ValueError:
            magic = MAGIC_DEFAULT
        return cls(
            host=_env("BRIDGE_HOST", "127.0.0.1"),
            port=max(1, min(65535, port)),
            token=_env("BRIDGE_TOKEN"),
            symbol=re.sub(r"[^A-Za-z0-9._#-]", "", _env("MT5_SYMBOL", "XAUUSD").upper())[:24] or "XAUUSD",
            login=_env("MT5_LOGIN"),
            password=os.environ.get("MT5_PASSWORD", ""),
            server=_env("MT5_SERVER"),
            terminal_path=_env("MT5_PATH"),
            allow_live=_env_bool("BRIDGE_ALLOW_LIVE", False),
            magic=magic,
            log_level=_env("BRIDGE_LOG_LEVEL", "INFO").upper(),
        )


class MT5Connector:
    """Serialised access to the MetaTrader5 Python API."""

    def __init__(self, config: BridgeConfig) -> None:
        self.config = config
        self.lock = threading.RLock()
        self.initialized = False
        self._last_error = ""
        self._idempotency: Dict[str, Dict[str, Any]] = {}
        self._last_order_at = 0.0

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------
    def ensure_initialized(self) -> None:
        if mt5 is None:
            raise BridgeError("The MetaTrader5 Python package is not installed. Run: pip install MetaTrader5", 503)
        if self.initialized:
            try:
                if mt5.terminal_info() is not None and mt5.account_info() is not None:
                    return
            except Exception:
                pass
            self.initialized = False
        kwargs: Dict[str, Any] = {}
        if self.config.login:
            try:
                kwargs["login"] = int(self.config.login)
            except ValueError:
                raise BridgeError("MT5_LOGIN must be numeric.", 500)
            kwargs["password"] = self.config.password
            kwargs["server"] = self.config.server
        if self.config.terminal_path:
            kwargs["path"] = self.config.terminal_path
        try:
            ok = mt5.initialize(**kwargs)
        except Exception as exc:
            raise BridgeError(f"MT5 initialization failed: {type(exc).__name__}", 503)
        if not ok:
            err = mt5.last_error() if mt5 is not None else ("bridge", "unavailable")
            raise BridgeError(f"MT5 initialize() failed: {err}. Is the terminal running and logged in?", 503)
        info = mt5.account_info()
        if info is None:
            raise BridgeError("MT5 initialized but no account is logged in.", 503)
        self.initialized = True
        self._last_error = ""
        LOG.info("Connected to MT5 account %s on %s (%s)", getattr(info, "login", "?"), getattr(info, "server", "?"), _account_type(info))

    def shutdown(self) -> None:
        if mt5 is not None and self.initialized:
            try:
                mt5.shutdown()
            except Exception:
                pass
        self.initialized = False

    def _info(self) -> Any:
        self.ensure_initialized()
        info = mt5.account_info()  # type: ignore[union-attr]
        if info is None:
            raise BridgeError("MT5 account info unavailable.", 503)
        return info

    def account_type(self) -> str:
        with self.lock:
            return _account_type(self._info())

    def _require_live_permission(self, headers: Any) -> None:
        if self.account_type() == "real":
            if not self.config.allow_live:
                raise LiveDenied("Live orders are disabled on this bridge. Set BRIDGE_ALLOW_LIVE=true deliberately.")
            if str(headers.get("X-Live-Confirm", "")) != "YES":
                raise LiveDenied("Real-account order rejected: missing X-Live-Confirm header.")

    # ------------------------------------------------------------------
    # Read methods
    # ------------------------------------------------------------------
    def health(self) -> Dict[str, Any]:
        with self.lock:
            self.ensure_initialized()
            account = self._info()
            terminal = mt5.terminal_info()  # type: ignore[union-attr]
            return {
                "ok": True,
                "version": BRIDGE_VERSION,
                "connected": True,
                "symbol": self.config.symbol,
                "server": str(getattr(account, "server", "")),
                "broker": str(getattr(account, "company", "")),
                "account_type": _account_type(account),
                "login": int(getattr(account, "login", 0) or 0),
                "live_allowed": bool(self.config.allow_live),
                "terminal": str(getattr(terminal, "name", "")) if terminal is not None else "",
                "build": int(getattr(terminal, "build", 0) or 0) if terminal is not None else 0,
                "time": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            }

    def account(self) -> Dict[str, Any]:
        with self.lock:
            info = self._info()
            return {"account": {
                "login": int(getattr(info, "login", 0) or 0),
                "name": str(getattr(info, "name", "") or ""),
                "server": str(getattr(info, "server", "") or ""),
                "broker": str(getattr(info, "company", "") or ""),
                "currency": str(getattr(info, "currency", "USD") or "USD"),
                "leverage": int(getattr(info, "leverage", 0) or 0),
                "balance": float(getattr(info, "balance", 0.0) or 0.0),
                "equity": float(getattr(info, "equity", 0.0) or 0.0),
                "margin": float(getattr(info, "margin", 0.0) or 0.0),
                "free_margin": float(getattr(info, "margin_free", 0.0) or 0.0),
                "margin_level": float(getattr(info, "margin_level", 0.0) or 0.0),
                "profit": float(getattr(info, "profit", 0.0) or 0.0),
                "account_type": _account_type(info),
                "trade_allowed": bool(getattr(info, "trade_allowed", False)),
            }}

    def _select_symbol(self) -> Any:
        self.ensure_initialized()
        symbol = self.config.symbol
        info = mt5.symbol_info(symbol)  # type: ignore[union-attr]
        if info is None:
            raise BridgeError(f"Symbol {symbol} not found in Market Watch. Add it in MT5 first.", 400)
        if not info.visible:
            if not mt5.symbol_select(symbol, True):  # type: ignore[union-attr]
                raise BridgeError(f"Could not select symbol {symbol} in Market Watch.", 400)
            info = mt5.symbol_info(symbol)  # type: ignore[union-attr]
        return info

    def quote(self) -> Dict[str, Any]:
        with self.lock:
            self._select_symbol()
            tick = mt5.symbol_info_tick(self.config.symbol)  # type: ignore[union-attr]
            if tick is None or float(tick.bid) <= 0 or float(tick.ask) <= 0:
                raise BridgeError("MT5 returned no live tick for the configured symbol.", 503)
            return {"quote": {
                "symbol": self.config.symbol,
                "bid": float(tick.bid),
                "ask": float(tick.ask),
                "time": int(tick.time),
                "time_iso": _iso(tick.time),
                "spread": round(float(tick.ask) - float(tick.bid), 4),
            }}

    def candles(self, timeframe: str, count: int) -> Dict[str, Any]:
        timeframe = timeframe.upper()
        if timeframe not in TF_SECONDS:
            raise BridgeError("Unsupported timeframe. Use M5, M15, M30 or H1.")
        count = max(10, min(int(count), 1000))
        tf_const = getattr(mt5, f"TIMEFRAME_{timeframe}", None) if mt5 is not None else None
        if tf_const is None:
            raise BridgeError("MT5 timeframe mapping is unavailable.", 503)
        with self.lock:
            self._select_symbol()
            rates = mt5.copy_rates_from_pos(self.config.symbol, tf_const, 0, count)  # type: ignore[union-attr]
            if rates is None:
                raise BridgeError(f"MT5 candle request failed: {mt5.last_error()}", 503)
            candles = []
            for bar in rates:
                candles.append({
                    "time": int(bar["time"]),
                    "open": float(bar["open"]),
                    "high": float(bar["high"]),
                    "low": float(bar["low"]),
                    "close": float(bar["close"]),
                    "volume": float(bar["tick_volume"]),
                })
            return {"candles": candles, "symbol": self.config.symbol, "timeframe": timeframe}

    def positions(self) -> Dict[str, Any]:
        with self.lock:
            self.ensure_initialized()
            raw = mt5.positions_get(symbol=self.config.symbol)  # type: ignore[union-attr]
            if raw is None:
                raw = []
            positions = []
            for pos in raw:
                side = "BUY" if int(pos.type) == getattr(mt5, "POSITION_TYPE_BUY", 0) else "SELL"
                positions.append({
                    "ticket": str(pos.ticket),
                    "symbol": str(pos.symbol),
                    "side": side,
                    "lots": float(pos.volume),
                    "entry_price": float(pos.price_open),
                    "current_price": float(pos.price_current),
                    "sl": float(pos.sl),
                    "tp": float(pos.tp),
                    "pnl": float(pos.profit),
                    "opened_at": _iso(pos.time),
                    "magic": int(getattr(pos, "magic", 0) or 0),
                    "comment": str(getattr(pos, "comment", "") or ""),
                })
            return {"positions": positions, "symbol": self.config.symbol}

    # ------------------------------------------------------------------
    # Trading
    # ------------------------------------------------------------------
    def _filling_mode(self, info: Any) -> Any:
        flags = int(getattr(info, "filling_mode", 0) or 0)
        # SYMBOL_FILLING_FOK = 1, SYMBOL_FILLING_IOC = 2
        if flags & 2:
            return mt5.ORDER_FILLING_IOC  # type: ignore[union-attr]
        if flags & 1:
            return mt5.ORDER_FILLING_FOK  # type: ignore[union-attr]
        return mt5.ORDER_FILLING_RETURN  # type: ignore[union-attr]

    def _send(self, request: Dict[str, Any]) -> Any:
        result = mt5.order_send(request)  # type: ignore[union-attr]
        if result is None:
            raise BridgeError(f"MT5 order_send returned no result: {mt5.last_error()}", 502)  # type: ignore[union-attr]
        done_codes = {
            int(getattr(mt5, "TRADE_RETCODE_DONE", 10009)),
            int(getattr(mt5, "TRADE_RETCODE_DONE_PARTIAL", 10010)),
        }
        if int(result.retcode) not in done_codes:
            raise BridgeError(f"MT5 rejected order: retcode={result.retcode} {getattr(result, 'comment', '')}", 502)
        return result

    def _remember(self, request_id: str, payload: Dict[str, Any]) -> None:
        if not request_id:
            return
        self._idempotency[request_id] = payload
        if len(self._idempotency) > IDEMPOTENCY_LIMIT:
            oldest = next(iter(self._idempotency))
            self._idempotency.pop(oldest, None)

    def order(self, body: Dict[str, Any], headers: Any) -> Dict[str, Any]:
        with self.lock:
            request_id = str(body.get("request_id", ""))[:80]
            if request_id and request_id in self._idempotency:
                cached = dict(self._idempotency[request_id])
                cached["idempotent_replay"] = True
                return cached
            self._require_live_permission(headers)
            symbol = str(body.get("symbol", self.config.symbol))
            if symbol != self.config.symbol:
                raise BridgeError("This bridge is pinned to one symbol; symbol mismatch.")
            side = str(body.get("side", "")).upper()
            if side not in {"BUY", "SELL"}:
                raise BridgeError("side must be BUY or SELL.")
            try:
                lots = float(body.get("lots", 0))
                sl = float(body.get("sl", 0))
                tp = float(body.get("tp", 0))
            except (TypeError, ValueError):
                raise BridgeError("lots, sl and tp must be numeric.")
            if not all(math.isfinite(x) for x in (lots, sl, tp)) or lots <= 0:
                raise BridgeError("Invalid order size or bracket.")

            info = self._select_symbol()
            step = float(getattr(info, "volume_step", 0.01) or 0.01)
            min_lot = float(getattr(info, "volume_min", 0.01) or 0.01)
            max_lot = float(getattr(info, "volume_max", 100.0) or 100.0)
            lots = math.floor(lots / step + 1e-9) * step
            lots = round(lots, 2)
            if lots < min_lot or lots > max_lot:
                raise BridgeError(f"Volume {lots} outside broker range {min_lot}-{max_lot}.")
            digits = int(getattr(info, "digits", 2) or 2)
            point = float(getattr(info, "point", 0.01) or 0.01)
            tick = mt5.symbol_info_tick(symbol)  # type: ignore[union-attr]
            if tick is None:
                raise BridgeError("No tick available for order.", 503)
            price = float(tick.ask if side == "BUY" else tick.bid)
            sl = _round_price(sl, digits)
            tp = _round_price(tp, digits)
            if sl <= 0 or tp <= 0:
                raise BridgeError("A protective stop-loss and take-profit are required.")
            if side == "BUY" and not (sl < price < tp):
                raise BridgeError("BUY bracket must satisfy SL < price < TP.")
            if side == "SELL" and not (tp < price < sl):
                raise BridgeError("SELL bracket must satisfy TP < price < SL.")
            min_stop = float(getattr(info, "trade_stops_level", 0) or 0) * point
            if min_stop and (abs(price - sl) < min_stop or abs(tp - price) < min_stop):
                raise BridgeError("Stop or target is inside the broker's minimum stop distance.")
            now = time.time()
            if now - self._last_order_at < 0.8:
                raise BridgeError("Order throttled; wait a moment and retry.", 429)
            self._last_order_at = now
            comment = re.sub(r"[^A-Za-z0-9 _-]", "", str(body.get("comment", "AURUM")))[:31] or "AURUM"
            request = {
                "action": mt5.TRADE_ACTION_DEAL,  # type: ignore[union-attr]
                "symbol": symbol,
                "volume": float(lots),
                "type": mt5.ORDER_TYPE_BUY if side == "BUY" else mt5.ORDER_TYPE_SELL,  # type: ignore[union-attr]
                "price": price,
                "sl": sl,
                "tp": tp,
                "deviation": 30,
                "magic": int(self.config.magic),
                "comment": comment,
                "type_time": mt5.ORDER_TIME_GTC,  # type: ignore[union-attr]
                "type_filling": self._filling_mode(info),
            }
            result = self._send(request)
            # MQL order tickets are not position tickets. The dashboard closes
            # and reconciles by *position* ticket, so resolve it immediately.
            position_ticket = ""
            try:
                opened = mt5.positions_get(symbol=symbol) or []  # type: ignore[union-attr]
                recent = [pos for pos in opened if int(getattr(pos, "magic", 0) or 0) == int(self.config.magic)]
                if recent:
                    newest = max(recent, key=lambda pos: int(getattr(pos, "time", 0) or 0))
                    if abs(float(getattr(newest, "volume", 0) or 0) - lots) < 1e-8:
                        position_ticket = str(newest.ticket)
            except Exception:
                position_ticket = ""
            payload = {
                "ok": True,
                "ticket": position_ticket or str(getattr(result, "order", 0) or getattr(result, "deal", 0) or ""),
                "order_ticket": str(getattr(result, "order", 0) or ""),
                "price": float(getattr(result, "price", price) or price),
                "lots": float(getattr(result, "volume", lots) or lots),
                "message": f"MT5 {side} order filled",
                "retcode": int(result.retcode),
            }
            self._remember(request_id, payload)
            LOG.info("Order filled: %s %.2f %s at %.2f (ticket %s)", side, lots, symbol, payload["price"], payload["ticket"])
            return payload

    def _close_internal(self, position: Any) -> Dict[str, Any]:
        tick = mt5.symbol_info_tick(position.symbol)  # type: ignore[union-attr]
        if tick is None:
            raise BridgeError("No tick available to close the position.", 503)
        info = mt5.symbol_info(position.symbol)  # type: ignore[union-attr]
        if info is None:
            raise BridgeError("Symbol info unavailable while closing.", 503)
        closing_buy = int(position.type) == getattr(mt5, "POSITION_TYPE_SELL", 1)
        price = float(tick.ask if closing_buy else tick.bid)
        request = {
                "action": mt5.TRADE_ACTION_DEAL,  # type: ignore[union-attr]
                "symbol": position.symbol,
                "volume": float(position.volume),
                "type": mt5.ORDER_TYPE_BUY if closing_buy else mt5.ORDER_TYPE_SELL,  # type: ignore[union-attr]
            "position": int(position.ticket),
            "price": price,
            "deviation": 30,
            "magic": int(self.config.magic),
            "comment": "AURUM close",
            "type_time": mt5.ORDER_TIME_GTC,  # type: ignore[union-attr]
            "type_filling": self._filling_mode(info),
        }
        result = self._send(request)
        remaining = mt5.positions_get(ticket=int(position.ticket))  # type: ignore[union-attr]
        if remaining is None:
            raise BridgeError("MT5 accepted the close request, but the remaining position could not be verified.", 503)
        remaining_volume = sum(float(getattr(item, "volume", 0.0) or 0.0) for item in remaining)
        if remaining_volume > 1e-8:
            raise BridgeError(
                f"MT5 only partially closed position {position.ticket}; {remaining_volume:.2f} lots remain. "
                "Review the terminal before retrying.",
                502,
            )
        return {
            "ok": True,
            "ticket": str(position.ticket),
            "price": float(getattr(result, "price", price) or price),
            "lots": float(getattr(result, "volume", position.volume) or position.volume),
            "profit": float(getattr(position, "profit", 0.0) or 0.0),
            "message": "MT5 position closed",
            "retcode": int(result.retcode),
        }

    def close(self, body: Dict[str, Any], headers: Any) -> Dict[str, Any]:
        with self.lock:
            request_id = str(body.get("request_id", ""))[:80]
            if request_id and request_id in self._idempotency:
                cached = dict(self._idempotency[request_id])
                cached["idempotent_replay"] = True
                return cached
            self._require_live_permission(headers)
            try:
                ticket = int(str(body.get("ticket", "")))
            except (TypeError, ValueError):
                raise BridgeError("A numeric ticket is required.")
            positions = mt5.positions_get(ticket=ticket)  # type: ignore[union-attr]
            if positions is None:
                raise BridgeError("Could not query MT5 positions.", 503)
            if not positions:
                raise BridgeError("Position not found or already closed.", 404)
            position = positions[0]
            if str(position.symbol) != self.config.symbol:
                raise BridgeError("Position is not on this bridge's configured symbol.")
            payload = self._close_internal(position)
            self._remember(request_id, payload)
            LOG.info("Closed position %s for %.2f", ticket, payload.get("profit", 0.0))
            return payload

    def modify(self, body: Dict[str, Any], headers: Any) -> Dict[str, Any]:
        with self.lock:
            request_id = "modify:" + str(body.get("request_id", ""))[:80]
            if request_id != "modify:" and request_id in self._idempotency:
                cached = dict(self._idempotency[request_id])
                cached["idempotent_replay"] = True
                return cached
            self._require_live_permission(headers)
            try:
                ticket = int(str(body.get("ticket", "")))
                sl = float(body.get("sl", 0))
                tp = float(body.get("tp", 0))
            except (TypeError, ValueError):
                raise BridgeError("ticket, sl and tp are required.")
            if not all(math.isfinite(x) for x in (sl, tp)) or sl <= 0 or tp <= 0:
                raise BridgeError("sl and tp must be positive finite prices.")
            positions = mt5.positions_get(ticket=ticket)  # type: ignore[union-attr]
            if positions is None:
                raise BridgeError("Could not query MT5 positions.", 503)
            if not positions:
                raise BridgeError("Position not found or already closed.", 404)
            position = positions[0]
            if str(position.symbol) != self.config.symbol:
                raise BridgeError("Position is not on this bridge's configured symbol.")
            info = mt5.symbol_info(position.symbol)  # type: ignore[union-attr]
            tick = mt5.symbol_info_tick(position.symbol)  # type: ignore[union-attr]
            if info is None or tick is None:
                raise BridgeError("Market data unavailable while modifying protection.", 503)
            digits = int(getattr(info, "digits", 2) or 2)
            point = float(getattr(info, "point", 0.01) or 0.01)
            sl = _round_price(sl, digits)
            tp = _round_price(tp, digits)
            is_buy = int(position.type) == getattr(mt5, "POSITION_TYPE_BUY", 0)
            price = float(tick.bid if is_buy else tick.ask)
            if is_buy:
                if not (sl < price < tp):
                    raise BridgeError("Long bracket must satisfy SL < current price < TP.")
            else:
                if not (tp < price < sl):
                    raise BridgeError("Short bracket must satisfy TP < current price < SL.")
            min_stop = float(getattr(info, "trade_stops_level", 0) or 0) * point
            if min_stop and (abs(price - sl) < min_stop or abs(tp - price) < min_stop):
                raise BridgeError("Adjusted stop or target is inside the broker's minimum stop distance.")
            current_sl = float(getattr(position, "sl", 0) or 0)
            current_tp = float(getattr(position, "tp", 0) or 0)
            if is_buy and current_sl and sl < current_sl:
                raise BridgeError("A long stop cannot be moved backwards.")
            if not is_buy and current_sl and sl > current_sl:
                raise BridgeError("A short stop cannot be moved backwards.")
            if is_buy and current_tp and tp < current_tp - 0.0001:
                raise BridgeError("A long target cannot be moved closer.")
            if not is_buy and current_tp and tp > current_tp + 0.0001:
                raise BridgeError("A short target cannot be moved closer.")
            request = {
                "action": mt5.TRADE_ACTION_SLTP,  # type: ignore[union-attr]
                "symbol": position.symbol,
                "position": int(position.ticket),
                "sl": sl,
                "tp": tp,
                "magic": int(self.config.magic),
            }
            result = self._send(request)
            payload = {
                "ok": True,
                "ticket": str(position.ticket),
                "sl": sl,
                "tp": tp,
                "message": "MT5 protection updated",
                "retcode": int(result.retcode),
            }
            self._remember(request_id, payload)
            LOG.info("Protection updated for %s: SL %.2f TP %.2f", ticket, sl, tp)
            return payload

    def close_all(self, body: Dict[str, Any], headers: Any) -> Dict[str, Any]:
        with self.lock:
            self._require_live_permission(headers)
            positions = mt5.positions_get(symbol=self.config.symbol)  # type: ignore[union-attr]  # noqa: F821
            if positions is None:
                raise BridgeError("Could not query MT5 positions.", 503)
            results = []
            for position in positions:
                try:
                    results.append(self._close_internal(position))
                except BridgeError as exc:
                    results.append({"ok": False, "ticket": str(position.ticket), "error": str(exc)})
            return {"ok": True, "results": results, "count": len(results)}

    def history(self, days: int) -> Dict[str, Any]:
        with self.lock:
            self.ensure_initialized()
            days = max(1, min(int(days), 30))
            end = datetime.utcnow()
            start = end - timedelta(days=days)
            deals = mt5.history_deals_get(start, end)  # type: ignore[union-attr]
            if deals is None:
                raise BridgeError(f"MT5 history query failed: {mt5.last_error()}", 503)
            grouped: Dict[int, Dict[str, Any]] = {}
            for deal in deals:
                position_id = int(getattr(deal, "position_id", 0) or 0)
                if position_id <= 0:
                    continue
                entry_type = int(getattr(deal, "entry", 0) or 0)
                item = grouped.setdefault(position_id, {
                    "ticket": str(position_id),
                    "symbol": str(getattr(deal, "symbol", "")),
                    "profit": 0.0,
                    "commission": 0.0,
                    "swap": 0.0,
                    "exit_price": 0.0,
                    "lots": 0.0,
                    "close_time_epoch": 0,
                    "closed": False,
                })
                item["profit"] += float(getattr(deal, "profit", 0) or 0)
                item["commission"] += float(getattr(deal, "commission", 0) or 0)
                item["swap"] += float(getattr(deal, "swap", 0) or 0)
                if entry_type in (getattr(mt5, "DEAL_ENTRY_OUT", 1), getattr(mt5, "DEAL_ENTRY_OUT_BY", 3)):
                    item["closed"] = True
                    item["exit_price"] = float(getattr(deal, "price", 0) or 0)
                    item["lots"] = float(getattr(deal, "volume", 0) or 0)
                    item["close_time_epoch"] = int(getattr(deal, "time", 0) or 0)
            closed = []
            for item in grouped.values():
                if not item["closed"]:
                    continue
                item["close_time"] = _iso(item["close_time_epoch"])
                item["profit"] = round(item["profit"], 2)
                item["commission"] = round(item["commission"], 2)
                item["swap"] = round(item["swap"], 2)
                item.pop("close_time_epoch", None)
                item.pop("closed", None)
                closed.append(item)
            closed.sort(key=lambda row: row.get("close_time") or "", reverse=True)
            return {"closed": closed}


class BridgeHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: Tuple[str, int], connector: MT5Connector, config: BridgeConfig) -> None:
        self.connector = connector
        self.config = config
        super().__init__(address, BridgeHandler)


class BridgeHandler(BaseHTTPRequestHandler):
    server: BridgeHTTPServer
    protocol_version = "HTTP/1.1"
    server_version = "AURUM-Bridge/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        LOG.debug("%s - %s", self.address_string(), fmt % args)

    def _send(self, status: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _error(self, status: int, message: str) -> None:
        self._send(status, {"ok": False, "error": message})

    def _authorized(self) -> bool:
        expected = "Bearer " + self.server.config.token
        supplied = self.headers.get("Authorization", "")
        return bool(self.server.config.token) and hmac.compare_digest(supplied, expected)

    def _read_json(self) -> Dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise BridgeError("Invalid Content-Length.")
        if length < 0 or length > MAX_BODY:
            raise BridgeError("Request body too large.")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            parsed = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, ValueError):
            raise BridgeError("Request body must be JSON.")
        if not isinstance(parsed, dict):
            raise BridgeError("Request body must be a JSON object.")
        return parsed

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if not self._authorized():
            self._error(HTTPStatus.UNAUTHORIZED, "Invalid bridge token.")
            return
        query = parse_qs(parsed.query)
        try:
            if parsed.path == "/health":
                self._send(HTTPStatus.OK, self.server.connector.health())
            elif parsed.path == "/account":
                self._send(HTTPStatus.OK, self.server.connector.account())
            elif parsed.path == "/quote":
                self._send(HTTPStatus.OK, self.server.connector.quote())
            elif parsed.path == "/candles":
                timeframe = (query.get("timeframe") or ["M5"])[0]
                try:
                    limit = int((query.get("limit") or ["240"])[0])
                except ValueError:
                    limit = 240
                self._send(HTTPStatus.OK, self.server.connector.candles(timeframe, limit))
            elif parsed.path == "/positions":
                self._send(HTTPStatus.OK, self.server.connector.positions())
            elif parsed.path == "/history":
                try:
                    days = int((query.get("days") or ["7"])[0])
                except ValueError:
                    days = 7
                self._send(HTTPStatus.OK, self.server.connector.history(days))
            else:
                self._error(HTTPStatus.NOT_FOUND, "Unknown route.")
        except LiveDenied as exc:
            self._error(HTTPStatus.FORBIDDEN, str(exc))
        except BridgeError as exc:
            self._error(exc.status, str(exc))
        except Exception:
            LOG.exception("Unhandled bridge GET error")
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "Bridge internal error.")

    def do_POST(self) -> None:  # noqa: N802
        if not self._authorized():
            self._error(HTTPStatus.UNAUTHORIZED, "Invalid bridge token.")
            return
        try:
            body = self._read_json()
        except BridgeError as exc:
            self._error(exc.status, str(exc))
            return
        path = urlparse(self.path).path
        try:
            if path == "/order":
                self._send(HTTPStatus.OK, self.server.connector.order(body, self.headers))
            elif path == "/close":
                self._send(HTTPStatus.OK, self.server.connector.close(body, self.headers))
            elif path == "/modify":
                self._send(HTTPStatus.OK, self.server.connector.modify(body, self.headers))
            elif path == "/close_all":
                self._send(HTTPStatus.OK, self.server.connector.close_all(body, self.headers))
            else:
                self._error(HTTPStatus.NOT_FOUND, "Unknown route.")
        except LiveDenied as exc:
            self._error(HTTPStatus.FORBIDDEN, str(exc))
        except BridgeError as exc:
            self._error(exc.status, str(exc))
        except Exception:
            LOG.exception("Unhandled bridge POST error")
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "Bridge internal error.")


def _validate_config(config: BridgeConfig) -> None:
    if not config.token or len(config.token) < 16:
        raise SystemExit("BRIDGE_TOKEN is missing or too short (use at least 16 characters).")
    if config.host not in {"127.0.0.1", "::1", "localhost"}:
        LOG.warning("Bridge is binding to %s. Use a firewall, SSH tunnel or private VPN; HTTP is not encrypted.", config.host)
    if config.allow_live:
        LOG.warning("BRIDGE_ALLOW_LIVE=true — real-account orders are permitted when the dashboard presents the live header.")


def main() -> int:
    parser = argparse.ArgumentParser(description="AURUM AI MetaTrader 5 Windows bridge")
    parser.add_argument("--check", action="store_true", help="validate environment and exit")
    args = parser.parse_args()
    config = BridgeConfig.from_env()
    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    _validate_config(config)
    if mt5 is None:
        raise SystemExit("MetaTrader5 package is missing. On Windows run: pip install -r requirements.txt")
    if args.check:
        print(f"Bridge check OK: bind {config.host}:{config.port}, symbol {config.symbol}, live_allowed={config.allow_live}")
        print("Start the bridge without --check to connect to the terminal.")
        return 0
    connector = MT5Connector(config)
    server = BridgeHTTPServer((config.host, config.port), connector, config)
    print("")
    print(f"  AURUM MT5 BRIDGE  v{BRIDGE_VERSION}")
    print(f"  Listening         http://{config.host}:{config.port}")
    print(f"  Symbol            {config.symbol}")
    print(f"  Live orders       {'ENABLED (header + token required)' if config.allow_live else 'disabled on real accounts'}")
    print("  Keep this terminal running beside MetaTrader 5.")
    print("")
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        LOG.info("Bridge interrupted")
    finally:
        server.server_close()
        connector.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
