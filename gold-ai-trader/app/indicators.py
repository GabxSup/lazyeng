"""Dependency-free technical indicators used by the strategy and backtester.

Every series-returning function preserves the input length. Values that cannot
be calculated yet are ``None`` so callers never accidentally use look-ahead
information from a short warm-up window.
"""
from __future__ import annotations

import math
from typing import List, Optional, Sequence, Tuple

NumberSeries = List[Optional[float]]


def sma(values: Sequence[float], period: int) -> NumberSeries:
    out: NumberSeries = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    window = 0.0
    for i, value in enumerate(values):
        window += float(value)
        if i >= period:
            window -= float(values[i - period])
        if i >= period - 1:
            out[i] = window / period
    return out


def ema(values: Sequence[float], period: int) -> NumberSeries:
    out: NumberSeries = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    seed = sum(float(v) for v in values[:period]) / period
    out[period - 1] = seed
    multiplier = 2.0 / (period + 1.0)
    previous = seed
    for i in range(period, len(values)):
        previous = (float(values[i]) - previous) * multiplier + previous
        out[i] = previous
    return out


def _ema_dense(values: Sequence[float], period: int) -> List[float]:
    if not values:
        return []
    seed = sum(values[:period]) / min(period, len(values))
    result = [seed]
    multiplier = 2.0 / (period + 1.0)
    for value in values[1:]:
        result.append((value - result[-1]) * multiplier + result[-1])
    return result


def rsi(values: Sequence[float], period: int = 14) -> NumberSeries:
    out: NumberSeries = [None] * len(values)
    if period <= 0 or len(values) <= period:
        return out
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        delta = float(values[i]) - float(values[i - 1])
        if delta >= 0:
            gains += delta
        else:
            losses -= delta
    avg_gain = gains / period
    avg_loss = losses / period
    out[period] = _rsi_value(avg_gain, avg_loss)
    for i in range(period + 1, len(values)):
        delta = float(values[i]) - float(values[i - 1])
        gain = max(delta, 0.0)
        loss = max(-delta, 0.0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        out[i] = _rsi_value(avg_gain, avg_loss)
    return out


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss <= 1e-12:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def true_ranges(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float]) -> List[float]:
    result: List[float] = []
    previous_close: Optional[float] = None
    for high, low, close in zip(highs, lows, closes):
        high = float(high)
        low = float(low)
        if previous_close is None:
            result.append(high - low)
        else:
            result.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
        previous_close = float(close)
    return result


def atr(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int = 14) -> NumberSeries:
    tr = true_ranges(highs, lows, closes)
    out: NumberSeries = [None] * len(tr)
    if period <= 0 or len(tr) < period:
        return out
    previous = sum(tr[:period]) / period
    out[period - 1] = previous
    for i in range(period, len(tr)):
        previous = (previous * (period - 1) + tr[i]) / period
        out[i] = previous
    return out


def macd(
    values: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> Tuple[NumberSeries, NumberSeries, NumberSeries]:
    n = len(values)
    macd_line: NumberSeries = [None] * n
    signal_line: NumberSeries = [None] * n
    histogram: NumberSeries = [None] * n
    fast_series = ema(values, fast)
    slow_series = ema(values, slow)
    dense: List[float] = []
    indexes: List[int] = []
    for i in range(n):
        if fast_series[i] is not None and slow_series[i] is not None:
            dense.append(float(fast_series[i]) - float(slow_series[i]))  # type: ignore[arg-type]
            indexes.append(i)
            macd_line[i] = dense[-1]
    if len(dense) >= signal:
        signal_dense = _ema_dense(dense, signal)
        for dense_i, original_i in enumerate(indexes):
            if dense_i >= signal - 1:
                sig = signal_dense[dense_i]
                signal_line[original_i] = sig
                if macd_line[original_i] is not None:
                    histogram[original_i] = float(macd_line[original_i]) - sig  # type: ignore[arg-type]
    return macd_line, signal_line, histogram


def bollinger(values: Sequence[float], period: int = 20, deviation: float = 2.0) -> Tuple[NumberSeries, NumberSeries, NumberSeries]:
    n = len(values)
    middle: NumberSeries = [None] * n
    upper: NumberSeries = [None] * n
    lower: NumberSeries = [None] * n
    if period <= 1 or n < period:
        return middle, upper, lower
    window_sum = 0.0
    window_sq = 0.0
    for i, value in enumerate(values):
        value = float(value)
        window_sum += value
        window_sq += value * value
        if i >= period:
            old = float(values[i - period])
            window_sum -= old
            window_sq -= old * old
        if i >= period - 1:
            mean = window_sum / period
            variance = max(0.0, window_sq / period - mean * mean)
            sd = math.sqrt(variance)
            middle[i] = mean
            upper[i] = mean + deviation * sd
            lower[i] = mean - deviation * sd
    return middle, upper, lower


def adx(
    highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int = 14
) -> Tuple[NumberSeries, NumberSeries, NumberSeries]:
    n = len(highs)
    plus_di: NumberSeries = [None] * n
    minus_di: NumberSeries = [None] * n
    adx_line: NumberSeries = [None] * n
    if period <= 0 or n < period * 2 + 1:
        return adx_line, plus_di, minus_di

    tr = true_ranges(highs, lows, closes)
    plus_dm: List[float] = [0.0] * n
    minus_dm: List[float] = [0.0] * n
    for i in range(1, n):
        up_move = float(highs[i]) - float(highs[i - 1])
        down_move = float(lows[i - 1]) - float(lows[i])
        plus_dm[i] = up_move if up_move > down_move and up_move > 0 else 0.0
        minus_dm[i] = down_move if down_move > up_move and down_move > 0 else 0.0

    smooth_tr = sum(tr[1 : period + 1])
    smooth_plus = sum(plus_dm[1 : period + 1])
    smooth_minus = sum(minus_dm[1 : period + 1])
    dx_values: List[Tuple[int, float]] = []
    for i in range(period, n):
        if i > period:
            smooth_tr = smooth_tr - (smooth_tr / period) + tr[i]
            smooth_plus = smooth_plus - (smooth_plus / period) + plus_dm[i]
            smooth_minus = smooth_minus - (smooth_minus / period) + minus_dm[i]
        if smooth_tr <= 1e-12:
            continue
        pdi = 100.0 * smooth_plus / smooth_tr
        mdi = 100.0 * smooth_minus / smooth_tr
        plus_di[i] = pdi
        minus_di[i] = mdi
        denominator = pdi + mdi
        dx = 0.0 if denominator <= 1e-12 else 100.0 * abs(pdi - mdi) / denominator
        dx_values.append((i, dx))
    if len(dx_values) < period:
        return adx_line, plus_di, minus_di
    first_adx = sum(dx for _, dx in dx_values[:period]) / period
    first_index = dx_values[period - 1][0]
    adx_line[first_index] = first_adx
    previous_adx = first_adx
    for i, dx in dx_values[period:]:
        previous_adx = (previous_adx * (period - 1) + dx) / period
        adx_line[i] = previous_adx
    return adx_line, plus_di, minus_di


def slope(values: Sequence[Optional[float]], lookback: int) -> float:
    window = [v for v in values[-lookback:] if v is not None]
    if len(window) < 2:
        return 0.0
    return (window[-1] - window[0]) / max(1, len(window) - 1)


def last_valid(series: Sequence[Optional[float]], index: int = -1) -> Optional[float]:
    if not series:
        return None
    if index < 0:
        index = max(0, len(series) + index)
    for i in range(min(index, len(series) - 1), -1, -1):
        if series[i] is not None:
            return series[i]
    return None
