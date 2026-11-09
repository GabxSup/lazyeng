import unittest
from datetime import datetime, timezone

from app.models import Account, Position, Quote, Signal
from app.risk import RiskContext, RiskGovernor
from app.settings import DEFAULT_SETTINGS


def make_signal(action="BUY", confidence=0.82, entry=4000.25, sl=3995.0, tp=4010.0):
    return Signal(
        action=action,
        confidence=confidence,
        raw_score=0.7 if action == "BUY" else -0.7,
        regime="Trend expansion",
        entry=entry,
        sl=sl,
        tp=tp,
        reasons=["test"],
        features={},
        created_at=datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc),
        id="test-signal",
    )


def make_context(**overrides):
    account = Account(balance=10000, equity=10000, free_margin=10000, leverage=100)
    defaults = dict(
        account=account,
        quote=Quote("XAUUSD", 4000.0, 4000.25),
        positions=[],
        settings=dict(DEFAULT_SETTINGS),
        day_start_equity=10000.0,
        peak_equity=10000.0,
        last_closed_at=None,
        now=datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc),
        kill_switch=False,
        connected=True,
        live=False,
        armed=False,
        signal_age_seconds=10,
    )
    defaults.update(overrides)
    return RiskContext(**defaults)


class RiskTests(unittest.TestCase):
    def setUp(self):
        self.governor = RiskGovernor()

    def test_clean_setup_is_approved_and_sized(self):
        result = self.governor.assess(make_signal(), make_context())
        self.assertTrue(result.allowed, result.blockers)
        self.assertGreater(result.lots, 0)
        self.assertLessEqual(result.risk_amount, 50 * 1.35 + 0.5)

    def test_kill_switch_blocks(self):
        result = self.governor.assess(make_signal(), make_context(kill_switch=True))
        self.assertFalse(result.allowed)
        self.assertIn("Halt switch", result.blockers)

    def test_daily_loss_blocks(self):
        account = Account(balance=9600, equity=9600, free_margin=9600)
        result = self.governor.assess(make_signal(), make_context(account=account, day_start_equity=10000))
        self.assertFalse(result.allowed)
        self.assertIn("Daily loss", result.blockers)

    def test_drawdown_blocks(self):
        account = Account(balance=9800, equity=9800, free_margin=9800)
        result = self.governor.assess(make_signal(), make_context(account=account, day_start_equity=9800, peak_equity=11000))
        self.assertFalse(result.allowed)
        self.assertIn("Drawdown", result.blockers)

    def test_wide_spread_blocks(self):
        result = self.governor.assess(make_signal(), make_context(quote=Quote("XAUUSD", 4000.0, 4000.6)))
        self.assertFalse(result.allowed)
        self.assertIn("Spread", result.blockers)

    def test_session_and_weekend(self):
        saturday = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)
        result = self.governor.assess(make_signal(), make_context(now=saturday))
        self.assertFalse(result.allowed)
        self.assertIn("Market hours", result.blockers)

    def test_cooldown_blocks(self):
        last = datetime(2026, 11, 2, 9, 55, tzinfo=timezone.utc)
        now = datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc)
        result = self.governor.assess(make_signal(), make_context(now=now, last_closed_at=last))
        self.assertFalse(result.allowed)
        self.assertIn("Cooldown", result.blockers)

    def test_max_positions_blocks(self):
        position = Position("1", "XAUUSD", "BUY", 0.1, 4000, 4001, sl=3990, tp=4020, pnl=10)
        result = self.governor.assess(make_signal(), make_context(positions=[position]))
        self.assertFalse(result.allowed)
        self.assertIn("Position slot", result.blockers)

    def test_live_requires_arming(self):
        result = self.governor.assess(make_signal(), make_context(live=True, armed=False))
        self.assertFalse(result.allowed)
        self.assertIn("Live arm", result.blockers)

    def test_wait_signal_is_not_executable(self):
        result = self.governor.assess(make_signal(action="WAIT", confidence=0.5, sl=0, tp=0), make_context())
        self.assertFalse(result.allowed)
        self.assertIn("Setup", result.blockers)


if __name__ == "__main__":
    unittest.main()
