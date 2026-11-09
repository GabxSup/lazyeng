import unittest
from datetime import datetime, timedelta, timezone

from app.indicators import adx, atr, bollinger, ema, last_valid, macd, rsi, sma
from app.market import synthetic_series
from app.util import resample


class IndicatorTests(unittest.TestCase):
    def test_ema_linear_series(self):
        out = ema([1, 2, 3, 4, 5], 3)
        self.assertIsNone(out[0])
        self.assertIsNone(out[1])
        self.assertAlmostEqual(out[2], 2.0)
        self.assertAlmostEqual(out[3], 3.0)
        self.assertAlmostEqual(out[4], 4.0)

    def test_sma_length_and_warmup(self):
        out = sma([1, 2, 3, 4], 3)
        self.assertEqual(len(out), 4)
        self.assertEqual(out[:2], [None, None])
        self.assertAlmostEqual(out[2], 2.0)
        self.assertAlmostEqual(out[3], 3.0)

    def test_rsi_strictly_rising_is_high(self):
        values = list(range(1, 40))
        out = rsi(values, 14)
        self.assertIsNone(out[13])
        self.assertGreater(out[-1], 95)

    def test_atr_constant_range(self):
        highs = [10.0] * 30
        lows = [9.0] * 30
        closes = [9.5] * 30
        out = atr(highs, lows, closes, 14)
        self.assertAlmostEqual(out[-1], 1.0)

    def test_bollinger_flat_series(self):
        middle, upper, lower = bollinger([5.0] * 30, 20, 2.0)
        self.assertAlmostEqual(middle[-1], 5.0)
        self.assertAlmostEqual(upper[-1], 5.0)
        self.assertAlmostEqual(lower[-1], 5.0)

    def test_macd_outputs_complete_series(self):
        values = [100 + i * 0.2 for i in range(60)]
        line, signal, hist = macd(values)
        self.assertEqual(len(line), 60)
        self.assertEqual(len(signal), 60)
        self.assertEqual(len(hist), 60)
        self.assertIsNotNone(line[-1])
        self.assertIsNotNone(signal[-1])
        self.assertIsNotNone(hist[-1])

    def test_adx_trend_has_directional_edge(self):
        candles = synthetic_series(400, seed=1234)
        highs = [c.high for c in candles]
        lows = [c.low for c in candles]
        closes = [c.close for c in candles]
        adx_line, plus_di, minus_di = adx(highs, lows, closes, 14)
        self.assertIsNotNone(adx_line[-1])
        self.assertGreater(adx_line[-1], 0)
        self.assertNotEqual(plus_di[-1], minus_di[-1])

    def test_ema_no_lookahead(self):
        values = [100 + (i % 7) * 0.3 for i in range(80)]
        full = ema(values, 10)
        first_half = ema(values[:50], 10)
        self.assertEqual(full[:50], first_half)
        self.assertEqual(last_valid(full), full[-1])

    def test_resample_m5_to_m15(self):
        candles = [
            {"time": 0, "open": 10, "high": 11, "low": 9, "close": 10.5, "volume": 1},
            {"time": 300, "open": 10.5, "high": 12, "low": 10, "close": 11.5, "volume": 2},
            {"time": 600, "open": 11.5, "high": 12.5, "low": 11, "close": 12, "volume": 3},
            {"time": 900, "open": 12, "high": 13, "low": 11.5, "close": 12.5, "volume": 4},
        ]
        result = resample(candles, 900)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["open"], 10)
        self.assertEqual(result[0]["high"], 12.5)
        self.assertEqual(result[0]["low"], 9)
        self.assertEqual(result[0]["close"], 12)
        self.assertEqual(result[0]["volume"], 6)


if __name__ == "__main__":
    unittest.main()
