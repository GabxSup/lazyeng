"""Windows MT5 bridge contract tests using a small fake MetaTrader5 API."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from bridge.mt5_bridge import BridgeConfig, BridgeError, LiveDenied, MT5Connector


class FakeMT5:
    ACCOUNT_TRADE_MODE_REAL = 2
    ACCOUNT_TRADE_MODE_CONTEST = 1
    POSITION_TYPE_BUY = 0
    POSITION_TYPE_SELL = 1
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_FILLING_FOK = 0
    ORDER_FILLING_IOC = 1
    ORDER_FILLING_RETURN = 2
    ORDER_TIME_GTC = 0
    TRADE_ACTION_DEAL = 1
    TRADE_ACTION_SLTP = 6
    TRADE_RETCODE_DONE = 10009
    TRADE_RETCODE_DONE_PARTIAL = 10010

    def __init__(self, account_mode=0, partial_close=False):
        self.account = SimpleNamespace(
            trade_mode=account_mode, login=123456, server="Test-Demo", company="Test Broker",
            name="Test", currency="USD", leverage=100, balance=5000.0, equity=5000.0,
            margin=0.0, margin_free=5000.0, margin_level=0.0, profit=0.0, trade_allowed=True,
        )
        self.terminal = SimpleNamespace(name="Test Terminal", build=1)
        self.position = SimpleNamespace(
            ticket=7001, symbol="XAUUSD", type=self.POSITION_TYPE_BUY, volume=0.10,
            price_open=4000.0, price_current=4001.0, sl=3990.0, tp=4020.0,
            profit=10.0, time=1762100000, magic=20261101, comment="AURUM",
        ) if partial_close else None
        self.partial_close = partial_close
        self.order_calls = []
        self.order_result_code = self.TRADE_RETCODE_DONE

    def initialize(self, **kwargs):
        return True

    def terminal_info(self):
        return self.terminal

    def account_info(self):
        return self.account

    def last_error(self):
        return (0, "ok")

    def symbol_info(self, symbol):
        return SimpleNamespace(
            visible=True, volume_step=0.01, volume_min=0.01, volume_max=100.0,
            digits=2, point=0.01, trade_stops_level=0, filling_mode=2,
        )

    def symbol_info_tick(self, symbol):
        return SimpleNamespace(bid=4000.0, ask=4000.2, time=1762100000)

    def positions_get(self, ticket=None, symbol=None):
        if self.position is None:
            return []
        if ticket is not None and int(ticket) != int(self.position.ticket):
            return []
        if symbol is not None and symbol != self.position.symbol:
            return []
        return [self.position]

    def order_send(self, request):
        self.order_calls.append(dict(request))
        if "position" in request and self.partial_close:
            self.position.volume = max(0.0, self.position.volume - 0.05)
            retcode = self.TRADE_RETCODE_DONE_PARTIAL
        else:
            retcode = self.order_result_code
        return SimpleNamespace(
            retcode=retcode, order=9001, deal=9002, volume=request.get("volume", 0.10),
            price=request.get("price", 4000.2), comment="filled",
        )


class MT5BridgeTests(unittest.TestCase):
    def make_connector(self, fake, allow_live=False):
        patcher = patch("bridge.mt5_bridge.mt5", fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        connector = MT5Connector(BridgeConfig(token="test-bridge-token-0123456789", allow_live=allow_live))
        connector.initialized = True
        return connector

    def test_real_orders_require_both_bridge_flag_and_dashboard_header(self):
        fake = FakeMT5(account_mode=FakeMT5.ACCOUNT_TRADE_MODE_REAL)
        connector = self.make_connector(fake, allow_live=False)
        body = {
            "symbol": "XAUUSD", "side": "BUY", "lots": 0.10,
            "sl": 3990.0, "tp": 4020.0, "request_id": "req-1",
        }

        with self.assertRaises(LiveDenied):
            connector.order(body, {})
        self.assertEqual(fake.order_calls, [])

        connector.config.allow_live = True
        with self.assertRaises(LiveDenied):
            connector.order(body, {})
        self.assertEqual(fake.order_calls, [])

        result = connector.order(body, {"X-Live-Confirm": "YES"})
        self.assertTrue(result["ok"])
        self.assertEqual(len(fake.order_calls), 1)

    def test_order_request_id_is_idempotent(self):
        fake = FakeMT5()
        connector = self.make_connector(fake)
        body = {
            "symbol": "XAUUSD", "side": "BUY", "lots": 0.10,
            "sl": 3990.0, "tp": 4020.0, "request_id": "same-request",
        }

        first = connector.order(body, {})
        replay = connector.order(body, {})

        self.assertTrue(first["ok"])
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(len(fake.order_calls), 1)

    def test_partial_close_is_not_reported_as_complete(self):
        fake = FakeMT5(partial_close=True)
        connector = self.make_connector(fake)

        with self.assertRaisesRegex(BridgeError, "0.05 lots remain") as caught:
            connector.close({"ticket": "7001"}, {})

        self.assertEqual(caught.exception.status, 502)
        self.assertAlmostEqual(fake.position.volume, 0.05)


if __name__ == "__main__":
    unittest.main()
