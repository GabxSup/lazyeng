import unittest

from app.brackets import plan_bracket_update
from app.settings import DEFAULT_SETTINGS


def base_settings(**overrides):
    settings = dict(DEFAULT_SETTINGS)
    settings.update(overrides)
    return settings


class BracketPlannerTests(unittest.TestCase):
    def test_no_adjustment_before_breakeven_trigger(self):
        plan = plan_bracket_update(
            side="BUY", entry=4000, initial_sl=3995, sl=3995, tp=4010,
            price=4003.0, atr=1.0, settings=base_settings(), score=0.8,
        )
        self.assertIsNone(plan)

    def test_long_stop_trails_and_locks_profit(self):
        plan = plan_bracket_update(
            side="BUY", entry=4000, initial_sl=3995, sl=3995, tp=4010,
            price=4008.0, atr=1.0, settings=base_settings(), score=0.8,
        )
        self.assertIsNotNone(plan)
        self.assertTrue(plan.sl_changed)
        self.assertGreater(plan.sl, 4000)
        self.assertLess(plan.sl, 4008)
        self.assertEqual(plan.tp, 4010)

    def test_long_take_profit_extends_only_near_target_with_momentum(self):
        near = plan_bracket_update(
            side="BUY", entry=4000, initial_sl=3995, sl=3995, tp=4010,
            price=4009.8, atr=1.0, settings=base_settings(), score=0.8,
        )
        self.assertIsNotNone(near)
        self.assertTrue(near.tp_changed)
        self.assertGreater(near.tp, 4010)
        self.assertEqual(near.extensions, 1)

        weak = plan_bracket_update(
            side="BUY", entry=4000, initial_sl=3995, sl=3995, tp=4010,
            price=4009.8, atr=1.0, settings=base_settings(), score=-0.4,
        )
        self.assertFalse(weak and weak.tp_changed)

    def test_short_symmetry(self):
        plan = plan_bracket_update(
            side="SELL", entry=4000, initial_sl=4005, sl=4005, tp=3990,
            price=3991.5, atr=1.0, settings=base_settings(), score=-0.8,
        )
        self.assertIsNotNone(plan)
        self.assertLess(plan.sl, 4000)
        self.assertLess(plan.sl, 3995)
        self.assertGreater(plan.sl, 3991.5)
        self.assertLessEqual(plan.tp, 3990)

    def test_stop_never_moves_backwards(self):
        plan = plan_bracket_update(
            side="BUY", entry=4000, initial_sl=3995, sl=4007.0, tp=4010,
            price=4008.0, atr=1.0, settings=base_settings(), score=0.8,
        )
        self.assertIsNone(plan)

    def test_extension_budget_and_switch(self):
        maxed = plan_bracket_update(
            side="BUY", entry=4000, initial_sl=3995, sl=4006, tp=4010,
            price=4009.8, atr=1.0, settings=base_settings(max_tp_extensions=2), score=0.8,
            extensions=2,
        )
        self.assertFalse(maxed and maxed.tp_changed)
        disabled = plan_bracket_update(
            side="BUY", entry=4000, initial_sl=3995, sl=4006, tp=4010,
            price=4009.8, atr=1.0, settings=base_settings(extend_tp_enabled=False), score=0.8,
        )
        self.assertFalse(disabled and disabled.tp_changed)

    def test_master_switch(self):
        self.assertIsNone(plan_bracket_update(
            side="BUY", entry=4000, initial_sl=3995, sl=3995, tp=4010,
            price=4010.0, atr=1.0, settings=base_settings(auto_adjust_brackets=False), score=1.0,
        ))

    def test_invalid_data_is_safe(self):
        self.assertIsNone(plan_bracket_update(
            side="BUY", entry=4000, initial_sl=3995, sl=3995, tp=4010,
            price=4008.0, atr=0.0, settings=base_settings(), score=1.0,
        ))


if __name__ == "__main__":
    unittest.main()
