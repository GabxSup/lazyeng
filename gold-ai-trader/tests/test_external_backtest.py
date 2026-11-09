import unittest

from app.backtest import run_backtest
from app.market import synthetic_series


class ExternalBacktestTests(unittest.TestCase):
    def test_external_candles_and_costs_are_used(self):
        candles = synthetic_series(600, seed=31415, base_price=4000.0)
        result = run_backtest(
            start_balance=10000,
            candles=candles,
            data_source="mt5_csv",
            timeframe="M5",
            commission_per_lot=7.0,
            spread=0.20,
            slippage=0.02,
            source_label="Imported test history",
        )
        self.assertEqual(result["data_source"], "mt5_csv")
        self.assertEqual(result["label"], "Imported test history")
        self.assertEqual(result["request"]["bars"], 600)
        self.assertIsNone(result["request"]["seed"])
        self.assertEqual(result["request"]["costs"]["commission_per_lot"], 7.0)
        self.assertEqual(result["request"]["costs"]["spread"], 0.20)
        self.assertIn("imported mt5 history", result["disclaimer"].lower())
        self.assertIn("summary", result)

    def test_external_data_needs_warmup(self):
        candles = synthetic_series(100, seed=1)
        with self.assertRaises(ValueError):
            run_backtest(candles=candles)


if __name__ == "__main__":
    unittest.main()
