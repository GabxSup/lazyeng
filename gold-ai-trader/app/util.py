"""Small shared helpers."""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any, Iterable, List, Optional, Tuple


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: Optional[datetime] = None) -> str:
    value = dt or utcnow()
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def round_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    return round(math.floor(value / step + 1e-9) * step, 8)


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        if math.isnan(result) or math.isinf(result):
            return default
        return result
    except (TypeError, ValueError):
        return default


def pct(part: float, whole: float) -> float:
    if not whole:
        return 0.0
    return (part / whole) * 100.0


def money(value: float, currency: str = "USD") -> str:
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.2f}"


def mask_secret(value: str, keep: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= keep:
        return "*" * len(value)
    return value[:2] + "•" * max(4, len(value) - keep) + value[-keep:]


def redact_text(text: str, secrets: Iterable[str]) -> str:
    result = str(text)
    for secret in secrets:
        if secret and len(secret) >= 4:
            result = result.replace(secret, "[redacted]")
    return result


def normalise_symbol(value: str) -> str:
    symbol = re.sub(r"[^A-Za-z0-9._#-]", "", str(value).upper())
    return symbol[:24] or "XAUUSD"


def timeframe_seconds(timeframe: str) -> int:
    table = {"M1": 60, "M5": 300, "M15": 900, "M30": 1800, "H1": 3600, "H4": 14400, "D1": 86400}
    return table.get(timeframe.upper(), 300)


def resample(candles: List[Any], seconds: int) -> List[Any]:
    """Resample lightweight candle dictionaries into a larger bucket.

    Candle dicts use epoch seconds in ``time`` and OHLC floats. The newest
    partial bucket is retained so the chart can keep drawing a live candle.
    """
    if seconds <= 0 or not candles:
        return list(candles)
    buckets: List[dict] = []
    current_key: Optional[int] = None
    current: Optional[dict] = None
    for raw in candles:
        timestamp = int(raw["time"])
        key = timestamp - (timestamp % seconds)
        if current_key != key or current is None:
            if current is not None:
                buckets.append(current)
            current_key = key
            current = {
                "time": key,
                "open": float(raw["open"]),
                "high": float(raw["high"]),
                "low": float(raw["low"]),
                "close": float(raw["close"]),
                "volume": float(raw.get("volume", 0)),
            }
        else:
            current["high"] = max(current["high"], float(raw["high"]))
            current["low"] = min(current["low"], float(raw["low"]))
            current["close"] = float(raw["close"])
            current["volume"] += float(raw.get("volume", 0))
    if current is not None:
        buckets.append(current)
    return buckets


def downsample(points: List[Tuple[float, float]], limit: int = 180) -> List[Tuple[float, float]]:
    if len(points) <= limit:
        return points
    step = len(points) / float(limit)
    result = []
    for i in range(limit):
        result.append(points[min(len(points) - 1, int(i * step))])
    if result[-1] != points[-1]:
        result[-1] = points[-1]
    return result
