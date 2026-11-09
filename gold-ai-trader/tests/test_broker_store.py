import tempfile
import unittest
from pathlib import Path

from app.broker import PaperBroker
from app.config import Config
from app.db import Store
from app.settings import SettingsError


class BrokerStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "test.db", 10000)
        self.config = Config(data_dir=Path(self.tmp.name), symbol="XAUUSD", paper_balance=10000)
        self.broker = PaperBroker(self.store, self.config)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_paper_order_round_trip(self):
        quote = self.broker.quote()
        result = self.broker.open_market(
            signal_id="sig-1",
            side="BUY",
            lots=0.10,
            sl=round(quote.bid - 5, 2),
            tp=round(quote.ask + 10, 2),
            confidence=0.8,
            reason="unit test",
        )
        self.assertTrue(result.ok)
        positions = self.broker.positions()
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0].ticket, result.ticket)
        open_trade = self.store.get_open_trades(mode="paper")[0]
        self.assertIsNotNone(open_trade.get("initial_sl"))
        self.assertEqual(open_trade.get("tp_extensions"), 0)
        self.assertTrue(self.store.update_protection(result.ticket, positions[0].entry_price, positions[0].tp, 1))
        updated = self.store.get_trade(result.ticket)
        self.assertEqual(updated["sl"], positions[0].entry_price)
        self.assertEqual(updated["tp_extensions"], 1)
        account = self.broker.account()
        self.assertGreaterEqual(account.margin, 0)
        closed = self.broker.close_position(result.ticket)
        self.assertTrue(closed["ok"])
        self.assertEqual(len(self.broker.positions()), 0)
        self.assertNotEqual(self.store.get_paper_account()["balance"], 10000)

    def test_settings_round_trip_and_validation(self):
        updated = self.store.update_settings({"risk_per_trade_pct": 0.75, "min_confidence": 0.7})
        self.assertEqual(updated["risk_per_trade_pct"], 0.75)
        self.assertEqual(updated["min_confidence"], 0.7)
        with self.assertRaises(SettingsError):
            self.store.update_settings({"risk_per_trade_pct": 99})
        with self.assertRaises(SettingsError):
            self.store.update_settings({"unknown": 1})

    def test_paper_reset(self):
        quote = self.broker.quote()
        self.broker.open_market("s", "SELL", 0.1, round(quote.ask + 5, 2), round(quote.bid - 10, 2), 0.8, "test")
        self.broker.close_all()
        self.broker.reset(12000)
        account = self.store.get_paper_account()
        self.assertEqual(account["balance"], 12000)
        self.assertEqual(len(self.store.get_open_trades("paper")), 0)


if __name__ == "__main__":
    unittest.main()
