"""Integration test for the MT5 bridge adapter and live execution path.

A local fake bridge stands in for the Windows MT5 terminal so the complete
mode-switch -> arm -> order -> close flow can be tested on macOS/Linux.
"""
import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from app.config import Config
from app.db import Store
from app.engine import EngineError, TradingEngine
from app.models import Candle

TOKEN = "test-bridge-token-1234567890"


def trend_rows(count=320):
    start = datetime.now(timezone.utc) - timedelta(minutes=5 * count)
    rows = []
    price = 4000.0
    for i in range(count):
        open_price = price
        close = price + 0.6
        rows.append({
            "time": int((start + timedelta(minutes=5 * i)).timestamp()),
            "open": open_price,
            "high": max(open_price, close) + 0.2,
            "low": min(open_price, close) - 0.2,
            "close": close,
            "volume": 1000,
        })
        price = close
    return rows


class FakeBridge:
    def __init__(self):
        self.lock = threading.Lock()
        self.positions = []
        self.orders = []
        self.modifications = []
        self.headers_seen = []
        self.quote_bid = 4000.0
        self.quote_ask = 4000.2
        self.httpd = None
        self.thread = None

    def start(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _json(self, status, payload):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _authorized(self):
                return self.headers.get("Authorization") == "Bearer " + TOKEN

            def do_GET(self):
                parsed = urlparse(self.path)
                if not self._authorized():
                    self._json(401, {"error": "bad token"})
                    return
                fake.headers_seen.append(dict(self.headers))
                with fake.lock:
                    if parsed.path == "/health":
                        self._json(200, {"ok": True, "symbol": "XAUUSD", "server": "Mock-Demo", "broker": "Mock", "account_type": "demo", "live_allowed": True})
                    elif parsed.path == "/account":
                        self._json(200, {"account": {
                            "login": 123456, "server": "Mock-Demo", "name": "Test", "broker": "Mock",
                            "currency": "USD", "leverage": 100, "balance": 5000, "equity": 5000,
                            "margin": 0, "free_margin": 5000, "margin_level": 0, "account_type": "demo",
                        }})
                    elif parsed.path == "/quote":
                        self._json(200, {"quote": {"symbol": "XAUUSD", "bid": fake.quote_bid, "ask": fake.quote_ask, "time_iso": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}})
                    elif parsed.path == "/positions":
                        self._json(200, {"positions": fake.positions})
                    elif parsed.path == "/candles":
                        self._json(200, {"candles": trend_rows()})
                    else:
                        self._json(404, {"error": "not found"})

            def do_POST(self):
                if not self._authorized():
                    self._json(401, {"error": "bad token"})
                    return
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length) or b"{}")
                parsed = urlparse(self.path)
                fake.headers_seen.append(dict(self.headers))
                with fake.lock:
                    if parsed.path == "/order":
                        ticket = "900001"
                        fake.orders.append({"body": body, "live_confirm": self.headers.get("X-Live-Confirm")})
                        fake.positions = [{
                            "ticket": ticket, "symbol": "XAUUSD", "side": body["side"], "lots": body["lots"],
                            "entry_price": body["sl"] + 2.0, "current_price": 4001.0,
                            "sl": body["sl"], "tp": body["tp"], "pnl": 1.0,
                            "opened_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                        }]
                        self._json(200, {"ok": True, "ticket": ticket, "price": 4000.25, "lots": body["lots"], "message": "filled"})
                    elif parsed.path == "/modify":
                        ticket = str(body.get("ticket"))
                        fake.modifications.append({"body": body, "live_confirm": self.headers.get("X-Live-Confirm")})
                        for position in fake.positions:
                            if position["ticket"] == ticket:
                                position["sl"] = body["sl"]
                                position["tp"] = body["tp"]
                        self._json(200, {"ok": True, "ticket": ticket, "sl": body["sl"], "tp": body["tp"]})
                    elif parsed.path == "/close":
                        ticket = str(body.get("ticket"))
                        fake.positions = [p for p in fake.positions if p["ticket"] != ticket]
                        self._json(200, {"ok": True, "ticket": ticket, "price": 4001.2, "profit": 12.5, "lots": 0.1})
                    elif parsed.path == "/close_all":
                        fake.positions = []
                        self._json(200, {"ok": True, "results": [], "count": 0})
                    else:
                        self._json(404, {"error": "not found"})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
        if self.thread:
            self.thread.join(timeout=2)


class LiveBridgeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeBridge()
        self.url = self.fake.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "live.db", 5000)
        self.config = Config(
            data_dir=Path(self.tmp.name),
            bridge_url=self.url,
            bridge_token=TOKEN,
            live_enabled=True,
            live_arm_phrase="ARM TEST",
            symbol="XAUUSD",
        )
        self.engine = TradingEngine(self.config, self.store)

    def tearDown(self):
        try:
            self.engine.mt5.disconnect() if self.engine.mt5 else None
        finally:
            self.store.close()
            self.fake.close()
            self.tmp.cleanup()

    def test_live_arm_order_close(self):
        connection = self.engine.switch_mode("mt5")
        self.assertEqual(connection["mode"], "mt5")
        self.assertTrue(connection["connected"])
        self.assertFalse(connection["armed"])
        with self.assertRaises(EngineError):
            self.engine.start_bot()
        with self.assertRaises(EngineError):
            self.engine.arm_live("wrong phrase")
        self.engine.arm_live("ARM TEST")
        self.assertTrue(self.engine._armed_active())
        self.engine.start_bot()
        self.engine._analyze(execute=True)
        self.assertTrue(self.fake.orders, "no live order reached the fake bridge")
        order = self.fake.orders[0]
        self.assertEqual(order["live_confirm"], "YES")
        self.assertIn(order["body"]["side"], {"BUY", "SELL"})
        self.assertGreater(order["body"]["sl"], 0)
        open_trades = self.store.get_open_trades(mode="live")
        self.assertEqual(len(open_trades), 1)
        ticket = str(open_trades[0]["ticket"])
        # Simulate a profitable continuation and let the engine's bracket
        # manager trail the stop / extend the target through the bridge.
        self.fake.quote_bid = 4003.0
        self.fake.quote_ask = 4003.2
        self.engine._last_bracket_check = time.monotonic() - 10.0
        self.assertEqual(self.engine.refresh(), True)
        self.engine._manage_brackets()
        self.assertTrue(self.fake.modifications, "engine did not send a protection update")
        updated = self.store.get_trade(ticket)
        self.assertGreater(updated["sl"], float(open_trades[0]["sl"]))
        modified = self.engine.mt5.modify_protection(ticket, 4000.5, 4002.5)
        self.assertTrue(modified["ok"])
        self.assertEqual(self.fake.modifications[0]["live_confirm"], "YES")
        result = self.engine.close_position(ticket)
        self.assertTrue(result["ok"])
        self.assertEqual(len(self.store.get_open_trades(mode="live")), 0)

    def test_switch_to_paper_is_blocked_with_open_live_trade(self):
        self.engine.switch_mode("mt5")
        self.engine.arm_live("ARM TEST")
        self.engine.start_bot()
        self.engine._analyze(execute=True)
        with self.assertRaises(EngineError):
            self.engine.switch_mode("paper")


if __name__ == "__main__":
    unittest.main()
