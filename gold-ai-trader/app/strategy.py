"""Hybrid quant + language-model signal generation.

The quant model is the only component allowed to originate a trade. The LLM
overlay can temper or veto a setup, never create one, never choose a lot size,
and never bypass the risk governor downstream.
"""
from __future__ import annotations

import math
import uuid
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .indicators import adx, atr, bollinger, ema, macd, rsi
from .models import Candle, Quote, Signal
from .util import clamp, utcnow


def _book(candles: Sequence[Any]) -> Tuple[List[float], List[float], List[float], List[float], List[float]]:
    opens: List[float] = []
    highs: List[float] = []
    lows: List[float] = []
    closes: List[float] = []
    volumes: List[float] = []
    for candle in candles:
        if isinstance(candle, dict):
            opens.append(float(candle["open"]))
            highs.append(float(candle["high"]))
            lows.append(float(candle["low"]))
            closes.append(float(candle["close"]))
            volumes.append(float(candle.get("volume", 0.0)))
        else:
            opens.append(float(candle.open))
            highs.append(float(candle.high))
            lows.append(float(candle.low))
            closes.append(float(candle.close))
            volumes.append(float(candle.volume))
    return opens, highs, lows, closes, volumes


def confidence_from_score(score: float) -> float:
    return clamp(0.5 + abs(score) * 0.46, 0.5, 0.96)


class QuantStrategy:
    name = "AURUM quant v1"

    def __init__(self, symbol: str = "XAUUSD") -> None:
        self.symbol = symbol

    def wait_signal(
        self,
        quote: Quote,
        timeframe: str,
        reason: str,
        features: Optional[Dict[str, float]] = None,
    ) -> Signal:
        return Signal(
            id=uuid.uuid4().hex[:16],
            action="WAIT",
            confidence=0.5,
            raw_score=0.0,
            regime="Unknown",
            entry=quote.mid,
            sl=0.0,
            tp=0.0,
            reasons=[reason],
            features=features or {},
            symbol=self.symbol,
            timeframe=timeframe,
            created_at=utcnow(),
            status="wait",
        )

    def evaluate(
        self,
        closed_candles: Sequence[Any],
        quote: Quote,
        timeframe: str = "M5",
        min_confidence: float = 0.64,
        rr_ratio: float = 1.8,
        atr_stop_mult: float = 1.6,
    ) -> Signal:
        """Evaluate only *closed* candles. The caller must exclude the forming bar."""
        if len(closed_candles) < 210:
            return self.wait_signal(
                quote,
                timeframe,
                f"Warming up — {len(closed_candles)}/210 closed candles required.",
                {"bars": float(len(closed_candles))},
            )
        opens, highs, lows, closes, _volumes = _book(closed_candles)
        e20 = ema(closes, 20)
        e50 = ema(closes, 50)
        e200 = ema(closes, 200)
        rsi_series = rsi(closes, 14)
        atr_series = atr(highs, lows, closes, 14)
        macd_line, _macd_signal, macd_hist = macd(closes, 12, 26, 9)
        _bb_mid, bb_upper, bb_lower = bollinger(closes, 20, 2.0)
        adx_series, plus_di, minus_di = adx(highs, lows, closes, 14)

        required = {
            "EMA20": e20[-1], "EMA50": e50[-1], "EMA200": e200[-1], "RSI": rsi_series[-1],
            "ATR": atr_series[-1], "MACD": macd_hist[-1], "ADX": adx_series[-1],
            "BB upper": bb_upper[-1], "BB lower": bb_lower[-1],
        }
        missing = [name for name, value in required.items() if value is None]
        if missing:
            return self.wait_signal(quote, timeframe, "Indicator warm-up incomplete: " + ", ".join(missing))

        close = closes[-1]
        ema20 = float(e20[-1])  # type: ignore[arg-type]
        ema50 = float(e50[-1])  # type: ignore[arg-type]
        ema200 = float(e200[-1])  # type: ignore[arg-type]
        rsi_value = float(rsi_series[-1])  # type: ignore[arg-type]
        atr_value = max(0.05, float(atr_series[-1]))  # type: ignore[arg-type]
        macd_value = float(macd_hist[-1])  # type: ignore[arg-type]
        adx_value = float(adx_series[-1])  # type: ignore[arg-type]
        bb_up = float(bb_upper[-1])  # type: ignore[arg-type]
        bb_low = float(bb_lower[-1])  # type: ignore[arg-type]
        pdi = float(plus_di[-1] or 0.0)
        mdi = float(minus_di[-1] or 0.0)
        bb_width = max(0.01, bb_up - bb_low)

        trend_gap = (ema20 - ema50) / max(atr_value, 0.01)
        structure = math.tanh(trend_gap / 0.7)
        long_term = math.tanh((ema50 - ema200) / max(atr_value * 2.2, 0.01))
        momentum = math.tanh(macd_value / max(atr_value * 0.22, 0.01))
        rsi_component = clamp((rsi_value - 50.0) / 18.0, -1.0, 1.0)
        if rsi_value >= 74 and rsi_component > 0:
            rsi_component *= 0.45
        elif rsi_value <= 26 and rsi_component < 0:
            rsi_component *= 0.45
        prev_high = max(highs[-21:-1])
        prev_low = min(lows[-21:-1])
        breakout = 0.0
        breakout_note = ""
        if close > prev_high:
            breakout = 1.0
            breakout_note = "break above the 20-bar high"
        elif close < prev_low:
            breakout = -1.0
            breakout_note = "break below the 20-bar low"
        candle_range = max(0.01, highs[-1] - lows[-1])
        body = (closes[-1] - opens[-1]) / candle_range
        ema_slope = (ema20 - float(e20[-6])) if e20[-6] is not None else 0.0
        slope_score = clamp(ema_slope / max(atr_value * 2.0, 0.01), -1.0, 1.0)

        score = (
            structure * 0.30
            + long_term * 0.12
            + momentum * 0.18
            + rsi_component * 0.12
            + breakout * 0.14
            + clamp(body, -1.0, 1.0) * 0.07
            + slope_score * 0.07
        )

        adx_value = float(adx_series[-1])  # type: ignore[arg-type]
        bb_width_atr = bb_width / atr_value
        if adx_value >= 23 and abs(trend_gap) >= 0.12:
            regime = "Trend expansion"
        elif bb_width_atr <= 1.65 and adx_value < 20:
            regime = "Compression"
        elif adx_value < 18:
            regime = "Range"
        else:
            regime = "Transition"

        reasons: List[str] = []
        if abs(trend_gap) >= 0.1:
            direction = "above" if trend_gap > 0 else "below"
            reasons.append(f"EMA20 is {direction} EMA50 by {abs(trend_gap):.2f} ATR.")
        if abs(long_term) >= 0.08:
            reasons.append("Higher-timeframe EMA structure is " + ("bullish." if long_term > 0 else "bearish."))
        if abs(momentum) >= 0.12:
            reasons.append("MACD impulse is " + ("positive." if momentum > 0 else "negative."))
        if 52 <= rsi_value <= 72:
            reasons.append(f"RSI {rsi_value:.1f} confirms momentum without being extreme.")
        elif rsi_value >= 72:
            reasons.append(f"RSI {rsi_value:.1f} is stretched; momentum score is discounted.")
        elif rsi_value <= 28:
            reasons.append(f"RSI {rsi_value:.1f} is oversold; chase risk is elevated.")
        else:
            reasons.append(f"RSI {rsi_value:.1f} is neutral.")
        if breakout_note:
            reasons.append("Price " + breakout_note + ".")
        reasons.append(f"{regime} · ADX {adx_value:.1f} · DI+ {pdi:.1f}/DI− {mdi:.1f}.")

        # Fade exhaustion only when the market is genuinely ranging. This is a
        # smaller counter-trend sleeve, not a reason to ignore risk limits.
        if regime in {"Range", "Compression"}:
            if rsi_value <= 32 and close <= bb_low:
                score += 0.24
                reasons.append("Oversold range fade setup at the lower band.")
            elif rsi_value >= 68 and close >= bb_up:
                score -= 0.24
                reasons.append("Overbought range fade setup at the upper band.")

        score = clamp(score, -1.0, 1.0)
        confidence = confidence_from_score(score)
        action = "WAIT"
        if confidence >= min_confidence and abs(score) >= 0.08:
            action = "BUY" if score > 0 else "SELL"
        elif confidence >= min_confidence:
            reasons.append("Directional edge is too small to trade.")
        else:
            reasons.append(f"Conviction {confidence * 100:.0f}% is below the {min_confidence * 100:.0f}% gate.")

        entry = quote.ask if action == "BUY" else quote.bid if action == "SELL" else quote.mid
        sl = 0.0
        tp = 0.0
        risk_distance = max(0.5, atr_value * atr_stop_mult)
        if action == "BUY":
            sl = round(entry - risk_distance, 2)
            tp = round(entry + risk_distance * rr_ratio, 2)
        elif action == "SELL":
            sl = round(entry + risk_distance, 2)
            tp = round(entry - risk_distance * rr_ratio, 2)

        features = {
            "close": close,
            "ema20": ema20,
            "ema50": ema50,
            "ema200": ema200,
            "rsi": rsi_value,
            "atr": atr_value,
            "atr_pct": atr_value / close * 100.0,
            "adx": adx_value,
            "macd_hist": macd_value,
            "bb_width_atr": bb_width_atr,
            "spread": quote.spread,
            "score": score,
            "trend_gap_atr": trend_gap,
        }
        return Signal(
            id=uuid.uuid4().hex[:16],
            action=action,
            confidence=confidence,
            raw_score=score,
            regime=regime,
            entry=round(entry, 2),
            sl=sl,
            tp=tp,
            reasons=reasons[:6],
            features=features,
            symbol=self.symbol,
            timeframe=timeframe,
            created_at=utcnow(),
            status="new" if action != "WAIT" else "wait",
        )

    def apply_ai(
        self,
        signal: Signal,
        review: Dict[str, Any],
        min_confidence: float,
    ) -> Signal:
        """Blend an AI review into a quant signal without giving it authority.

        The overlay has a maximum 30% influence, cannot originate a direction
        when the quant model is waiting, and can only reduce or reverse the
        blended score into a WAIT — it can never enlarge a position.
        """
        signal.ai = {k: v for k, v in review.items() if k not in {"raw"}}
        if not review.get("used"):
            signal.ai["status"] = review.get("status", "unavailable")
            return signal
        ai_bias = float(review.get("bias_score", 0.0))
        ai_conf = clamp(float(review.get("confidence", 0.0)), 0.0, 1.0)
        weight = 0.30 * ai_conf
        original_score = signal.raw_score
        blended = clamp(original_score * (1.0 - weight) + ai_bias * weight, -1.0, 1.0)
        was_candidate = signal.action in {"BUY", "SELL"}
        original_direction = 1 if signal.action == "BUY" else -1 if signal.action == "SELL" else 0
        signal.raw_score = blended
        signal.confidence = confidence_from_score(blended)
        signal.features["ai_bias"] = ai_bias
        signal.features["ai_confidence"] = ai_conf
        signal.features["quant_score"] = original_score
        rationale = str(review.get("rationale", "")).strip()
        if rationale:
            signal.reasons = (signal.reasons + [f"AI analyst: {rationale[:180]}"])[-6:]
        risks = review.get("risks") or []
        if risks:
            signal.ai["risks"] = [str(r)[:160] for r in risks[:4]]

        new_direction = 1 if blended > 0.02 else -1 if blended < -0.02 else 0
        if was_candidate and new_direction == original_direction and signal.confidence >= min_confidence:
            signal.action = "BUY" if new_direction > 0 else "SELL"
            signal.status = "new"
            # Re-anchor entry and bracket to the current quote only if the
            # direction survived. A vetoed signal never reaches execution.
            risk_distance = abs(signal.entry - signal.sl) if signal.sl else 0.0
            if risk_distance <= 0:
                signal.action = "WAIT"
                signal.status = "wait"
        else:
            if was_candidate:
                reason = "AI overlay vetoed the quant setup" if new_direction else "AI overlay reduced conviction below the execution floor."
                signal.reasons = (signal.reasons + [reason])[-6:]
            signal.action = "WAIT"
            signal.sl = 0.0
            signal.tp = 0.0
            signal.status = "wait"
        return signal
