"""Broker adapters: in-process paper simulation and MT5 Windows bridge."""
from __future__ import annotations

import json
import logging
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .config import Config
from .db import Store
from .market import SimulatedMarket
from .models import Account, ExecutionResult, Position, Quote
from .risk import COMMISSION_PER_LOT, CONTRACT_SIZE
from .util import iso, parse_iso, safe_float, utcnow

LOGGER = logging.getLogger(__name__)


class BrokerError(RuntimeError):
    pass


class BaseBroker:
    mode = "base"

    def connect(self) -> Dict[str, Any]:
        raise NotImplementedError

    def disconnect(self) -> None:
        raise NotImplementedError

    def account(self) -> Account:
        raise NotImplementedError

    def quote(self) -> Quote:
        raise NotImplementedError

    def candles(self, timeframe: str, limit: int) -> List[Dict[str, Any]]:
        raise NotImplementedError

    def positions(self) -> List[Position]:
        raise NotImplementedError

    def open_market(self, signal_id: str, side: str, lots: float, sl: float, tp: float,
                    confidence: float, reason: str) -> ExecutionResult:
        raise NotImplementedError

    def close_position(self, ticket: str, reason: str = "Manual close") -> Dict[str, Any]:
        raise NotImplementedError

    def close_all(self, reason: str = "Flatten all") -> List[Dict[str, Any]]:
        raise NotImplementedError

    def modify_protection(self, ticket: str, sl: float, tp: float) -> Dict[str, Any]:
        raise NotImplementedError

    def sweep_stops(self) -> List[Dict[str, Any]]:
        return []


class PaperBroker(BaseBroker):
    mode = "paper"

    def __init__(self, store: Store, config: Config) -> None:
        self.store = store
        self.config = config
        self.symbol = config.symbol
        self.connected = True
        self._lock = threading.RLock()
        spot = store.get_runtime("paper_spot")
        try:
            starting = float(spot) if spot else 4000.0
        except (TypeError, ValueError):
            starting = 4000.0
        self.market = SimulatedMarket(starting_price=starting)

    def connect(self) -> Dict[str, Any]:
        self.connected = True
        return {"ok": True, "status": "connected", "latency_ms": 0, "server": "AURUM Simulator"}

    def disconnect(self) -> None:
        # The simulator has no external connection; keep local state intact.
        self.connected = True

    def status(self) -> Dict[str, Any]:
        return {
            "connected": True,
            "status": "connected",
            "label": "Paper simulator",
            "server": "AURUM Simulator",
            "broker": "AURUM Sim",
            "account_type": "demo",
            "data_source": "Simulated ticks",
            "latency_ms": 0,
        }

    def account(self) -> Account:
        with self._lock:
            stored = self.store.get_paper_account()
            positions = self._positions_unlocked()
            unrealised = sum(p.pnl for p in positions)
            equity = stored["balance"] + unrealised
            gross_notional = sum(p.lots * CONTRACT_SIZE * p.current_price for p in positions)
            margin = gross_notional / 100.0
            free_margin = max(0.0, equity - margin)
            margin_level = (equity / margin * 100.0) if margin > 0 else 0.0
            peak = max(stored["peak_equity"], equity)
            account = Account(
                balance=stored["balance"],
                equity=equity,
                margin=margin,
                free_margin=free_margin,
                margin_level=margin_level,
                currency="USD",
                leverage=100,
                login=10001001,
                server="AURUM Simulator",
                name="Paper Trader",
                account_type="demo",
                broker="AURUM Sim",
            )
            self._peak = peak
            return account

    def _positions_unlocked(self) -> List[Position]:
        quote = self.market.quote()
        result: List[Position] = []
        for trade in self.store.get_open_trades(mode="paper"):
            side = trade["side"]
            current = quote.bid if side == "BUY" else quote.ask
            pnl = (current - trade["entry_price"]) * trade["lots"] * CONTRACT_SIZE
            if side == "SELL":
                pnl *= -1
            result.append(Position(
                ticket=str(trade["ticket"]),
                symbol=trade["symbol"],
                side=side,
                lots=float(trade["lots"]),
                entry_price=float(trade["entry_price"]),
                current_price=current,
                sl=float(trade["sl"]),
                tp=float(trade["tp"]),
                pnl=round(pnl - COMMISSION_PER_LOT * float(trade["lots"]), 2),
                opened_at=parse_iso(trade["opened_at"]) or utcnow(),
                confidence=float(trade.get("confidence") or 0),
                reason=trade.get("reason") or "",
                source="paper",
            ))
        return result

    def positions(self) -> List[Position]:
        self.market.refresh()
        with self._lock:
            return self._positions_unlocked()

    def quote(self) -> Quote:
        return self.market.refresh()

    def candles(self, timeframe: str, limit: int) -> List[Dict[str, Any]]:
        return self.market.candles(timeframe, limit)

    def open_market(self, signal_id: str, side: str, lots: float, sl: float, tp: float,
                    confidence: float, reason: str) -> ExecutionResult:
        if side not in {"BUY", "SELL"}:
            raise BrokerError("Invalid order side")
        if lots < 0.01:
            raise BrokerError("Lot size below broker minimum")
        with self._lock:
            quote = self.market.refresh()
            # Small deterministic adverse slippage; simulated execution is not
            # optimistic about the fill.
            slip = 0.02
            price = quote.ask + slip if side == "BUY" else quote.bid - slip
            price = round(price, 2)
            ticket = "SIM-" + uuid.uuid4().hex[:10].upper()
            self.store.insert_trade({
                "ticket": ticket,
                "signal_id": signal_id,
                "symbol": self.symbol,
                "side": side,
                "lots": round(lots, 2),
                "entry_price": price,
                "exit_price": None,
                "sl": round(sl, 2),
                "tp": round(tp, 2),
                "opened_at": iso(),
                "closed_at": None,
                "status": "open",
                "pnl": 0.0,
                "fees": round(COMMISSION_PER_LOT * lots, 2),
                "confidence": confidence,
                "reason": reason[:240],
                "close_reason": None,
                "mode": "paper",
                "source": "quant+ai" if "AI" in reason else "quant",
            })
            self.store.set_runtime("paper_spot", round(self.market.spot, 2))
            return ExecutionResult(ok=True, ticket=ticket, message="Paper market order filled", price=price, lots=round(lots, 2))

    def close_position(self, ticket: str, reason: str = "Manual close") -> Dict[str, Any]:
        with self._lock:
            trade = self.store.get_trade(ticket)
            if not trade or trade["mode"] != "paper" or trade["status"] != "open":
                raise BrokerError("Paper position not found")
            quote = self.market.refresh()
            side = trade["side"]
            slip = 0.02
            raw = quote.bid - slip if side == "BUY" else quote.ask + slip
            return self._settle_trade(trade, round(raw, 2), reason)

    def _settle_trade(self, trade: Dict[str, Any], exit_price: float, reason: str) -> Dict[str, Any]:
        side = trade["side"]
        lots = float(trade["lots"])
        gross = (exit_price - float(trade["entry_price"])) * lots * CONTRACT_SIZE
        if side == "SELL":
            gross *= -1
        fees = round(COMMISSION_PER_LOT * lots, 2)
        pnl = round(gross - fees, 2)
        closed = self.store.close_trade(str(trade["ticket"]), exit_price, pnl, fees, reason)
        if closed:
            account = self.store.get_paper_account()
            new_balance = round(account["balance"] + pnl, 2)
            self.store.save_paper_account(new_balance, account["peak_equity"])
            self.store.set_runtime("last_closed_at", iso())
            self.store.log(
                "INFO", "trade_closed",
                f"{side} {lots:.2f} lots closed at {exit_price:.2f} for {pnl:+.2f} USD",
                {"ticket": trade["ticket"], "pnl": pnl, "reason": reason},
            )
        return {
            "ok": bool(closed),
            "ticket": str(trade["ticket"]),
            "price": round(exit_price, 2),
            "pnl": pnl,
            "lots": lots,
            "reason": reason,
        }

    def close_all(self, reason: str = "Flatten all") -> List[Dict[str, Any]]:
        results = []
        for trade in self.store.get_open_trades(mode="paper"):
            try:
                results.append(self.close_position(str(trade["ticket"]), reason))
            except BrokerError:
                continue
        return results

    def modify_protection(self, ticket: str, sl: float, tp: float) -> Dict[str, Any]:
        with self._lock:
            trade = self.store.get_trade(ticket)
            if not trade or trade.get("mode") != "paper" or trade.get("status") != "open":
                raise BrokerError("Paper position not found")
            quote = self.market.refresh()
            side = trade["side"]
            if side == "BUY":
                valid = sl < quote.bid and tp > quote.bid
            else:
                valid = sl > quote.ask and tp < quote.ask
            if not valid:
                raise BrokerError("Proposed bracket would cross the current market price")
            if side == "BUY" and sl < float(trade["sl"]):
                raise BrokerError("A long stop cannot be moved backwards")
            if side == "SELL" and sl > float(trade["sl"]):
                raise BrokerError("A short stop cannot be moved backwards")
            return {"ok": True, "ticket": ticket, "sl": round(float(sl), 2), "tp": round(float(tp), 2)}

    def sweep_stops(self) -> List[Dict[str, Any]]:
        """Check bid/ask against each bracket. Stop loss is evaluated first."""
        closed: List[Dict[str, Any]] = []
        with self._lock:
            quote = self.market.refresh()
            for trade in self.store.get_open_trades(mode="paper"):
                side = trade["side"]
                sl = float(trade["sl"])
                tp = float(trade["tp"])
                price = quote.bid if side == "BUY" else quote.ask
                hit_sl = price <= sl if side == "BUY" else price >= sl
                hit_tp = price >= tp if side == "BUY" else price <= tp
                if hit_sl:
                    # If a candle gapped through both levels, assume the stop.
                    closed.append(self._settle_trade(trade, sl, "Stop loss"))
                elif hit_tp:
                    closed.append(self._settle_trade(trade, tp, "Take profit"))
        return closed

    def reset(self, balance: Optional[float] = None) -> Dict[str, float]:
        with self._lock:
            account = self.store.reset_paper(balance)
            self.store.set_runtime("paper_spot", round(self.market.spot, 2))
            return account

    def persist(self) -> None:
        self.market.persist_spot(self.store)

    def market_stats(self) -> Dict[str, float]:
        self.market.refresh()
        return self.market.session_stats()


class MT5BridgeBroker(BaseBroker):
    """HTTP client for ``bridge/mt5_bridge.py`` running beside MT5 on Windows.

    The Windows-only MetaTrader5 Python package cannot run inside a Linux
    container. Keeping the bridge separate is the only clean path to Docker.
    """

    mode = "mt5"

    def __init__(self, config: Config) -> None:
        self.config = config
        self.symbol = config.symbol
        self.connected = False
        self.armed = False
        self._status: Dict[str, Any] = {}
        self._last_error = ""
        self._latency_ms = 0

    def _request(self, path: str, method: str = "GET", body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if not self.config.bridge_configured:
            raise BrokerError("MT5 bridge is not configured (AURUM_BRIDGE_URL / AURUM_BRIDGE_TOKEN).")
        url = self.config.bridge_url + path
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {
            "Authorization": "Bearer " + self.config.bridge_token,
            "Accept": "application/json",
            "User-Agent": "AURUM-AI/1.0",
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        if self.armed:
            headers["X-Live-Confirm"] = "YES"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=self.config.bridge_timeout) as response:
                raw = response.read(2_000_000).decode("utf-8", "replace")
            self._latency_ms = round((time.monotonic() - started) * 1000)
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise BrokerError("Bridge returned an invalid response")
            if parsed.get("error"):
                raise BrokerError(str(parsed["error"])[:300])
            return parsed
        except urllib.error.HTTPError as exc:
            self._latency_ms = round((time.monotonic() - started) * 1000)
            detail = ""
            try:
                detail = str(json.loads(exc.read(100_000).decode("utf-8", "replace")).get("error", ""))[:240]
            except Exception:
                pass
            raise BrokerError(f"Bridge HTTP {exc.code}" + (f": {detail}" if detail else ""))
        except urllib.error.URLError as exc:
            raise BrokerError(f"Bridge unreachable: {getattr(exc, 'reason', 'network error')}")
        except (ValueError, TypeError) as exc:
            raise BrokerError(f"Bridge response error: {type(exc).__name__}")

    def connect(self) -> Dict[str, Any]:
        try:
            health = self._request("/health")
            bridge_symbol = str(health.get("symbol", ""))
            if bridge_symbol and bridge_symbol != self.symbol:
                raise BrokerError(
                    f"Bridge is pinned to {bridge_symbol}, dashboard expects {self.symbol}. "
                    "Set AURUM_SYMBOL and MT5_SYMBOL to the same broker symbol."
                )
            self.connected = True
            self._status = health
            self._last_error = ""
            self.config.bridge_configured  # ensure explicit property read
            self._status.update({"latency_ms": self._latency_ms})
            return {"ok": True, "latency_ms": self._latency_ms, "health": health}
        except BrokerError as exc:
            self.connected = False
            self._last_error = str(exc)
            self._status = {"connected": False, "error": str(exc)}
            raise

    def disconnect(self) -> None:
        self.connected = False
        self.armed = False
        self._status = {"connected": False}

    def status(self) -> Dict[str, Any]:
        return {
            "connected": self.connected,
            "status": "connected" if self.connected else "disconnected",
            "label": "MetaTrader 5 bridge",
            "server": self._status.get("server", ""),
            "broker": self._status.get("broker", ""),
            "account_type": self._status.get("account_type", ""),
            "data_source": "MT5 live feed",
            "latency_ms": self._latency_ms,
            "last_error": self._last_error,
            "bridge_version": self._status.get("version"),
        }

    def account(self) -> Account:
        data = self._request("/account")
        acct = data.get("account", data)
        if not self.connected:
            raise BrokerError("MT5 bridge is disconnected")
        return Account(
            balance=safe_float(acct.get("balance")),
            equity=safe_float(acct.get("equity")),
            margin=safe_float(acct.get("margin")),
            free_margin=safe_float(acct.get("free_margin")),
            margin_level=safe_float(acct.get("margin_level")),
            currency=str(acct.get("currency", "USD")),
            leverage=int(safe_float(acct.get("leverage"), 100)),
            login=int(safe_float(acct.get("login"), 0)),
            server=str(acct.get("server", "")),
            name=str(acct.get("name", "")),
            account_type=str(acct.get("account_type", "unknown")),
            broker=str(acct.get("broker", "")),
        )

    def quote(self) -> Quote:
        data = self._request(f"/quote?symbol={urllib.parse.quote(self.symbol)}")
        tick = data.get("quote", data)
        bid = safe_float(tick.get("bid"))
        ask = safe_float(tick.get("ask"))
        if bid <= 0 or ask <= 0:
            raise BrokerError("Bridge returned an invalid quote")
        ts = safe_float(tick.get("time"), 0)
        parsed_time = parse_iso(tick.get("time_iso"))
        if parsed_time is None:
            parsed_time = datetime.fromtimestamp(ts, tz=timezone.utc) if ts > 0 else utcnow()
        return Quote(
            symbol=self.symbol,
            bid=bid,
            ask=ask,
            time=parsed_time,
        )

    def candles(self, timeframe: str, limit: int) -> List[Dict[str, Any]]:
        query = urllib.parse.urlencode({"symbol": self.symbol, "timeframe": timeframe, "limit": max(10, min(limit, 1000))})
        data = self._request(f"/candles?{query}")
        candles = data.get("candles", [])
        if not isinstance(candles, list):
            raise BrokerError("Bridge returned invalid candles")
        return candles

    def positions(self) -> List[Position]:
        data = self._request(f"/positions?symbol={urllib.parse.quote(self.symbol)}")
        items = data.get("positions", [])
        result: List[Position] = []
        for item in items:
            side = str(item.get("side", "BUY")).upper()
            result.append(Position(
                ticket=str(item.get("ticket", "")),
                symbol=str(item.get("symbol", self.symbol)),
                side=side,
                lots=safe_float(item.get("lots")),
                entry_price=safe_float(item.get("entry_price")),
                current_price=safe_float(item.get("current_price")),
                sl=safe_float(item.get("sl")),
                tp=safe_float(item.get("tp")),
                pnl=safe_float(item.get("pnl")),
                opened_at=parse_iso(item.get("opened_at")) or utcnow(),
                confidence=safe_float(item.get("confidence")),
                reason=str(item.get("reason", "")),
                source="mt5",
            ))
        return result

    def open_market(self, signal_id: str, side: str, lots: float, sl: float, tp: float,
                    confidence: float, reason: str) -> ExecutionResult:
        if not self.connected:
            raise BrokerError("MT5 bridge is not connected")
        if not self.armed:
            raise BrokerError("Live execution is not armed")
        body = {
            "symbol": self.symbol,
            "side": side,
            "lots": round(lots, 2),
            "sl": round(sl, 2),
            "tp": round(tp, 2),
            "request_id": uuid.uuid4().hex,
            "comment": f"AURUM {signal_id[:8]}",
        }
        data = self._request("/order", method="POST", body=body)
        return ExecutionResult(
            ok=bool(data.get("ok")),
            ticket=str(data.get("ticket", "")),
            message=str(data.get("message", "MT5 order sent")),
            price=safe_float(data.get("price")),
            lots=safe_float(data.get("lots"), lots),
        )

    def close_position(self, ticket: str, reason: str = "Manual close") -> Dict[str, Any]:
        if not self.connected:
            raise BrokerError("MT5 bridge is not connected")
        if not self.armed:
            raise BrokerError("Live execution is not armed")
        data = self._request("/close", method="POST", body={"ticket": ticket, "request_id": uuid.uuid4().hex, "reason": reason[:80]})
        return {
            "ok": bool(data.get("ok")),
            "ticket": ticket,
            "price": safe_float(data.get("price")),
            "pnl": safe_float(data.get("profit")),
            "lots": safe_float(data.get("lots")),
            "reason": reason,
        }

    def close_all(self, reason: str = "Flatten all") -> List[Dict[str, Any]]:
        if not self.connected:
            raise BrokerError("MT5 bridge is not connected")
        if not self.armed:
            raise BrokerError("Live execution is not armed")
        data = self._request("/close_all", method="POST", body={"request_id": uuid.uuid4().hex, "reason": reason[:80]})
        results = []
        for item in data.get("results", []):
            results.append({
                "ok": bool(item.get("ok")),
                "ticket": str(item.get("ticket", "")),
                "price": safe_float(item.get("price")),
                "pnl": safe_float(item.get("profit")),
                "reason": reason,
            })
        return results

    def modify_protection(self, ticket: str, sl: float, tp: float) -> Dict[str, Any]:
        if not self.connected:
            raise BrokerError("MT5 bridge is not connected")
        if not self.armed:
            raise BrokerError("Live execution is not armed")
        data = self._request("/modify", method="POST", body={
            "ticket": str(ticket),
            "sl": round(float(sl), 2),
            "tp": round(float(tp), 2),
            "request_id": uuid.uuid4().hex,
        })
        return {
            "ok": bool(data.get("ok")),
            "ticket": ticket,
            "sl": safe_float(data.get("sl"), sl),
            "tp": safe_float(data.get("tp"), tp),
            "message": str(data.get("message", "MT5 protection updated")),
        }

    def history(self, days: int = 7) -> List[Dict[str, Any]]:
        data = self._request(f"/history?days={max(1, min(int(days), 30))}")
        items = data.get("closed", [])
        return items if isinstance(items, list) else []
