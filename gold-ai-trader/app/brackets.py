"""Automatic protection management for open positions.

The planner is deliberately pure: it takes a position snapshot, current price
and ATR and returns a proposed bracket update. The engine is responsible for
throttling writes and sending the change to the broker.

Rules:
* never move a stop backwards;
* only start protecting profit once the trade is ``breakeven_at_r`` in profit;
* trail the stop at ``trailing_atr_mult`` ATR from current price, floored at
  break-even once the trail is active;
* extend take-profit only when price is near the target, the stop is already at
  or beyond break-even, momentum agrees, and a finite extension budget remains;
* never tighten/rotate take-profit back toward price.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .util import clamp


@dataclass
class BracketUpdate:
    sl: float
    tp: float
    sl_changed: bool
    tp_changed: bool
    extensions: int
    r_multiple: float
    reasons: List[str]

    @property
    def changed(self) -> bool:
        return self.sl_changed or self.tp_changed


def _round_price(value: float) -> float:
    return round(float(value), 2)


def plan_bracket_update(
    *,
    side: str,
    entry: float,
    initial_sl: Optional[float],
    sl: float,
    tp: float,
    price: float,
    atr: float,
    settings: Dict[str, Any],
    score: float = 0.0,
    extensions: int = 0,
) -> Optional[BracketUpdate]:
    if not bool(settings.get("auto_adjust_brackets", True)):
        return None
    side = str(side).upper()
    if side not in {"BUY", "SELL"}:
        return None
    entry = float(entry)
    price = float(price)
    atr = float(atr)
    sl = float(sl or 0.0)
    tp = float(tp or 0.0)
    if entry <= 0 or price <= 0 or atr <= 0 or sl <= 0:
        return None
    direction = 1.0 if side == "BUY" else -1.0
    initial = float(initial_sl or sl)
    risk = abs(entry - initial)
    if risk <= 0.01:
        return None
    progress = (price - entry) * direction
    r_multiple = progress / risk
    breakeven_trigger = float(settings.get("breakeven_at_r", 1.0))
    trailing_mult = max(0.1, float(settings.get("trailing_atr_mult", 1.2)))
    min_move = max(0.01, atr * 0.05)

    new_sl = sl
    new_tp = tp
    reasons: List[str] = []

    # Trail once the position has earned the configured number of R.
    if r_multiple >= breakeven_trigger:
        raw_trail = price - direction * trailing_mult * atr
        if direction > 0:
            candidate = max(entry, sl, raw_trail)
            candidate = min(candidate, price - min_move)
            if candidate > sl + min_move:
                new_sl = _round_price(candidate)
                reasons.append(f"stop trailed to {new_sl:.2f}")
        else:
            candidate = min(entry, sl, raw_trail)
            candidate = max(candidate, price + min_move)
            if candidate < sl - min_move:
                new_sl = _round_price(candidate)
                reasons.append(f"stop trailed to {new_sl:.2f}")

    # Extend the target only when the trade is effectively risk-free and the
    # trend still has momentum. This avoids loosening a target in a weakening
    # move and never moves a target closer.
    extend_enabled = bool(settings.get("extend_tp_enabled", True))
    max_extensions = int(settings.get("max_tp_extensions", 2))
    extension_atr = max(0.1, float(settings.get("tp_extension_atr", 0.8)))
    if (
        extend_enabled
        and tp > 0
        and extensions < max_extensions
        and (new_sl - entry) * direction >= -1e-9
    ):
        remaining = (tp - price) * direction
        momentum_agrees = score * direction >= 0.15
        if 0 <= remaining <= atr * 0.30 and momentum_agrees:
            candidate_tp = tp + direction * extension_atr * atr
            if direction > 0 and candidate_tp > tp + min_move:
                new_tp = _round_price(candidate_tp)
                extensions += 1
                reasons.append(f"target extended to {new_tp:.2f}")
            elif direction < 0 and candidate_tp < tp - min_move:
                new_tp = _round_price(candidate_tp)
                extensions += 1
                reasons.append(f"target extended to {new_tp:.2f}")

    sl_changed = abs(new_sl - sl) >= 0.005
    tp_changed = abs(new_tp - tp) >= 0.005
    if not sl_changed and not tp_changed:
        return None
    return BracketUpdate(
        sl=new_sl,
        tp=new_tp,
        sl_changed=sl_changed,
        tp_changed=tp_changed,
        extensions=extensions,
        r_multiple=round(r_multiple, 2),
        reasons=reasons,
    )
