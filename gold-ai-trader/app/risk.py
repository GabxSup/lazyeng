"""Deterministic risk governor. Every order must pass every check."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from .models import Account, Position, Quote, RiskAssessment, RiskCheck, Signal
from .util import clamp, money, parse_iso, utcnow

CONTRACT_SIZE = 100.0  # XAUUSD: 100 troy ounces per standard lot
COMMISSION_PER_LOT = 7.0  # round-turn estimate for paper accounting
LOT_STEP = 0.01
MIN_LOT = 0.01


@dataclass
class RiskContext:
    account: Account
    quote: Quote
    positions: List[Position]
    settings: Dict[str, Any]
    day_start_equity: float
    peak_equity: float
    last_closed_at: Optional[datetime] = None
    now: Optional[datetime] = None
    kill_switch: bool = False
    connected: bool = True
    live: bool = False
    armed: bool = False
    signal_age_seconds: Optional[float] = None

    @property
    def moment(self) -> datetime:
        return self.now or utcnow()


class RiskGovernor:
    def __init__(self, symbol: str = "XAUUSD") -> None:
        self.symbol = symbol

    @staticmethod
    def market_open(now: Optional[datetime] = None) -> bool:
        moment = now or utcnow()
        weekday = moment.weekday()
        hour = moment.hour + moment.minute / 60.0
        if weekday == 5:
            return False
        if weekday == 4 and hour >= 21.0:
            return False
        if weekday == 6 and hour < 22.0:
            return False
        return True

    @staticmethod
    def in_session(now: datetime, start_hour: float, end_hour: float) -> bool:
        hour = now.hour + now.minute / 60.0
        if start_hour < end_hour:
            return start_hour <= hour < end_hour
        return hour >= start_hour or hour < end_hour

    def assess(self, signal: Signal, ctx: RiskContext) -> RiskAssessment:
        settings = ctx.settings
        now = ctx.moment
        checks: List[RiskCheck] = []

        def add(key: str, label: str, passed: bool, detail: str) -> None:
            checks.append(RiskCheck(key=key, label=label, passed=bool(passed), detail=detail))

        add("kill_switch", "Halt switch", not ctx.kill_switch,
            "Halt is active." if ctx.kill_switch else "No halt is active.")
        add("connection", "Broker link", ctx.connected,
            "Data/execution channel is online." if ctx.connected else "Broker or feed is offline.")
        is_open = self.market_open(now)
        add("market_hours", "Market hours", is_open,
            "XAUUSD market is open." if is_open else "Market is closed (weekend / Friday close).")
        start = float(settings.get("session_start_utc", 0))
        end = float(settings.get("session_end_utc", 24))
        in_session = self.in_session(now, start, end)
        add("session", "Session", in_session,
            f"{start:g}:00–{end:g}:00 UTC entry window." if in_session else f"Outside {start:g}:00–{end:g}:00 UTC window.")

        if ctx.live and not ctx.armed:
            add("live_arm", "Live arm", False, "Live execution requires the explicit arm phrase.")
        else:
            add("live_arm", "Live arm", True, "Paper mode." if not ctx.live else "Live session is armed.")

        spread_limit = float(settings.get("max_spread", 0.45))
        spread_ok = 0 <= ctx.quote.spread <= spread_limit
        add("spread", "Spread", spread_ok,
            f"Spread {ctx.quote.spread:.2f} vs {spread_limit:.2f} max.")

        max_positions = int(settings.get("max_positions", 1))
        slots_ok = len(ctx.positions) < max_positions
        add("slots", "Position slot", slots_ok,
            f"{len(ctx.positions)}/{max_positions} open positions.")

        directional = signal.action in {"BUY", "SELL"}
        min_conf = float(settings.get("min_confidence", 0.64))
        setup_ok = directional and signal.confidence >= min_conf
        detail = (
            f"{signal.action} at {signal.confidence * 100:.0f}% confidence."
            if directional
            else f"Signal is {signal.action}; no order will be sent."
        )
        add("setup", "Setup", setup_ok, detail)
        fresh = ctx.signal_age_seconds is None or ctx.signal_age_seconds <= 150
        add("freshness", "Signal age", fresh,
            "Signal is fresh." if fresh else f"Last scan was {ctx.signal_age_seconds or 0:.0f}s ago; freshness window is 150s.")

        equity = max(0.001, ctx.account.equity)
        day_start = ctx.day_start_equity if ctx.day_start_equity > 0 else equity
        daily_loss_pct = max(0.0, (day_start - equity) / day_start * 100.0)
        daily_limit = float(settings.get("max_daily_loss_pct", 3.0))
        daily_ok = daily_loss_pct < daily_limit
        add("daily_loss", "Daily loss", daily_ok,
            f"Down {daily_loss_pct:.2f}% today vs {daily_limit:.2f}% limit."
            if daily_loss_pct > 0 else f"Daily loss {daily_loss_pct:.2f}% vs {daily_limit:.2f}% limit.")

        peak = max(ctx.peak_equity, equity)
        dd_pct = max(0.0, (peak - equity) / peak * 100.0) if peak > 0 else 0.0
        dd_limit = float(settings.get("max_drawdown_pct", 10.0))
        dd_ok = dd_pct < dd_limit
        add("drawdown", "Drawdown", dd_ok,
            f"Drawdown {dd_pct:.2f}% vs {dd_limit:.2f}% limit.")

        cooldown_min = float(settings.get("cooldown_minutes", 15))
        cooldown_ok = True
        cooldown_detail = "No recent trade."
        if ctx.last_closed_at is not None:
            elapsed = (now - ctx.last_closed_at).total_seconds() / 60.0
            cooldown_ok = elapsed >= cooldown_min
            cooldown_detail = f"{elapsed:.1f} min since last close; {cooldown_min:g} min cooldown."
            if not cooldown_ok:
                cooldown_detail = f"Cooling down: {elapsed:.1f}/{cooldown_min:g} min elapsed."
        add("cooldown", "Cooldown", cooldown_ok, cooldown_detail)

        entry = 0.0
        sl = signal.sl
        tp = signal.tp
        stop_distance = 0.0
        lots = 0.0
        risk_amount = 0.0
        sizing_ok = False
        sizing_detail = "No sized setup."
        rr_ok = False
        rr_detail = "No bracket."
        if directional and signal.sl and signal.tp:
            entry = ctx.quote.ask if signal.action == "BUY" else ctx.quote.bid
            stop_distance = abs(entry - sl)
            risk_budget = equity * float(settings.get("risk_per_trade_pct", 0.5)) / 100.0
            if stop_distance > 0.01:
                raw_lots = risk_budget / (stop_distance * CONTRACT_SIZE)
                lots = round(max(0.0, raw_lots // LOT_STEP * LOT_STEP), 2)
                lots = min(lots, float(settings.get("max_lots", 1.0)))
                code_risk = lots * stop_distance * CONTRACT_SIZE
                if lots >= MIN_LOT:
                    lots = max(MIN_LOT, lots)
                    code_risk = lots * stop_distance * CONTRACT_SIZE
                    sizing_ok = code_risk <= risk_budget * 1.35 + 0.5
                    sizing_detail = (
                        f"{lots:.2f} lots risks {money(code_risk)} of {money(risk_budget)} budget."
                        if sizing_ok else
                        f"Broker minimum {MIN_LOT:.2f} lot risks {money(code_risk)}, above {money(risk_budget)} budget."
                    )
                    margin_needed = lots * CONTRACT_SIZE * entry / max(1, ctx.account.leverage)
                    margin_ok = ctx.account.free_margin <= 0 or ctx.account.free_margin >= margin_needed
                    if not margin_ok:
                        sizing_ok = False
                        sizing_detail += f" Insufficient free margin ({money(ctx.account.free_margin)} vs {money(margin_needed)})."
                    risk_amount = code_risk
                else:
                    sizing_detail = "Risk budget is below the broker minimum lot."
            else:
                sizing_detail = "Stop distance is too small."
            achieved_rr = abs(tp - entry) / stop_distance if stop_distance > 0 else 0.0
            target_rr = float(settings.get("rr_ratio", 1.8))
            rr_ok = achieved_rr >= target_rr * 0.85
            rr_detail = f"Bracket offers {achieved_rr:.2f}R vs {target_rr:.1f}R target."
        add("sizing", "Position size", sizing_ok, sizing_detail)
        add("reward_risk", "Reward / risk", rr_ok, rr_detail)

        allowed = all(check.passed for check in checks)
        return RiskAssessment(
            allowed=allowed,
            checks=checks,
            lots=lots if allowed else 0.0,
            risk_amount=risk_amount,
            sl=sl,
            tp=tp,
            entry=entry,
        )

    def sizing_preview(self, account: Account, price: float, risk_pct: float, stop_distance: float, max_lots: float) -> Dict[str, float]:
        budget = account.equity * risk_pct / 100.0
        raw = budget / max(0.01, stop_distance * CONTRACT_SIZE)
        lots = max(MIN_LOT, min(max_lots, round(raw // LOT_STEP * LOT_STEP, 2)))
        return {
            "budget": round(budget, 2),
            "lots": round(lots, 2),
            "actual_risk": round(lots * stop_distance * CONTRACT_SIZE, 2),
        }


def open_risk(positions: List[Position], entry_fallback: float = 0.0) -> float:
    total = 0.0
    for position in positions:
        distance = abs(position.entry_price - position.sl) if position.sl else 0.0
        total += distance * position.lots * CONTRACT_SIZE
    return total
