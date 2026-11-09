import unittest

from app.auth import AuthManager
from app.backtest import run_backtest


class BacktestTests(unittest.TestCase):
    def test_backtest_shape_and_disclaimer(self):
        result = run_backtest(days=7, start_balance=10000)
        self.assertTrue(result["ok"])
        self.assertTrue(result["simulated"])
        self.assertIn("summary", result)
        self.assertIn("trades", result)
        self.assertIn("equity", result)
        self.assertIn("management", result)
        self.assertIn("bracket_adjustments", result["summary"])
        self.assertTrue(result["management"]["dynamic_brackets"])
        self.assertLessEqual(len(result["equity"]), 240)
        self.assertIn("disclaimer", result)
        # Fewer than ten daily observations: Sharpe should not be fabricated.
        self.assertIsNone(result["summary"]["sharpe"])

    def test_backtest_days_are_clamped(self):
        result = run_backtest(days=500, start_balance=5000)
        self.assertEqual(result["request"]["days"], 60)


class AuthTests(unittest.TestCase):
    def test_signed_cookie_round_trip_and_tamper(self):
        auth = AuthManager("correct horse", "a" * 64)
        token = auth.issue()
        self.assertTrue(auth.valid(token))
        self.assertFalse(auth.valid(token + "x"))
        self.assertFalse(auth.valid(""))
        self.assertTrue(auth.check_password("correct horse"))
        self.assertFalse(auth.check_password("wrong"))

    def test_rate_limiter(self):
        auth = AuthManager("pw", "b" * 64)
        for _ in range(6):
            allowed, _retry = auth.login_allowed("1.2.3.4")
            self.assertTrue(allowed)
            auth.record_failure("1.2.3.4")
        allowed, retry = auth.login_allowed("1.2.3.4")
        self.assertFalse(allowed)
        self.assertGreater(retry, 0)


if __name__ == "__main__":
    unittest.main()
