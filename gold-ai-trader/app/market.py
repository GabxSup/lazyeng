"""Synthetic XAUUSD market data for paper trading and offline backtests.

This feed is deliberately labelled everywhere as simulated. It is useful for
exercising the execution stack and dashboard without a broker connection, but
it is not a substitute for exchange-grade historical data.
"""
from __future__ import annotations

import math
import random
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

from .models import Candle, Quote
from .util import resample, timeframe_seconds, utcnow

BASE_INTERVAL = 300
HISTORY_BARS = 4200
FEED_SEED = 918273


def _utc_floor(timestamp: float, interval: int) -> int:
    return int(timestamp) - (int(timestamp) % interval)


def synthetic_series(
    count: int,
    end_time: Optional[datetime] = None,
    interval: int = BASE_INTERVAL,
    seed: int = FEED_SEED,
    base_price: float = 4000.0,
    finish_price: Optional[float] = None,
) -> List[Candle]:
    """Generate a deterministic-but-market-like OHLC series.

    The final timestamp is the last *closed* candle before ``end_time``. If a
    ``finish_price`` is supplied the entire path is rescaled so the last close
    lands on it, which keeps paper account equity continuous across restarts.
    """
    if count < 10:
        raise ValueError("Need at least ten candles")
    now = end_time or utcnow()
    anchor = _utc_floor(now.timestamp(), interval)
    start = anchor - count * interval
    rng = random.Random(seed)
    candles: List[Candle] = []
    price = float(base_price)
    regime = rng.choice([-1.0, 1.0])
    for i in range(count):
        if i % rng.randint(110, 240) == 0:
            regime = rng.choice([-1.0, 1.0])
        wave = math.sin(i / 53.0) * 0.9 + math.sin(i / 11.0) * 0.22
        pull = (base_price - price) * 0.00008
        drift = (regime * 0.045) + wave * 0.025 + pull
        vol = 0.56 * (0.78 + 0.36 * (1.0 + math.sin(i / 29.0)))
        if rng.random() < 0.006:
            drift += rng.choice([-1.0, 1.0]) * rng.uniform(0.8, 2.4)
        timestamp = start + i * interval
        dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        hour = dt.hour
        activity = 1.0
        if 7 <= hour < 11:
            activity = 1.25
        elif 12 <= hour < 17:
            activity = 1.45
        elif 21 <= hour or hour < 2:
            activity = 0.7
        open_price = price
        close_price = open_price + drift + rng.gauss(0.0, vol) * activity
        wick = vol * activity * rng.uniform(0.25, 1.05)
        high = max(open_price, close_price) + wick * rng.uniform(0.2, 1.0)
        low = min(open_price, close_price) - wick * rng.uniform(0.2, 1.0)
        base_volume = 900 + rng.randint(0, 900)
        candles.append(
            Candle(
                time=dt,
                open=open_price,
                high=high,
                low=low,
                close=close_price,
                volume=base_volume * activity,
            )
        )
        price = close_price
    if finish_price is not None and candles and candles[-1].close:
        ratio = float(finish_price) / candles[-1].close
        for candle in candles:
            candle.open *= ratio
            candle.high *= ratio
            candle.low *= ratio
            candle.close *= ratio
        price = float(finish_price)
    for candle in candles:
        candle.open = round(candle.open, 2)
        candle.high = round(candle.high, 2)
        candle.low = round(candle.low, 2)
        candle.close = round(candle.close, 2)
        candle.volume = round(candle.volume, 0)
    return candles


class SimulatedMarket:
    """A live-feeling synthetic feed that advances with wall-clock time."""

    def __init__(self, starting_price: Optional[float] = None) -> None:
        self.seed = FEED_SEED
        self.rng = random.Random()
        self.interval = BASE_INTERVAL
        self.starting_price = float(starting_price or 4000.0)
        now = utcnow()
        self._bars = synthetic_series(
            HISTORY_BARS,
            end_time=now,
            interval=self.interval,
            seed=self.seed,
            base_price=4000.0,
            finish_price=self.starting_price,
        )
        current_bucket = _utc_floor(now.timestamp(), self.interval)
        self._forming = Candle(
            time=datetime.fromtimestamp(current_bucket, tz=timezone.utc),
            open=self._bars[-1].close,
            high=self._bars[-1].close,
            low=self._bars[-1].close,
            close=self._bars[-1].close,
            volume=random.randint(25, 120),
        )
        self._spread = 0.22
        self._last_refresh = time.time()
        self._last_persist = 0.0
        self.refresh()

    @property
    def spot(self) -> float:
        return self._forming.close

    def _roll_bars(self, bucket: int) -> None:
        while int(self._forming.time.timestamp()) < bucket:
            self._bars.append(self._forming)
            previous = self._forming.close
            next_time = datetime.fromtimestamp(int(self._forming.time.timestamp()) + self.interval, tz=timezone.utc)
            self._forming = Candle(
                time=next_time,
                open=previous,
                high=previous,
                low=previous,
                close=previous,
                volume=0,
            )
        if len(self._bars) > HISTORY_BARS + 300:
            self._bars = self._bars[-HISTORY_BARS:]

    def refresh(self) -> Quote:
        now = time.time()
        elapsed = max(0.2, min(10.0, now - self._last_refresh))
        self._last_refresh = now
        bucket = _utc_floor(now, self.interval)
        self._roll_bars(bucket)
        # A small stochastic tick with an occasional impulse. Sigma is scaled
        # from a realistic 5-minute range so fast polling looks smooth.
        sigma = 0.12 * math.sqrt(elapsed / 2.0)
        step = self.rng.gauss(0.0, sigma)
        if self.rng.random() < 0.003:
            step += self.rng.choice([-1.0, 1.0]) * self.rng.uniform(0.08, 0.22)
        new_price = round(max(1.0, self._forming.close + step), 2)
        self._forming.close = new_price
        self._forming.high = max(self._forming.high, new_price)
        self._forming.low = min(self._forming.low, new_price)
        self._forming.volume += int(max(1, elapsed * self.rng.uniform(4, 14)))
        target_spread = 0.16 + self.rng.uniform(0.0, 0.08) + min(0.12, abs(step) * 2)
        self._spread = round(self._spread * 0.8 + target_spread * 0.2, 2)
        if self._last_persist == 0.0:
            self._last_persist = now
        return self.quote()

    def quote(self) -> Quote:
        mid = self._forming.close
        half = self._spread / 2.0
        return Quote(
            symbol="XAUUSD",
            bid=round(mid - half, 2),
            ask=round(mid + half, 2),
            time=utcnow(),
        )

    def candles(self, timeframe: str = "M5", limit: int = 240) -> List[dict]:
        self.refresh()
        all_bars = self._bars + [self._forming]
        seconds = timeframe_seconds(timeframe)
        if seconds == self.interval:
            series = all_bars
        else:
            series = resample([c.to_dict() for c in all_bars], seconds)
            return series[-max(1, limit):]
        return [c.to_dict() for c in series[-max(1, limit):]]

    def persist_spot(self, store: object) -> None:
        now = time.time()
        if now - self._last_persist >= 15:
            self._last_persist = now
            try:
                store.set_runtime("paper_spot", round(self.spot, 2))  # type: ignore[attr-defined]
            except Exception:
                pass

    def session_stats(self) -> Dict[str, float]:
        now = utcnow()
        today_start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
        today_bars = [c for c in self._bars if c.time >= today_start]
        open_price = today_bars[0].open if today_bars else self._forming.open
        change = self.spot - open_price
        pct = (change / open_price * 100.0) if open_price else 0.0
        return {"open": round(open_price, 2), "change": round(change, 2), "change_pct": round(pct, 3)}
