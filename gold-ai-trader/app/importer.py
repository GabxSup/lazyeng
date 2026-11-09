"""Import MetaTrader 5 historical bars from CSV.

Supports the common MT5 History Center / chart exports: comma, tab or
semicolon separated, optional angle-bracket header, separate date/time columns
or a single timestamp column. MT5 timestamps are broker server time, so an
explicit UTC offset is applied unless the timestamp already carries one.
"""
from __future__ import annotations

import csv
import io
import math
import re
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from .models import Candle
from .util import timeframe_seconds

MIN_BARS = 260
MAX_ROWS = 300_000
_DATE_RE = re.compile(r"^\d{4}[./-]\d{1,2}[./-]\d{1,2}$|^\d{1,2}[./-]\d{1,2}[./-]\d{4}$")
_TIME_RE = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?$")
_EPOCH_RE = re.compile(r"^\d{10}(\d{3})?$")


class CSVParseError(ValueError):
    pass


def _normalise_cell(value: Any) -> str:
    return str(value or "").strip().strip('"').strip("'").strip("<>").strip()


def _float(value: Any) -> Optional[float]:
    text = _normalise_cell(value).replace(" ", "").replace("\u00a0", "")
    if not text or text.lower() in {"nan", "null", "none"}:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _parse_naive_datetime(date_text: str, time_text: str = "") -> Optional[datetime]:
    text = " ".join(part for part in (date_text.strip(), time_text.strip()) if part)
    text = text.replace("T", " ").strip()
    formats = (
        "%Y.%m.%d %H:%M:%S",
        "%Y.%m.%d %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%d.%m.%Y %H:%M:%S",
        "%d.%m.%Y %H:%M",
        "%Y.%m.%d",
        "%Y-%m-%d",
        "%Y/%m/%d",
    )
    for fmt in formats:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _parse_timestamp(cells: List[str], mapping: Optional[Dict[str, int]]) -> Optional[datetime]:
    date_text = ""
    time_text = ""
    if mapping and "date" in mapping:
        date_text = cells[mapping["date"]]
        time_text = cells[mapping["time"]] if "time" in mapping and mapping["time"] < len(cells) else ""
    elif mapping and "datetime" in mapping:
        date_text = cells[mapping["datetime"]]
    else:
        first = cells[0] if cells else ""
        second = cells[1] if len(cells) > 1 else ""
        if _EPOCH_RE.match(first):
            stamp = int(first)
            if len(first) == 13:
                stamp //= 1000
            return datetime.fromtimestamp(stamp, tz=timezone.utc)
        if _DATE_RE.match(first) and _TIME_RE.match(second):
            date_text, time_text = first, second
        else:
            date_text = first
            time_text = second if _TIME_RE.match(second) else ""
            if not _TIME_RE.match(second):
                time_text = ""
    if not date_text:
        return None
    text = " ".join(part for part in (date_text, time_text) if part).strip()
    # Preserve an explicit timezone if present.
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            return parsed.astimezone(timezone.utc)
    except ValueError:
        pass
    return _parse_naive_datetime(date_text, time_text)


def _header_map(cells: List[str]) -> Optional[Dict[str, int]]:
    normalised = [_normalise_cell(cell).lower() for cell in cells]
    labels = set(normalised)
    if not (labels & {"date", "time", "open", "high", "low", "close", "datetime", "timestamp"}):
        return None
    mapping: Dict[str, int] = {}
    aliases = {
        "date": "date",
        "time": "time",
        "datetime": "datetime",
        "timestamp": "datetime",
        "open": "open",
        "high": "high",
        "low": "low",
        "close": "close",
    }
    for index, label in enumerate(normalised):
        key = aliases.get(label)
        if key and key not in mapping:
            mapping[key] = index
    if "datetime" not in mapping and "date" not in mapping:
        return None
    if not all(k in mapping for k in ("open", "high", "low", "close")):
        return None
    return mapping


def _pick_delimiter(lines: List[str]) -> str:
    sample = lines[:80]
    best = ","
    best_score = -1
    for delimiter in (",", "\t", ";"):
        score = 0
        for line in sample:
            try:
                cells = next(csv.reader([line], delimiter=delimiter))
            except csv.Error:
                continue
            if len(cells) >= 6 and any(_float(cell) is not None for cell in cells):
                score += 1
        if score > best_score:
            best_score = score
            best = delimiter
    return best


def _infer_timeframe(rows: List[Candle]) -> Tuple[str, int]:
    if len(rows) < 3:
        return "M5", 300
    diffs = [
        int((rows[i].time - rows[i - 1].time).total_seconds())
        for i in range(1, min(len(rows), 250))
        if (rows[i].time - rows[i - 1].time).total_seconds() > 0
    ]
    interval = int(statistics.median(diffs)) if diffs else 300
    reverse = {60: "M1", 300: "M5", 900: "M15", 1800: "M30", 3600: "H1", 14400: "H4", 86400: "D1"}
    return reverse.get(interval, "M5"), interval


def parse_mt5_csv(
    text: str,
    *,
    symbol: str = "XAUUSD",
    timeframe: str = "M5",
    utc_offset_minutes: int = 0,
) -> Tuple[List[Candle], Dict[str, Any]]:
    if not isinstance(text, str) or not text.strip():
        raise CSVParseError("The CSV file is empty.")
    text = text.replace("\x00", "").lstrip("\ufeff")
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) < 2:
        raise CSVParseError("The CSV file does not contain enough rows.")
    delimiter = _pick_delimiter(lines)
    reader = csv.reader(io.StringIO("\n".join(lines)), delimiter=delimiter)
    raw_rows = [[_normalise_cell(cell) for cell in row] for row in reader]
    mapping: Optional[Dict[str, int]] = None
    start_index = 0
    for index, row in enumerate(raw_rows[:40]):
        candidate = _header_map(row)
        if candidate:
            mapping = candidate
            start_index = index + 1
            break
    if mapping is None:
        # Skip report preamble/metadata before the first plausible bar.
        for index, row in enumerate(raw_rows):
            if len(row) < 5:
                continue
            first = row[0] if row else ""
            second = row[1] if len(row) > 1 else ""
            if _EPOCH_RE.match(first) or _parse_naive_datetime(first, second if _TIME_RE.match(second) else ""):
                start_index = index
                break
        else:
            raise CSVParseError("No MT5 candle rows found. Export Date, Time, Open, High, Low, Close columns.")

    candles: List[Candle] = []
    offset = timedelta(minutes=max(-720, min(840, int(utc_offset_minutes))))
    skipped = 0
    for row in raw_rows[start_index:]:
        if not row or len(row) < 5:
            if row and any(row):
                skipped += 1
            continue
        try:
            stamp = _parse_timestamp(row, mapping)
            if stamp is None:
                skipped += 1
                continue
            if mapping:
                open_i, high_i, low_i, close_i = (
                    mapping["open"], mapping["high"], mapping["low"], mapping["close"]
                )
            else:
                first = row[0]
                second = row[1] if len(row) > 1 else ""
                offset_index = 2 if _DATE_RE.match(first) and _TIME_RE.match(second) else 1
                open_i, high_i, low_i, close_i = offset_index, offset_index + 1, offset_index + 2, offset_index + 3
            if max(open_i, high_i, low_i, close_i) >= len(row):
                skipped += 1
                continue
            open_p, high_p, low_p, close_p = (_float(row[i]) for i in (open_i, high_i, low_i, close_i))
            if None in (open_p, high_p, low_p, close_p):
                skipped += 1
                continue
            if min(open_p, high_p, low_p, close_p) <= 0:
                skipped += 1
                continue
            if high_p < max(open_p, close_p) - 0.0001 or low_p > min(open_p, close_p) + 0.0001:
                skipped += 1
                continue
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc) - offset
            else:
                stamp = stamp.astimezone(timezone.utc)
            candles.append(Candle(
                time=stamp,
                open=open_p,
                high=high_p,
                low=low_p,
                close=close_p,
                volume=0.0,
            ))
        except (ValueError, TypeError, IndexError):
            skipped += 1
    if len(candles) < MIN_BARS:
        raise CSVParseError(
            f"Only {len(candles)} valid bars parsed; at least {MIN_BARS} are required. "
            "Export a longer XAUUSD history and check the Date/Time/OHLC columns."
        )
    candles.sort(key=lambda candle: candle.time)
    deduped: List[Candle] = []
    for candle in candles:
        if deduped and candle.time == deduped[-1].time:
            deduped[-1] = candle
        else:
            deduped.append(candle)
    if len(deduped) > MAX_ROWS:
        raise CSVParseError(f"Too many rows ({len(deduped)}). Limit is {MAX_ROWS}.")
    inferred_timeframe, interval = _infer_timeframe(deduped)
    declared = timeframe_seconds(str(timeframe).upper())
    if abs(interval - declared) > max(5, declared * 0.2):
        timeframe = inferred_timeframe
    else:
        timeframe = str(timeframe).upper()
    meta = {
        "symbol": symbol,
        "timeframe": timeframe,
        "bars": len(deduped),
        "started_at": deduped[0].time.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "ended_at": deduped[-1].time.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "interval_seconds": interval,
        "utc_offset_minutes": utc_offset_minutes,
        "skipped_rows": skipped,
        "delimiter": "tab" if delimiter == "\t" else delimiter,
    }
    return deduped, meta
