"""Performance statistics derived from closed trades."""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Dict, List


def _pnl(trade: Dict[str, Any]) -> float:
    try:
        return float(trade.get("pnl") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def trade_stats(trades: List[Dict[str, Any]], starting_balance: float) -> Dict[str, Any]:
    closed = [t for t in trades if t.get("status") == "closed"]
    pnls = [_pnl(t) for t in closed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    breakeven = [p for p in pnls if p == 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    net = sum(pnls)
    win_rate = len(wins) / len(pnls) * 100.0 if pnls else 0.0
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0)
    avg_win = gross_profit / len(wins) if wins else 0.0
    avg_loss = gross_loss / len(losses) if losses else 0.0
    expectancy = (win_rate / 100.0 * avg_win) - ((1 - win_rate / 100.0) * avg_loss) if pnls else 0.0
    max_consecutive_losses = 0
    current = 0
    for value in pnls:
        if value < 0:
            current += 1
            max_consecutive_losses = max(max_consecutive_losses, current)
        else:
            current = 0
    fees = sum(float(t.get("fees") or 0.0) for t in closed)
    return {
        "trades": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "breakeven": len(breakeven),
        "win_rate_pct": round(win_rate, 2),
        "profit_factor": round(min(profit_factor, 999.0), 2),
        "net_pnl": round(net, 2),
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "expectancy": round(expectancy, 2),
        "best_trade": round(max(pnls), 2) if pnls else 0.0,
        "worst_trade": round(min(pnls), 2) if pnls else 0.0,
        "max_consecutive_losses": max_consecutive_losses,
        "fees": round(fees, 2),
        "return_pct": round(net / starting_balance * 100.0, 2) if starting_balance else 0.0,
        "starting_balance": round(starting_balance, 2),
    }


def daily_realised(trades: List[Dict[str, Any]], now: datetime) -> float:
    start = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    total = 0.0
    for trade in trades:
        raw = trade.get("closed_at")
        if not raw:
            continue
        try:
            closed_at = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if closed_at.tzinfo is None:
                closed_at = closed_at.replace(tzinfo=timezone.utc)
            if closed_at >= start:
                total += _pnl(trade)
        except (ValueError, TypeError):
            continue
    return round(total, 2)


def public_trade(trade: Dict[str, Any]) -> Dict[str, Any]:
    def number(value: Any) -> float:
        try:
            return round(float(value), 2)
        except (TypeError, ValueError):
            return 0.0

    return {
        "ticket": str(trade.get("ticket", "")),
        "signal_id": trade.get("signal_id"),
        "symbol": trade.get("symbol", "XAUUSD"),
        "side": trade.get("side", ""),
        "lots": number(trade.get("lots")),
        "entry_price": number(trade.get("entry_price")),
        "exit_price": number(trade.get("exit_price")) if trade.get("exit_price") is not None else None,
        "sl": number(trade.get("sl")),
        "tp": number(trade.get("tp")),
        "opened_at": trade.get("opened_at"),
        "closed_at": trade.get("closed_at"),
        "status": trade.get("status"),
        "pnl": number(trade.get("pnl")),
        "fees": number(trade.get("fees")),
        "confidence": number(trade.get("confidence")),
        "reason": trade.get("reason", ""),
        "close_reason": trade.get("close_reason"),
        "mode": trade.get("mode", "paper"),
    }
