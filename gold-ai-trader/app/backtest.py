"""Event-driven backtester for the quant core on simulated XAUUSD data.

Fills occur at the next bar open with adverse spread and slippage. If a bar
touches both stop and target, the stop is assumed to fill first. No AI overlay
is used, no forming candle is passed to the strategy, and the result is
explicitly a simulated research estimate -- not a promise of future returns.
"""
from __future__ import annotations

import math
import statistics
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from .brackets import plan_bracket_update
from .indicators import atr as atr_series_fn, ema as ema_series_fn
from .market import synthetic_series
from .models import Candle, Quote
from .risk import CONTRACT_SIZE, LOT_STEP, MIN_LOT
from .settings import DEFAULT_SETTINGS
from .strategy import QuantStrategy
from .util import downsample, iso, timeframe_seconds, utcnow

BARS_PER_DAY = 288
WARMUP = 215
COMMISSION_PER_LOT = 7.0
SLIPPAGE = 0.02


def _round_lots(value: float) -> float:
    return round(max(0.0, value) // LOT_STEP * LOT_STEP, 2)


def run_backtest(days: int = 30, start_balance: float = 10000.0, settings: Optional[Dict[str, Any]] = None,
                 seed: int = 202611, symbol: str = "XAUUSD",
                 candles: Optional[List[Candle]] = None,
                 data_source: str = "synthetic",
                 timeframe: str = "M5",
                 commission_per_lot: float = COMMISSION_PER_LOT,
                 spread: float = 0.25,
                 slippage: float = SLIPPAGE,
                 source_label: Optional[str] = None) -> Dict[str, Any]:
    started = time.monotonic()
    settings = {**DEFAULT_SETTINGS, **(settings or {})}
    synthetic_data = candles is None
    if synthetic_data:
        days = max(1, min(int(days), 60))
        bars_needed = days * BARS_PER_DAY + WARMUP + 10
        candles = synthetic_series(
            bars_needed,
            end_time=utcnow(),
            interval=300,
            seed=seed,
            base_price=4000.0,
        )
        data_source = "synthetic"
    else:
        candles = sorted(candles, key=lambda candle: candle.time)
        if len(candles) < WARMUP + 40:
            raise ValueError(f"At least {WARMUP + 40} candles are required; got {len(candles)}.")
        span_seconds = max(0.0, (candles[-1].time - candles[0].time).total_seconds())
        days = max(1, int(round(span_seconds / 86400.0)))
    timeframe = str(timeframe or "M5").upper()
    bar_seconds = timeframe_seconds(timeframe)
    commission_per_lot = max(0.0, float(commission_per_lot))
    spread = max(0.0, float(spread))
    slippage = max(0.0, float(slippage))
    strategy = QuantStrategy(symbol=symbol)
    highs_all = [c.high for c in candles]
    lows_all = [c.low for c in candles]
    closes_all = [c.close for c in candles]
    atr_series = atr_series_fn(highs_all, lows_all, closes_all, 14)
    ema_fast_series = ema_series_fn(closes_all, 20)
    ema_slow_series = ema_series_fn(closes_all, 50)
    timeframe = "M5"
    min_conf = float(settings["min_confidence"])
    rr = float(settings["rr_ratio"])
    atr_mult = float(settings["atr_stop_mult"])
    risk_pct = float(settings["risk_per_trade_pct"])
    daily_limit = float(settings["max_daily_loss_pct"])
    dd_limit = float(settings["max_drawdown_pct"])
    cooldown_bars = max(0, int(math.ceil(float(settings["cooldown_minutes"]) * 60 / max(1, bar_seconds))))
    max_lots = float(settings["max_lots"])
    session_start = float(settings["session_start_utc"])
    session_end = float(settings["session_end_utc"])
    max_spread = float(settings["max_spread"])

    balance = float(start_balance)
    equity = float(start_balance)
    peak = equity
    max_drawdown = 0.0
    position: Optional[Dict[str, Any]] = None
    closed: List[Dict[str, Any]] = []
    equity_curve: List[Dict[str, Any]] = []
    cooldown_until = 0
    day = None
    day_start_equity = equity
    halted = False
    bars_in_market = 0
    bracket_adjustments = 0
    last_index = len(candles) - 2

    for i in range(WARMUP, last_index):
        candle = candles[i]
        next_candle = candles[i + 1]
        bar_day = candle.time.date()

        if day != bar_day:
            day = bar_day
            day_start_equity = equity

        if position is not None:
            bars_in_market += 1
            exit_price: Optional[float] = None
            exit_reason = ""
            if position["side"] == "BUY":
                if candle.low <= position["sl"]:
                    exit_price, exit_reason = position["sl"], "Stop loss"
                elif candle.high >= position["tp"]:
                    exit_price, exit_reason = position["tp"], "Take profit"
            else:
                if candle.high >= position["sl"]:
                    exit_price, exit_reason = position["sl"], "Stop loss"
                elif candle.low <= position["tp"]:
                    exit_price, exit_reason = position["tp"], "Take profit"
            if exit_price is not None:
                pnl = (exit_price - position["entry"]) * position["lots"] * CONTRACT_SIZE
                if position["side"] == "SELL":
                    pnl *= -1
                fees = commission_per_lot * position["lots"]
                pnl -= fees
                balance += pnl
                equity = balance
                peak = max(peak, equity)
                max_drawdown = max(max_drawdown, (peak - equity) / peak * 100.0 if peak else 0.0)
                trade = dict(position)
                trade.update({
                    "exit": round(exit_price, 2),
                    "closed_at": iso(candle.time),
                    "pnl": round(pnl, 2),
                    "fees": round(fees, 2),
                    "exit_reason": exit_reason,
                    "return_pct": round(pnl / start_balance * 100.0, 3),
                })
                closed.append(trade)
                position = None
                cooldown_until = i + cooldown_bars
            else:
                current = candle.close
                current_atr = atr_series[i]
                fast = ema_fast_series[i]
                slow = ema_slow_series[i]
                if current_atr and fast is not None and slow is not None:
                    momentum_score = 1.0 if fast > slow else -1.0
                    plan = plan_bracket_update(
                        side=position["side"],
                        entry=position["entry"],
                        initial_sl=position["initial_sl"],
                        sl=position["sl"],
                        tp=position["tp"],
                        price=current,
                        atr=float(current_atr),
                        settings=settings,
                        score=momentum_score,
                        extensions=position.get("tp_extensions", 0),
                    )
                    if plan is not None and plan.changed:
                        position["sl"] = plan.sl
                        position["tp"] = plan.tp
                        position["tp_extensions"] = plan.extensions
                        bracket_adjustments += 1
                unrealised = (current - position["entry"]) * position["lots"] * CONTRACT_SIZE
                if position["side"] == "SELL":
                    unrealised *= -1
                equity = balance + unrealised
                peak = max(peak, equity)
                max_drawdown = max(max_drawdown, (peak - equity) / peak * 100.0 if peak else 0.0)
        else:
            equity = balance

        if (peak - equity) / peak * 100.0 >= dd_limit and not halted:
            halted = True

        equity_curve.append({"time": int(candle.time.timestamp()), "equity": round(equity, 2)})

        daily_loss_pct = max(0.0, (day_start_equity - equity) / day_start_equity * 100.0) if day_start_equity else 0.0
        if position is None and not halted and i >= cooldown_until and daily_loss_pct < daily_limit:
            if not _market_open(candle.time) or not _in_session(candle.time, session_start, session_end):
                continue
            if spread > max_spread:
                continue
            quote = Quote(symbol=symbol, bid=candle.close - spread / 2, ask=candle.close + spread / 2, time=candle.time)
            window_start = max(0, i - 260)
            signal = strategy.evaluate(
                closed_candles=candles[max(0, i - 260):i + 1],
                quote=quote,
                timeframe=timeframe,
                min_confidence=min_conf,
                rr_ratio=rr,
                atr_stop_mult=atr_mult,
            )
            if signal.action not in {"BUY", "SELL"}:
                continue
            stop_distance = abs(signal.entry - signal.sl)
            if stop_distance <= 0.01:
                continue
            risk_budget = equity * risk_pct / 100.0
            raw_lots = risk_budget / (stop_distance * CONTRACT_SIZE)
            lots = min(max_lots, _round_lots(raw_lots))
            if lots < MIN_LOT:
                continue
            actual_risk = lots * stop_distance * CONTRACT_SIZE
            if actual_risk > risk_budget * 1.35 + 0.5:
                continue
            side = signal.action
            fill = next_candle.open + spread / 2 + slippage if side == "BUY" else next_candle.open - spread / 2 - slippage
            fill = round(fill, 2)
            entry_sl = round(fill - stop_distance if side == "BUY" else fill + stop_distance, 2)
            entry_tp = round(fill + stop_distance * rr if side == "BUY" else fill - stop_distance * rr, 2)
            position = {
                "ticket": f"BT-{len(closed) + 1:04d}",
                "side": side,
                "lots": round(lots, 2),
                "entry": fill,
                "sl": entry_sl,
                "tp": entry_tp,
                "initial_sl": entry_sl,
                "initial_tp": entry_tp,
                "tp_extensions": 0,
                "opened_at": iso(next_candle.time),
                "confidence": round(signal.confidence, 3),
                "regime": signal.regime,
                "risk": round(actual_risk, 2),
            }

    if position is not None:
        final = candles[-1]
        close_price = final.close - spread / 2 - slippage if position["side"] == "BUY" else final.close + spread / 2 + slippage
        pnl = (close_price - position["entry"]) * position["lots"] * CONTRACT_SIZE
        if position["side"] == "SELL":
            pnl *= -1
        fees = commission_per_lot * position["lots"]
        pnl -= fees
        balance += pnl
        equity = balance
        trade = dict(position)
        trade.update({
            "exit": round(close_price, 2),
            "closed_at": iso(final.time),
            "pnl": round(pnl, 2),
            "fees": round(fees, 2),
            "exit_reason": "End of test",
            "return_pct": round(pnl / start_balance * 100.0, 3),
        })
        closed.append(trade)
        equity_curve[-1]["equity"] = round(equity, 2)

    sample = downsample([(point["time"], point["equity"]) for point in equity_curve], 240)
    curve = [{"time": ts, "equity": round(eq, 2)} for ts, eq in sample]
    stats = _compute_stats(closed, start_balance, max_drawdown, equity_curve)
    buy_hold = (candles[-1].close - candles[WARMUP].close) / candles[WARMUP].close * 100.0
    elapsed = time.monotonic() - started
    default_label = "Simulated XAUUSD" if synthetic_data else f"MT5 history · {symbol}"
    label = source_label or f"{default_label} · {timeframe} · dynamic brackets"
    disclaimer = (
        "Backtest on synthetic data. Fees, slippage and conservative stop-first fills are simulated. "
        "Past simulated performance does not imply future results."
        if synthetic_data else
        "Backtest on imported MT5 history. Fills and costs are still simulated, and this does not imply future results."
    )
    result = {
        "ok": True,
        "simulated": True,
        "data_source": "synthetic" if synthetic_data else data_source,
        "label": label,
        "started_at": iso(),
        "duration_seconds": round(elapsed, 2),
        "request": {
            "days": days,
            "starting_balance": start_balance,
            "seed": seed if synthetic_data else None,
            "bars": len(candles),
            "timeframe": timeframe,
            "data_source": "synthetic" if synthetic_data else data_source,
            "costs": {
                "commission_per_lot": commission_per_lot,
                "spread": spread,
                "slippage": slippage,
            },
        },
        "summary": {
            "starting_balance": round(start_balance, 2),
            "ending_equity": round(equity, 2),
            "net_pnl": round(equity - start_balance, 2),
            "return_pct": round((equity / start_balance - 1.0) * 100.0, 3),
            "max_drawdown_pct": round(stats["max_drawdown_pct"], 3),
            "buy_hold_pct": round(buy_hold, 3),
            "trades": len(closed),
            "win_rate_pct": round(stats["win_rate_pct"], 2),
            "profit_factor": round(stats["profit_factor"], 3),
            "sharpe": round(stats["sharpe"], 2) if stats["sharpe"] is not None else None,
            "avg_hold_hours": round(stats["avg_hold_hours"], 2),
            "total_fees": round(sum(t["fees"] for t in closed), 2),
            "halted_by_drawdown": halted,
            "exposure_pct": round(bars_in_market / max(1, len(candles) - WARMUP) * 100.0, 2),
            "bracket_adjustments": bracket_adjustments,
        },
        "management": {
            "dynamic_brackets": bool(settings.get("auto_adjust_brackets", True)),
            "breakeven_at_r": settings.get("breakeven_at_r"),
            "trailing_atr_mult": settings.get("trailing_atr_mult"),
            "extend_tp_enabled": bool(settings.get("extend_tp_enabled", True)),
            "max_tp_extensions": settings.get("max_tp_extensions"),
        },
        "equity": curve,
        "trades": list(reversed(closed[-60:])),
        "disclaimer": disclaimer,
    }
    return result


def _market_open(dt: datetime) -> bool:
    weekday = dt.weekday()
    hour = dt.hour + dt.minute / 60.0
    if weekday == 5:
        return False
    if weekday == 4 and hour >= 21.0:
        return False
    if weekday == 6 and hour < 22.0:
        return False
    return True


def _in_session(dt: datetime, start: float, end: float) -> bool:
    hour = dt.hour + dt.minute / 60.0
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def _compute_stats(trades: List[Dict[str, Any]], start_balance: float, max_drawdown: float,
                   equity_curve: List[Dict[str, Any]]) -> Dict[str, float]:
    pnls = [float(t["pnl"]) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    win_rate = len(wins) / len(pnls) * 100.0 if pnls else 0.0
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0)

    daily: Dict[int, float] = {}
    for point in equity_curve:
        daily[point["time"] // 86400] = point["equity"]
    ordered = [daily[k] for k in sorted(daily)]
    returns = []
    for prev, current in zip(ordered, ordered[1:]):
        if prev > 0:
            returns.append(current / prev - 1.0)
    sharpe = None
    # Annualising a handful of daily observations is statistically meaningless.
    if len(returns) >= 10:
        stdev = statistics.pstdev(returns)
        if stdev > 0:
            sharpe = statistics.mean(returns) / stdev * math.sqrt(252)
    holds = []
    for trade in trades:
        opened = datetime.fromisoformat(trade["opened_at"].replace("Z", "+00:00"))
        closed = datetime.fromisoformat(trade["closed_at"].replace("Z", "+00:00"))
        holds.append(max(0.0, (closed - opened).total_seconds() / 3600.0))
    return {
        "win_rate_pct": win_rate,
        "profit_factor": profit_factor,
        "max_drawdown_pct": max_drawdown,
        "sharpe": sharpe,
        "avg_hold_hours": sum(holds) / len(holds) if holds else 0.0,
    }
