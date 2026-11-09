import unittest
from datetime import datetime, timedelta, timezone

from app.models import Candle, Quote
from app.strategy import QuantStrategy


def trend_candles(direction=1, count=280, start=4000.0, step=0.55):
    candles = []
    now = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)
    price = start
    for i in range(count):
        drift = direction * (step + 0.08 * (i % 4))
        open_price = price
        close_price = price + drift
        high = max(open_price, close_price) + 0.28
        low = min(open_price, close_price) - 0.28
        candles.append(Candle(
            time=now + timedelta(minutes=5 * i),
            open=open_price,
            high=high,
            low=low,
            close=close_price,
            volume=1000,
        ))
        price = close_price
    return candles


class StrategyTests(unittest.TestCase):
    def test_requires_warmup(self):
        strategy = QuantStrategy()
        quote = Quote("XAUUSD", 4000.0, 4000.2)
        signal = strategy.evaluate(trend_candles(count=30), quote)
        self.assertEqual(signal.action, "WAIT")
        self.assertIn("Warming up", signal.reasons[0])

    def test_uptrend_produces_long_bracket(self):
        candles = trend_candles(1)
        quote = Quote("XAUUSD", candles[-1].close - 0.12, candles[-1].close + 0.12)
        signal = QuantStrategy().evaluate(candles, quote, min_confidence=0.60)
        self.assertEqual(signal.action, "BUY")
        self.assertGreater(signal.confidence, 0.60)
        self.assertLess(signal.sl, signal.entry)
        self.assertGreater(signal.tp, signal.entry)
        self.assertTrue(signal.reasons)

    def test_downtrend_produces_short_bracket(self):
        candles = trend_candles(-1)
        quote = Quote("XAUUSD", candles[-1].close - 0.12, candles[-1].close + 0.12)
        signal = QuantStrategy().evaluate(candles, quote, min_confidence=0.60)
        self.assertEqual(signal.action, "SELL")
        self.assertGreater(signal.sl, signal.entry)
        self.assertLess(signal.tp, signal.entry)

    def test_ai_overlay_can_veto_but_not_create(self):
        candles = trend_candles(1)
        quote = Quote("XAUUSD", candles[-1].close - 0.12, candles[-1].close + 0.12)
        strategy = QuantStrategy()
        signal = strategy.evaluate(candles, quote, min_confidence=0.60)
        self.assertEqual(signal.action, "BUY")
        review = {
            "used": True,
            "bias": "bearish",
            "bias_score": -1.0,
            "confidence": 1.0,
            "rationale": "Momentum exhaustion risk.",
            "risks": ["Chasing an extended move."],
            "model": "test",
        }
        vetoed = strategy.apply_ai(signal, review, 0.64)
        self.assertEqual(vetoed.action, "WAIT")
        self.assertFalse(vetoed.sl)

    def test_higher_confidence_threshold_blocks_weak_setup(self):
        candles = trend_candles(1)
        quote = Quote("XAUUSD", candles[-1].close - 0.12, candles[-1].close + 0.12)
        signal = QuantStrategy().evaluate(candles, quote, min_confidence=0.995)
        self.assertEqual(signal.action, "WAIT")


if __name__ == "__main__":
    unittest.main()
