"""Local MetaTrader 4 Expert Advisor bridge.

The EA runs inside the MT4 terminal and polls this dashboard over HTTP on
localhost. It sends account/quote/position/candle snapshots and receives
commands. The dashboard queues commands and waits for the EA's next result
line, so execution stays inside MT4 where the broker terminal manages fills.

Protocol (plain text, one record per line):
  AURUM|1
  SERVERTIME|<epoch>
  ACCOUNT|<login>|balance|equity|margin|free|leverage|currency|name|server|company|demo|real
  SYMBOL|<symbol>
  QUOTE|<symbol>|<bid>|<ask> (legacy: QUOTE|<bid>|<ask>)
  POS|<ticket>|<0 buy/1 sell>|lots|open|current|sl|tp|profit|open_epoch|magic|comment
  CLOSED|<ticket>|side|<lots>|open|close|profit|commission|swap|open_epoch|close_epoch
  RESULT|<id>|<1 ok/0 fail>|<ticket>|<price>|<lots>|<profit>|<message>
  CANDLE|<id>|<tf>|<epoch>|<open>|<high>|<low>|<close>|<volume>

Commands returned by the dashboard:
  CMD|<id>|OPEN|<BUY/SELL>|<lots>|<sl>|<tp>|<symbol>
  CMD|<id>|CLOSE|<ticket>
  CMD|<id>|MODIFY|<ticket>|<sl>|<tp>
  CMD|<id>|CANDLES|<timeframe>|<count>
"""
from __future__ import annotations

import hmac
import logging
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Deque, Dict, List, Optional, Tuple

from .broker import BaseBroker, BrokerError
from .config import Config
from .models import Account, Candle, ExecutionResult, Position, Quote
from .util import iso, parse_iso, safe_float, utcnow

LOGGER = logging.getLogger(__name__)

EA_POLL_MAX_AGE_SECONDS = 6.0
EA_MAX_COMMANDS = 25
EA_MAX_CANDLES = 500
COMMAND_TIMEOUT = 20.0
CANDLE_TIMEOUT = 10.0
VERSION = "1.0"


class EAProtocolError(ValueError):
    pass


def _clean(value: Any, limit: int = 160) -> str:
    return str(value or "").replace("|", "/").replace("\r", " ").replace("\n", " ").strip()[:limit]


def _timestamp(epoch: Any, offset_seconds: int = 0) -> datetime:
    try:
        stamp = int(float(epoch)) - int(offset_seconds)
        return datetime.fromtimestamp(stamp, tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return utcnow()


class EABroker(BaseBroker):
    mode = "mt4"

    def __init__(self, config: Config) -> None:
        self.config = config
        self.symbol = config.symbol
        self.armed = False
        self._cond = threading.Condition(threading.RLock())
        self._account: Optional[Account] = None
        self._quote: Optional[Quote] = None
        self._positions: List[Position] = []
        self._closed: Dict[str, Dict[str, Any]] = {}
        self._last_poll: Optional[datetime] = None
        self._server_offset = 0
        self._snapshot_symbol = ""
        self._last_error = ""
        self._connected = False
        self._seq = 0
        self._out: Deque[str] = deque()
        self._pending: Dict[str, None] = {}
        self._results: Dict[str, Dict[str, Any]] = {}
        self._candle_cache: Dict[Tuple[str, int], Tuple[float, List[Dict[str, Any]]]] = {}
        self._polls = 0

    # ------------------------------------------------------------------
    # Status / handshake
    # ------------------------------------------------------------------
    @property
    def configured(self) -> bool:
        return bool(self.config.ea_token)

    def heartbeat_age(self) -> Optional[float]:
        with self._cond:
            if self._last_poll is None:
                return None
            return max(0.0, (utcnow() - self._last_poll).total_seconds())

    def _fresh_locked(self) -> bool:
        return (
            self._last_poll is not None
            and (utcnow() - self._last_poll).total_seconds() <= EA_POLL_MAX_AGE_SECONDS
        )

    def connect(self) -> Dict[str, Any]:
        if not self.configured:
            raise BrokerError(
                "MT4 EA access is not configured. Add AURUM_EA_TOKEN to the dashboard .env and restart."
            )
        with self._cond:
            if not self._fresh_locked() or self._account is None:
                raise BrokerError(
                    "The MT4 EA has not polled this dashboard yet. Attach AURUM_EA to XAUUSD in MT4 "
                    "and check Tools → Options → Expert Advisors → Allow WebRequest."
                )
            if self._snapshot_symbol and self._snapshot_symbol != self.symbol:
                raise BrokerError(
                    f"The EA is attached to {self._snapshot_symbol}, but AURUM_SYMBOL is {self.symbol}. "
                    "They must match."
                )
            self._connected = True
            return {"ok": True, "latency_ms": round((self.heartbeat_age() or 0) * 1000)}

    def disconnect(self) -> None:
        with self._cond:
            self._connected = False
            self._out.clear()
            self._pending.clear()
            self._cond.notify_all()

    def status(self) -> Dict[str, Any]:
        with self._cond:
            age = self.heartbeat_age()
            fresh = self._fresh_locked()
            acct = self._account
            return {
                "connected": bool(fresh and self._account is not None),
                "status": "connected" if fresh else "waiting",
                "label": "MetaTrader 4 EA",
                "server": acct.server if acct else "",
                "broker": acct.broker if acct else "",
                "account_type": acct.account_type if acct else "",
                "data_source": "MetaTrader 4 EA",
                "latency_ms": int((age or 0) * 1000),
                "heartbeat_age": round(age, 1) if age is not None else None,
                "last_seen": iso(self._last_poll) if self._last_poll else None,
                "last_error": self._last_error,
                "ea_symbol": self._snapshot_symbol,
                "ea_configured": self.configured,
                "ea_polls": self._polls,
            }

    def _ensure_ready_locked(self) -> None:
        if not self.configured:
            raise BrokerError("MT4 EA token is not configured on the dashboard.")
        if not self._fresh_locked() or self._account is None:
            self._connected = False
            raise BrokerError("MT4 EA heartbeat is stale. Check that the EA is attached and AutoTrading is enabled.")
        if self._snapshot_symbol and self._snapshot_symbol != self.symbol:
            raise BrokerError(
                f"EA symbol {self._snapshot_symbol} does not match configured symbol {self.symbol}."
            )

    # ------------------------------------------------------------------
    # Snapshot reads
    # ------------------------------------------------------------------
    def account(self) -> Account:
        with self._cond:
            self._ensure_ready_locked()
            if self._account is None:
                raise BrokerError("No MT4 account snapshot available.")
            return self._account

    def quote(self) -> Quote:
        with self._cond:
            self._ensure_ready_locked()
            if self._quote is None:
                raise BrokerError("No MT4 quote available.")
            return Quote(self._quote.symbol, self._quote.bid, self._quote.ask, time=utcnow())

    def positions(self) -> List[Position]:
        with self._cond:
            self._ensure_ready_locked()
            return list(self._positions)

    def candles(self, timeframe: str, limit: int) -> List[Dict[str, Any]]:
        timeframe = str(timeframe).upper()
        if timeframe not in {"M5", "M15", "M30", "H1"}:
            raise BrokerError("Unsupported timeframe for MT4 EA.")
        limit = max(20, min(int(limit), EA_MAX_CANDLES))
        key = (timeframe, limit)
        with self._cond:
            cached = self._candle_cache.get(key)
            if cached and time.monotonic() - cached[0] < 5.0:
                return list(cached[1])
        result = self._queue_command(f"CANDLES|{timeframe}|{limit}", CANDLE_TIMEOUT)
        candles = result.get("candles") or []
        if not candles:
            raise BrokerError(result.get("message") or "The EA returned no candles. Load the XAUUSD chart history.")
        with self._cond:
            self._candle_cache[key] = (time.monotonic(), candles)
        return list(candles)

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------
    def _next_id_locked(self) -> str:
        self._seq = (self._seq + 1) % 2_000_000_000
        return f"e{self._seq}"

    def _queue_command(self, command: str, timeout: float) -> Dict[str, Any]:
        with self._cond:
            self._ensure_ready_locked()
            if command.startswith(("OPEN", "CLOSE", "MODIFY")) and not self.armed:
                raise BrokerError("Live execution is not armed.")
            command_id = self._next_id_locked()
            line = f"CMD|{command_id}|{command}"
            self._pending[command_id] = None
            self._out.append(line)
            self._cond.notify_all()
            deadline = time.monotonic() + timeout
            while command_id not in self._results:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._pending.pop(command_id, None)
                    try:
                        self._out.remove(line)
                    except ValueError:
                        pass
                    raise BrokerError("MT4 EA command timed out. Check the Experts log and MT4 AutoTrading.")
                if not self._fresh_locked():
                    self._pending.pop(command_id, None)
                    raise BrokerError("MT4 EA stopped responding while a command was pending.")
                self._cond.wait(min(remaining, 1.0))
            result = self._results.pop(command_id, {})
            self._pending.pop(command_id, None)
            if not result.get("ok"):
                raise BrokerError(result.get("message") or "MT4 EA rejected the command.")
            return result

    def open_market(self, signal_id: str, side: str, lots: float, sl: float, tp: float,
                    confidence: float, reason: str) -> ExecutionResult:
        side = str(side).upper()
        if side not in {"BUY", "SELL"}:
            raise BrokerError("Invalid order side.")
        result = self._queue_command(
            f"OPEN|{side}|{round(float(lots), 2):.2f}|{round(float(sl), 2):.2f}|{round(float(tp), 2):.2f}|{self.symbol}",
            COMMAND_TIMEOUT,
        )
        ticket = str(result.get("ticket", ""))
        if not ticket:
            raise BrokerError(result.get("message") or "MT4 returned no order ticket.")
        return ExecutionResult(
            ok=True,
            ticket=ticket,
            message=result.get("message") or "MT4 EA order filled",
            price=safe_float(result.get("price")),
            lots=safe_float(result.get("lots"), lots),
        )

    def close_position(self, ticket: str, reason: str = "Manual close") -> Dict[str, Any]:
        result = self._queue_command(f"CLOSE|{ticket}", COMMAND_TIMEOUT)
        return {
            "ok": True,
            "ticket": ticket,
            "price": safe_float(result.get("price")),
            "pnl": safe_float(result.get("profit")),
            "lots": safe_float(result.get("lots")),
            "reason": reason,
        }

    def close_all(self, reason: str = "Flatten all") -> List[Dict[str, Any]]:
        tickets = [p.ticket for p in self.positions()]
        results: List[Dict[str, Any]] = []
        for ticket in tickets:
            try:
                results.append(self.close_position(ticket, reason))
            except BrokerError as exc:
                results.append({"ok": False, "ticket": ticket, "error": str(exc)})
        return results

    def modify_protection(self, ticket: str, sl: float, tp: float) -> Dict[str, Any]:
        result = self._queue_command(
            f"MODIFY|{ticket}|{round(float(sl), 2):.2f}|{round(float(tp), 2):.2f}",
            COMMAND_TIMEOUT,
        )
        return {
            "ok": True,
            "ticket": ticket,
            "sl": safe_float(result.get("sl"), sl),
            "tp": safe_float(result.get("tp"), tp),
            "message": result.get("message") or "MT4 protection updated",
        }

    def history(self, days: int = 7) -> List[Dict[str, Any]]:
        cutoff = utcnow() - timedelta(days=max(1, min(int(days), 30)))
        items = []
        with self._cond:
            for item in self._closed.values():
                closed_at = parse_iso(item.get("close_time"))
                if closed_at is None or closed_at >= cutoff:
                    items.append(dict(item))
        items.sort(key=lambda row: row.get("close_time") or "", reverse=True)
        return items

    def sweep_stops(self) -> List[Dict[str, Any]]:
        return []

    # ------------------------------------------------------------------
    # EA poll
    # ------------------------------------------------------------------
    def handle_poll(self, body: str) -> str:
        if not self.configured:
            raise EAProtocolError("MT4 EA bridge is disabled.")
        if not isinstance(body, str) or not body.strip():
            raise EAProtocolError("Empty EA snapshot.")
        if len(body.encode("utf-8", "ignore")) > 2 * 1024 * 1024:
            raise EAProtocolError("EA snapshot too large.")
        lines = [line.strip() for line in body.splitlines() if line.strip()]
        if not lines or not lines[0].startswith("AURUM|"):
            raise EAProtocolError("Invalid EA protocol header.")
        now = utcnow()
        server_epoch: Optional[float] = None
        account: Optional[Account] = None
        quote: Optional[Quote] = None
        symbol = ""
        positions: List[Position] = []
        closed_updates: Dict[str, Dict[str, Any]] = {}
        results: Dict[str, Dict[str, Any]] = {}
        candles_by_id: Dict[str, List[Dict[str, Any]]] = {}
        server_offset = 0
        for raw in lines:
            parts = raw.split("|")
            kind = parts[0].upper()
            try:
                if kind == "SERVERTIME" and len(parts) >= 2:
                    server_epoch = float(parts[1])
                elif kind == "ACCOUNT" and len(parts) >= 12:
                    login = int(float(parts[1]))
                    balance = safe_float(parts[2])
                    equity = safe_float(parts[3])
                    margin = safe_float(parts[4])
                    free = safe_float(parts[5])
                    leverage = int(float(parts[6] or 100))
                    account = Account(
                        balance=balance,
                        equity=equity,
                        margin=margin,
                        free_margin=free,
                        margin_level=(equity / margin * 100.0) if margin > 0 else 0.0,
                        currency=_clean(parts[7], 12) or "USD",
                        leverage=max(1, leverage),
                        login=login,
                        server=_clean(parts[9]),
                        name=_clean(parts[8]),
                        account_type=_clean(parts[11]).lower() if parts[11].lower() in {"demo", "real"} else "demo",
                        broker=_clean(parts[10]),
                    )
                elif kind == "SYMBOL" and len(parts) >= 2:
                    symbol = _clean(parts[1], 24).upper()
                elif kind == "QUOTE" and len(parts) >= 3:
                    if len(parts) >= 4:
                        quote_symbol = _clean(parts[1], 24).upper()
                        bid_text, ask_text = parts[2], parts[3]
                    else:
                        quote_symbol = symbol or self.symbol
                        bid_text, ask_text = parts[1], parts[2]
                    bid = safe_float(bid_text)
                    ask = safe_float(ask_text)
                    if bid > 0 and ask >= bid:
                        quote = Quote(quote_symbol, bid, ask, time=now)
                elif kind == "POS" and len(parts) >= 11:
                    side = "BUY" if int(float(parts[2])) == 0 else "SELL"
                    positions.append(Position(
                        ticket=_clean(parts[1], 40),
                        symbol=symbol or self.symbol,
                        side=side,
                        lots=safe_float(parts[3]),
                        entry_price=safe_float(parts[4]),
                        current_price=safe_float(parts[5]),
                        sl=safe_float(parts[6]),
                        tp=safe_float(parts[7]),
                        pnl=safe_float(parts[8]),
                        opened_at=_timestamp(parts[9], 0),
                        reason=_clean(parts[11]) if len(parts) > 11 else "",
                        source="mt4",
                    ))
                elif kind == "CLOSED" and len(parts) >= 11:
                    ticket = _clean(parts[1], 40)
                    closed_updates[ticket] = {
                        "ticket": ticket,
                        "exit_price": safe_float(parts[5]),
                        "profit": safe_float(parts[6]),
                        "commission": safe_float(parts[7]),
                        "swap": safe_float(parts[8]),
                        "close_time": iso(_timestamp(parts[10], 0)),
                    }
                elif kind == "CANDLE" and len(parts) >= 9:
                    request_id = _clean(parts[1], 40)
                    candles_by_id.setdefault(request_id, []).append({
                        "time": int(_timestamp(parts[3], 0).timestamp()),
                        "open": safe_float(parts[4]),
                        "high": safe_float(parts[5]),
                        "low": safe_float(parts[6]),
                        "close": safe_float(parts[7]),
                        "volume": safe_float(parts[8]),
                        "timeframe": _clean(parts[2], 8),
                    })
                elif kind == "RESULT" and len(parts) >= 3:
                    request_id = _clean(parts[1], 40)
                    result = {
                        "ok": str(parts[2]).strip() in {"1", "true", "TRUE", "yes"},
                        "ticket": _clean(parts[3], 40) if len(parts) > 3 else "",
                        "price": safe_float(parts[4]) if len(parts) > 4 else 0.0,
                        "lots": safe_float(parts[5]) if len(parts) > 5 else 0.0,
                        "profit": safe_float(parts[6]) if len(parts) > 6 else 0.0,
                        "message": "|".join(parts[7:])[:240] if len(parts) > 7 else "",
                    }
                    results[request_id] = result
            except (IndexError, TypeError, ValueError):
                continue
        if account is None or quote is None:
            raise EAProtocolError("EA snapshot is missing ACCOUNT or QUOTE record.")
        symbol = symbol or self.symbol
        if symbol != self.symbol:
            raise EAProtocolError(f"EA is attached to {symbol}, dashboard expects {self.symbol}.")
        if quote is not None and quote.symbol != symbol:
            raise EAProtocolError(f"EA quote is for {quote.symbol}, but its chart symbol is {symbol}.")
        offset = 0
        if server_epoch:
            raw_offset = server_epoch - now.timestamp()
            offset = int(round(raw_offset / 900.0) * 900)
            offset = max(-12 * 3600, min(14 * 3600, offset))
        # Recompute time fields with the offset (the first pass used zero).
        for pos in positions:
            if pos.opened_at:
                pos.opened_at = pos.opened_at - timedelta(seconds=offset)
        for item in closed_updates.values():
            close_time = parse_iso(item.get("close_time"))
            if close_time:
                item["close_time"] = iso(close_time - timedelta(seconds=offset))
        for rows in candles_by_id.values():
            for candle in rows:
                candle["time"] = int(candle["time"] - offset)
        with self._cond:
            self._account = account
            self._quote = quote
            self._positions = positions
            self._snapshot_symbol = symbol
            self._server_offset = offset
            self._last_poll = now
            self._connected = True
            self._polls += 1
            for ticket, item in closed_updates.items():
                self._closed[ticket] = item
            for request_id, result in results.items():
                if request_id in candles_by_id:
                    result["candles"] = candles_by_id[request_id]
                if request_id in self._pending:
                    self._results[request_id] = result
            # A candle result may reference a command that is still pending.
            for request_id, rows in candles_by_id.items():
                if request_id in self._results and "candles" not in self._results[request_id]:
                    self._results[request_id]["candles"] = rows
            commands = []
            while self._out and len(commands) < EA_MAX_COMMANDS:
                commands.append(self._out.popleft())
            self._cond.notify_all()
        return "\n".join(commands)

    def last_snapshot_symbol(self) -> str:
        return self._snapshot_symbol
