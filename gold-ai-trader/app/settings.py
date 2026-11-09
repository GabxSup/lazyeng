"""Typed trading settings with a schema the dashboard renders dynamically."""
from __future__ import annotations

from typing import Any, Dict, Tuple

TIMEFRAMES = ["M5", "M15", "M30", "H1"]

SETTING_SPECS: Dict[str, Dict[str, Any]] = {
    "timeframe": {
        "type": "select",
        "label": "Signal timeframe",
        "group": "engine",
        "default": "M5",
        "options": TIMEFRAMES,
        "help": "Candle horizon used by the strategy. M5 is a balanced intraday default.",
    },
    "risk_per_trade_pct": {
        "type": "number",
        "label": "Risk per trade",
        "group": "risk",
        "default": 0.5,
        "min": 0.05,
        "max": 5.0,
        "step": 0.05,
        "suffix": "%",
        "help": "Percentage of equity risked between entry and stop-loss. Keep small.",
    },
    "max_daily_loss_pct": {
        "type": "number",
        "label": "Daily loss limit",
        "group": "risk",
        "default": 3.0,
        "min": 0.25,
        "max": 25.0,
        "step": 0.25,
        "suffix": "%",
        "help": "New entries stop after this much loss in a UTC day.",
    },
    "max_drawdown_pct": {
        "type": "number",
        "label": "Equity drawdown limit",
        "group": "risk",
        "default": 10.0,
        "min": 1.0,
        "max": 50.0,
        "step": 0.5,
        "suffix": "%",
        "help": "Hard halt when equity falls this far below its peak.",
    },
    "max_positions": {
        "type": "number",
        "label": "Max open positions",
        "group": "risk",
        "default": 1,
        "min": 1,
        "max": 5,
        "step": 1,
        "help": "One position keeps the risk model predictable.",
    },
    "max_lots": {
        "type": "number",
        "label": "Maximum lot size",
        "group": "risk",
        "default": 1.0,
        "min": 0.01,
        "max": 100.0,
        "step": 0.01,
        "help": "An absolute position-size ceiling, regardless of equity.",
    },
    "min_confidence": {
        "type": "number",
        "label": "Minimum confidence",
        "group": "engine",
        "default": 0.64,
        "min": 0.50,
        "max": 0.95,
        "step": 0.01,
        "help": "AI + quant conviction required before a market order can be sent.",
    },
    "rr_ratio": {
        "type": "number",
        "label": "Reward / risk target",
        "group": "engine",
        "default": 1.8,
        "min": 0.5,
        "max": 5.0,
        "step": 0.1,
        "suffix": "R",
        "help": "Take-profit distance as a multiple of stop distance.",
    },
    "atr_stop_mult": {
        "type": "number",
        "label": "ATR stop multiplier",
        "group": "engine",
        "default": 1.6,
        "min": 0.3,
        "max": 5.0,
        "step": 0.1,
        "suffix": "×",
        "help": "Stop distance in ATR units. Volatility widens the bracket.",
    },
    "max_spread": {
        "type": "number",
        "label": "Maximum spread",
        "group": "risk",
        "default": 0.45,
        "min": 0.05,
        "max": 5.0,
        "step": 0.01,
        "suffix": "$",
        "help": "No entries when the XAUUSD spread is wider than this.",
    },
    "cooldown_minutes": {
        "type": "number",
        "label": "Trade cooldown",
        "group": "risk",
        "default": 15,
        "min": 0,
        "max": 1440,
        "step": 1,
        "suffix": "min",
        "help": "Minimum gap after a position closes before a new entry.",
    },
    "session_start_utc": {
        "type": "number",
        "label": "Session start",
        "group": "engine",
        "default": 0,
        "min": 0,
        "max": 23,
        "step": 1,
        "suffix": "UTC",
        "help": "Entries are only allowed inside the daily window.",
    },
    "session_end_utc": {
        "type": "number",
        "label": "Session end",
        "group": "engine",
        "default": 24,
        "min": 1,
        "max": 24,
        "step": 1,
        "suffix": "UTC",
        "help": "End of the daily entry window. 24 means end of day.",
    },
    "auto_adjust_brackets": {
        "type": "boolean",
        "label": "Dynamic SL / TP",
        "group": "management",
        "default": True,
        "help": "Trail the stop toward break-even and extend the target when the trend continues.",
    },
    "breakeven_at_r": {
        "type": "number",
        "label": "Break-even trigger",
        "group": "management",
        "default": 1.0,
        "min": 0.2,
        "max": 5.0,
        "step": 0.1,
        "suffix": "R",
        "help": "Start trailing once the trade is this many times its initial risk in profit.",
    },
    "trailing_atr_mult": {
        "type": "number",
        "label": "Trailing distance",
        "group": "management",
        "default": 1.2,
        "min": 0.2,
        "max": 5.0,
        "step": 0.1,
        "suffix": "× ATR",
        "help": "Stop distance behind current price once the break-even trigger has been reached.",
    },
    "extend_tp_enabled": {
        "type": "boolean",
        "label": "Extend winners",
        "group": "management",
        "default": True,
        "help": "Move the take-profit further away near target if the stop is risk-free and momentum agrees.",
    },
    "tp_extension_atr": {
        "type": "number",
        "label": "Target extension",
        "group": "management",
        "default": 0.8,
        "min": 0.1,
        "max": 3.0,
        "step": 0.1,
        "suffix": "× ATR",
        "help": "How far to move the target when a continuation extension is allowed.",
    },
    "max_tp_extensions": {
        "type": "number",
        "label": "Max target extensions",
        "group": "management",
        "default": 2,
        "min": 0,
        "max": 5,
        "step": 1,
        "help": "A hard cap on target extensions per position. Set to 0 to keep the original target.",
    },
    "ai_overlay": {
        "type": "boolean",
        "label": "AI analyst overlay",
        "group": "ai",
        "default": True,
        "help": "Let the configured LLM veto or temper quant signals. It can never size or place an order.",
    },
}

GROUPS = {
    "engine": {"label": "Signal engine", "description": "How opportunity and conviction are measured."},
    "risk": {"label": "Risk governor", "description": "Hard boundaries applied to every order."},
    "management": {"label": "Trade management", "description": "How protective stops and targets adapt while a position is open."},
    "ai": {"label": "AI overlay", "description": "Optional language-model review of the quant setup."},
}

DEFAULT_SETTINGS: Dict[str, Any] = {key: spec["default"] for key, spec in SETTING_SPECS.items()}


class SettingsError(ValueError):
    pass


def validate_updates(updates: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(updates, dict) or not updates:
        raise SettingsError("No settings supplied.")
    clean: Dict[str, Any] = {}
    unknown = [key for key in updates if key not in SETTING_SPECS]
    if unknown:
        raise SettingsError(f"Unknown setting: {unknown[0]}")
    for key, raw in updates.items():
        spec = SETTING_SPECS[key]
        kind = spec["type"]
        if kind == "boolean":
            if not isinstance(raw, bool):
                raise SettingsError(f"{spec['label']} must be true or false.")
            clean[key] = raw
        elif kind == "select":
            value = str(raw)
            if value not in spec["options"]:
                raise SettingsError(f"{spec['label']} has an invalid option.")
            clean[key] = value
        elif kind == "number":
            try:
                value = float(raw)
            except (TypeError, ValueError):
                raise SettingsError(f"{spec['label']} must be a number.")
            if value < spec["min"] or value > spec["max"]:
                raise SettingsError(f"{spec['label']} must be between {spec['min']} and {spec['max']}.")
            if spec.get("step") == 1:
                value = int(round(value))
            else:
                value = round(value, 4)
            clean[key] = value
    if "session_start_utc" in clean or "session_end_utc" in clean:
        start = clean.get("session_start_utc", DEFAULT_SETTINGS["session_start_utc"])
        end = clean.get("session_end_utc", DEFAULT_SETTINGS["session_end_utc"])
        if start == end:
            raise SettingsError("Session start and end cannot be the same.")
    return clean


def merged_settings(current: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(DEFAULT_SETTINGS)
    result.update({k: v for k, v in current.items() if k in SETTING_SPECS})
    return result


def public_schema() -> Dict[str, Any]:
    return {
        "specs": SETTING_SPECS,
        "groups": GROUPS,
        "defaults": DEFAULT_SETTINGS,
    }
