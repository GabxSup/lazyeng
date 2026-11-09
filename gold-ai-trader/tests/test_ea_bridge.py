"""Protocol tests for the MT4 Expert Advisor polling bridge."""
import threading
import time
import unittest
from datetime import datetime, timezone

from app.config import Config
from app.ea import EABroker, EAProtocolError


TOKEN = "test-ea-token-0123456789"


def snapshot(quote="QUOTE|XAUUSD|4000.10|4000.30"):
    now = int(datetime.now(timezone.utc).timestamp())
    opened = now - 7200
    closed = now - 3600
    return "\r\n".join([
        "AURUM|1",
        f"SERVERTIME|{now}",
        "SYMBOL|XAUUSD",
        "ACCOUNT|123456|10000|10050|100|9950|100|USD|Demo Trader|Broker-Demo|Test Broker|demo",
        quote,
        f"POS|8101|0|0.10|4000.00|4001.00|3990.00|4020.00|10.00|{opened}|20261101|AURUM sample",
        f"CLOSED|8100|BUY|0.10|3990.00|4000.00|100.00|-1.00|0.00|{opened}|{closed}",
        "",
    ])


class EABridgeProtocolTests(unittest.TestCase):
    def setUp(self):
        self.broker = EABroker(Config(ea_token=TOKEN, symbol="XAUUSD"))

    def test_parses_actual_mt4_symbol_prefixed_snapshot(self):
        self.broker.handle_poll(snapshot())
        self.broker.connect()

        account = self.broker.account()
        quote = self.broker.quote()
        positions = self.broker.positions()
        self.assertEqual(account.login, 123456)
        self.assertEqual(account.account_type, "demo")
        self.assertEqual(quote.symbol, "XAUUSD")
        self.assertAlmostEqual(quote.bid, 4000.10)
        self.assertAlmostEqual(quote.ask, 4000.30)
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0].side, "BUY")
        self.assertEqual(positions[0].ticket, "8101")
        self.assertEqual(len(self.broker.history()), 1)

    def test_accepts_legacy_quote_without_symbol(self):
        self.broker.handle_poll(snapshot("QUOTE|4000.10|4000.30"))
        self.assertAlmostEqual(self.broker.quote().ask, 4000.30)

    def test_rejects_quote_for_a_different_symbol(self):
        with self.assertRaisesRegex(EAProtocolError, "quote is for EURUSD"):
            self.broker.handle_poll(snapshot("QUOTE|EURUSD|1.1000|1.1002"))

    def test_command_is_delivered_and_result_completes_waiting_call(self):
        self.broker.handle_poll(snapshot())
        self.broker.connect()
        self.broker.armed = True
        result = []
        errors = []

        def open_trade():
            try:
                result.append(self.broker.open_market("signal-1", "BUY", 0.10, 3990.0, 4020.0, 0.8, "test"))
            except Exception as exc:  # surfaced in the main test thread
                errors.append(exc)

        worker = threading.Thread(target=open_trade, daemon=True)
        worker.start()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            with self.broker._cond:
                if self.broker._out:
                    break
            time.sleep(0.01)
        response = self.broker.handle_poll(snapshot())
        self.assertIn("|OPEN|BUY|", response)
        command_id = response.split("|", 2)[1]
        reply = snapshot() + f"RESULT|{command_id}|1|9100|4000.20|0.10|0|filled\r\n"
        self.broker.handle_poll(reply)
        worker.join(timeout=2.0)

        self.assertFalse(worker.is_alive(), "EA command call did not complete")
        self.assertEqual(errors, [])
        self.assertEqual(result[0].ticket, "9100")
        self.assertAlmostEqual(result[0].lots, 0.10)


if __name__ == "__main__":
    unittest.main()
