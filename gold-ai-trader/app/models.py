"""Typed domain objects shared by the trading stack and the HTTP layer."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from .util import iso, utcnow


@dataclass
class Candle:
    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "time": int(self.time.timestamp()),
            "open": round(self.open, 2),
            "high": round(self.high, 2),
            "low": round(self.low, 2),
            "close": round(self.close, 2),
            "volume": round(self.volume, 2),
        }


@dataclass
class Quote:
    symbol: str
    bid: float
    ask: float
    time: datetime = field(default_factory=utcnow)

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread(self) -> float:
        return max(0.0, self.ask - self.bid)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "bid": round(self.bid, 2),
            "ask": round(self.ask, 2),
            "mid": round(self.mid, 2),
            "spread": round(self.spread, 2),
            "time": iso(self.time),
        }


@dataclass
class Account:
    balance: float
    equity: float
    margin: float = 0.0
    free_margin: float = 0.0
    margin_level: float = 0.0
    currency: str = "USD"
    leverage: int = 100
    login: int = 0
    server: str = "AURUM Simulator"
    name: str = "Paper Trader"
    account_type: str = "demo"
    broker: str = "AURUM Sim"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "balance": round(self.balance, 2),
            "equity": round(self.equity, 2),
            "margin": round(self.margin, 2),
            "free_margin": round(self.free_margin, 2),
            "margin_level": round(self.margin_level, 2),
            "currency": self.currency,
            "leverage": self.leverage,
            "login": self.login,
            "server": self.server,
            "name": self.name,
            "account_type": self.account_type,
            "broker": self.broker,
        }


@dataclass
class Position:
    ticket: str
    symbol: str
    side: str
    lots: float
    entry_price: float
    current_price: float
    sl: float = 0.0
    tp: float = 0.0
    pnl: float = 0.0
    opened_at: datetime = field(default_factory=utcnow)
    confidence: float = 0.0
    reason: str = ""
    source: str = "paper"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ticket": self.ticket,
            "symbol": self.symbol,
            "side": self.side,
            "lots": round(self.lots, 2),
            "entry_price": round(self.entry_price, 2),
            "current_price": round(self.current_price, 2),
            "sl": round(self.sl, 2),
            "tp": round(self.tp, 2),
            "pnl": round(self.pnl, 2),
            "opened_at": iso(self.opened_at),
            "confidence": round(self.confidence, 3),
            "reason": self.reason,
            "source": self.source,
        }


@dataclass
class Signal:
    action: str
    confidence: float
    raw_score: float
    regime: str
    entry: float
    sl: float
    tp: float
    reasons: List[str] = field(default_factory=list)
    features: Dict[str, float] = field(default_factory=dict)
    ai: Dict[str, Any] = field(default_factory=dict)
    symbol: str = "XAUUSD"
    timeframe: str = "M5"
    created_at: datetime = field(default_factory=utcnow)
    id: str = ""
    status: str = "new"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "action": self.action,
            "confidence": round(self.confidence, 3),
            "raw_score": round(self.raw_score, 3),
            "regime": self.regime,
            "entry": round(self.entry, 2),
            "sl": round(self.sl, 2),
            "tp": round(self.tp, 2),
            "reasons": self.reasons,
            "features": {k: round(v, 4) for k, v in self.features.items()},
            "ai": self.ai,
            "created_at": iso(self.created_at),
            "status": self.status,
        }


@dataclass
class RiskCheck:
    key: str
    label: str
    passed: bool
    detail: str

    def to_dict(self) -> Dict[str, Any]:
        return {"key": self.key, "label": self.label, "passed": self.passed, "detail": self.detail}


@dataclass
class RiskAssessment:
    allowed: bool
    checks: List[RiskCheck]
    lots: float = 0.0
    risk_amount: float = 0.0
    sl: float = 0.0
    tp: float = 0.0
    entry: float = 0.0

    @property
    def blockers(self) -> List[str]:
        return [c.label for c in self.checks if not c.passed]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "allowed": self.allowed,
            "lots": round(self.lots, 2),
            "risk_amount": round(self.risk_amount, 2),
            "entry": round(self.entry, 2),
            "sl": round(self.sl, 2),
            "tp": round(self.tp, 2),
            "checks": [c.to_dict() for c in self.checks],
            "blockers": self.blockers,
        }


@dataclass
class ExecutionResult:
    ok: bool
    ticket: str = ""
    message: str = ""
    price: float = 0.0
    lots: float = 0.0
