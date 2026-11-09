"""SQLite persistence for accounts, trades, signals, logs and analytics."""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .settings import DEFAULT_SETTINGS, merged_settings, validate_updates
from .util import iso, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_account (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    initial_balance REAL NOT NULL,
    balance REAL NOT NULL,
    peak_equity REAL NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket TEXT NOT NULL UNIQUE,
    signal_id TEXT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    lots REAL NOT NULL,
    entry_price REAL NOT NULL,
    exit_price REAL,
    sl REAL NOT NULL,
    tp REAL NOT NULL,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    status TEXT NOT NULL,
    pnl REAL NOT NULL DEFAULT 0,
    fees REAL NOT NULL DEFAULT 0,
    confidence REAL NOT NULL DEFAULT 0,
    reason TEXT NOT NULL DEFAULT '',
    close_reason TEXT,
    mode TEXT NOT NULL DEFAULT 'paper',
    source TEXT NOT NULL DEFAULT 'quant',
    initial_sl REAL,
    initial_tp REAL,
    tp_extensions INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status, mode);
CREATE INDEX IF NOT EXISTS idx_trades_opened ON trades(opened_at);

CREATE TABLE IF NOT EXISTS signals (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    symbol TEXT NOT NULL,
    action TEXT NOT NULL,
    confidence REAL NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'new'
);
CREATE INDEX IF NOT EXISTS idx_signals_created ON signals(created_at);

CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    level TEXT NOT NULL,
    event TEXT NOT NULL,
    message TEXT NOT NULL,
    meta TEXT
);
CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs(ts);

CREATE TABLE IF NOT EXISTS equity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    equity REAL NOT NULL,
    balance REAL NOT NULL,
    drawdown_pct REAL NOT NULL DEFAULT 0,
    mode TEXT NOT NULL DEFAULT 'paper'
);
CREATE INDEX IF NOT EXISTS idx_equity_ts ON equity(ts);

CREATE TABLE IF NOT EXISTS runtime (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS backtests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    request TEXT NOT NULL,
    result TEXT NOT NULL
);
"""


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _loads(value: Optional[str], default: Any = None) -> Any:
    if value is None:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


class Store:
    def __init__(self, path: Path, initial_balance: float = 10000.0) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.initial_balance = initial_balance
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, timeout=15.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=15000")
        self._conn.executescript(SCHEMA)
        self._migrate()
        self._conn.commit()
        self._seed()

    def _migrate(self) -> None:
        columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(trades)").fetchall()}
        additions = {
            "initial_sl": "REAL",
            "initial_tp": "REAL",
            "tp_extensions": "INTEGER NOT NULL DEFAULT 0",
        }
        for name, ddl in additions.items():
            if name not in columns:
                self._conn.execute(f"ALTER TABLE trades ADD COLUMN {name} {ddl}")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _seed(self) -> None:
        now = iso()
        with self._lock, self._conn:
            for key, value in DEFAULT_SETTINGS.items():
                self._conn.execute(
                    "INSERT OR IGNORE INTO settings(key, value, updated_at) VALUES (?, ?, ?)",
                    (key, _json(value), now),
                )
            self._conn.execute(
                "INSERT OR IGNORE INTO paper_account(id, initial_balance, balance, peak_equity, updated_at) "
                "VALUES (1, ?, ?, ?, ?)",
                (self.initial_balance, self.initial_balance, self.initial_balance, now),
            )

    # ------------------------------------------------------------------
    # Settings and runtime state
    # ------------------------------------------------------------------
    def get_settings(self) -> Dict[str, Any]:
        with self._lock:
            rows = self._conn.execute("SELECT key, value FROM settings").fetchall()
        stored = {row["key"]: _loads(row["value"], DEFAULT_SETTINGS.get(row["key"])) for row in rows}
        return merged_settings(stored)

    def update_settings(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        clean = validate_updates(updates)
        now = iso()
        with self._lock, self._conn:
            for key, value in clean.items():
                self._conn.execute(
                    "INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                    (key, _json(value), now),
                )
        return self.get_settings()

    def get_runtime(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._conn.execute("SELECT value FROM runtime WHERE key = ?", (key,)).fetchone()
        return _loads(row["value"], default) if row else default

    def set_runtime(self, key: str, value: Any) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO runtime(key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, _json(value), iso()),
            )

    # ------------------------------------------------------------------
    # Paper account and equity
    # ------------------------------------------------------------------
    def get_paper_account(self) -> Dict[str, float]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM paper_account WHERE id = 1").fetchone()
        if row is None:
            self._seed()
            return self.get_paper_account()
        return {
            "initial_balance": float(row["initial_balance"]),
            "balance": float(row["balance"]),
            "peak_equity": float(row["peak_equity"]),
            "updated_at": row["updated_at"],
        }

    def save_paper_account(self, balance: float, peak_equity: float) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE paper_account SET balance = ?, peak_equity = ?, updated_at = ? WHERE id = 1",
                (float(balance), float(peak_equity), iso()),
            )

    def reset_paper(self, initial_balance: Optional[float] = None) -> Dict[str, float]:
        balance = float(initial_balance if initial_balance is not None else self.initial_balance)
        now = iso()
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM trades WHERE mode = 'paper'")
            self._conn.execute("DELETE FROM equity WHERE mode = 'paper'")
            self._conn.execute("DELETE FROM signals")
            self._conn.execute(
                "UPDATE paper_account SET initial_balance = ?, balance = ?, peak_equity = ?, updated_at = ? WHERE id = 1",
                (balance, balance, balance, now),
            )
            for key in ("last_signal_id", "last_analysis_ts", "last_closed_at", "day_start_equity"):
                self._conn.execute("DELETE FROM runtime WHERE key = ?", (key,))
        return self.get_paper_account()

    def record_equity(self, equity: float, balance: float, drawdown_pct: float, mode: str = "paper") -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO equity(ts, equity, balance, drawdown_pct, mode) VALUES (?, ?, ?, ?, ?)",
                (iso(), float(equity), float(balance), float(drawdown_pct), mode),
            )
            self._conn.execute(
                "DELETE FROM equity WHERE id IN (SELECT id FROM equity WHERE mode = ? ORDER BY id DESC LIMIT -1 OFFSET 5000)",
                (mode,),
            )

    def equity_series(self, limit: int = 180, mode: Optional[str] = None) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 1000))
        with self._lock:
            if mode:
                rows = self._conn.execute(
                    "SELECT ts, equity, balance, drawdown_pct FROM equity WHERE mode = ? ORDER BY id DESC LIMIT ?",
                    (mode, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT ts, equity, balance, drawdown_pct FROM equity ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [dict(row) for row in reversed(rows)]

    # ------------------------------------------------------------------
    # Trades
    # ------------------------------------------------------------------
    def insert_trade(self, trade: Dict[str, Any]) -> int:
        columns = (
            "ticket", "signal_id", "symbol", "side", "lots", "entry_price", "exit_price",
            "sl", "tp", "opened_at", "closed_at", "status", "pnl", "fees", "confidence",
            "reason", "close_reason", "mode", "source", "initial_sl", "initial_tp", "tp_extensions",
        )
        payload = {key: trade.get(key) for key in columns}
        if payload.get("initial_sl") is None:
            payload["initial_sl"] = trade.get("sl")
        if payload.get("initial_tp") is None:
            payload["initial_tp"] = trade.get("tp")
        if payload.get("tp_extensions") is None:
            payload["tp_extensions"] = 0
        payload.setdefault("status", "open")
        payload.setdefault("pnl", 0.0)
        payload.setdefault("fees", 0.0)
        payload.setdefault("confidence", 0.0)
        payload.setdefault("reason", "")
        payload.setdefault("mode", "paper")
        payload.setdefault("source", "quant")
        payload.setdefault("opened_at", iso())
        placeholders = ", ".join("?" for _ in columns)
        with self._lock, self._conn:
            cursor = self._conn.execute(
                f"INSERT INTO trades({', '.join(columns)}) VALUES ({placeholders})",
                tuple(payload[key] for key in columns),
            )
            return int(cursor.lastrowid)

    def update_open_trade(self, ticket: str, **fields: Any) -> None:
        allowed = {"sl", "tp", "lots", "confidence", "reason", "initial_sl", "initial_tp", "tp_extensions"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        assignments = ", ".join(f"{key} = ?" for key in updates)
        with self._lock, self._conn:
            self._conn.execute(
                f"UPDATE trades SET {assignments} WHERE ticket = ? AND status = 'open'",
                (*updates.values(), ticket),
            )

    def update_protection(self, ticket: str, sl: float, tp: float, tp_extensions: int) -> bool:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE trades SET sl = ?, tp = ?, tp_extensions = ? WHERE ticket = ? AND status = 'open'",
                (float(sl), float(tp), int(tp_extensions), ticket),
            )
            return cursor.rowcount > 0

    def close_trade(self, ticket: str, exit_price: float, pnl: float, fees: float, reason: str, closed_at: Optional[str] = None) -> bool:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE trades SET status = 'closed', exit_price = ?, pnl = ?, fees = ?, close_reason = ?, "
                "closed_at = ? WHERE ticket = ? AND status = 'open'",
                (float(exit_price), float(pnl), float(fees), reason, closed_at or iso(), ticket),
            )
            return cursor.rowcount > 0

    def get_open_trades(self, mode: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            if mode:
                rows = self._conn.execute(
                    "SELECT * FROM trades WHERE status = 'open' AND mode = ? ORDER BY id", (mode,)
                ).fetchall()
            else:
                rows = self._conn.execute("SELECT * FROM trades WHERE status = 'open' ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    def get_trade(self, ticket: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM trades WHERE ticket = ?", (ticket,)).fetchone()
        return dict(row) if row else None

    def recent_trades(self, limit: int = 50, mode: Optional[str] = None) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._lock:
            if mode:
                rows = self._conn.execute(
                    "SELECT * FROM trades WHERE mode = ? ORDER BY id DESC LIMIT ?", (mode, limit)
                ).fetchall()
            else:
                rows = self._conn.execute("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def closed_trades(self, mode: Optional[str] = None, since: Optional[str] = None) -> List[Dict[str, Any]]:
        clauses = ["status = 'closed'"]
        params: List[Any] = []
        if mode:
            clauses.append("mode = ?")
            params.append(mode)
        if since:
            clauses.append("closed_at >= ?")
            params.append(since)
        query = "SELECT * FROM trades WHERE " + " AND ".join(clauses) + " ORDER BY id"
        with self._lock:
            rows = self._conn.execute(query, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def realised_pnl(self, mode: Optional[str] = None, since: Optional[str] = None) -> float:
        clauses = ["status = 'closed'"]
        params: List[Any] = []
        if mode:
            clauses.append("mode = ?")
            params.append(mode)
        if since:
            clauses.append("closed_at >= ?")
            params.append(since)
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(pnl), 0) AS total FROM trades WHERE " + " AND ".join(clauses), tuple(params)
            ).fetchone()
        return float(row["total"] or 0.0)

    def last_close_time(self) -> Optional[str]:
        with self._lock:
            row = self._conn.execute(
                "SELECT closed_at FROM trades WHERE status = 'closed' ORDER BY closed_at DESC LIMIT 1"
            ).fetchone()
        return row["closed_at"] if row else None

    # ------------------------------------------------------------------
    # Signals and logs
    # ------------------------------------------------------------------
    def save_signal(self, signal: Dict[str, Any]) -> None:
        now = signal.get("created_at") or iso()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO signals(id, created_at, symbol, action, confidence, payload, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload, status=excluded.status",
                (
                    signal["id"], now, signal.get("symbol", "XAUUSD"), signal.get("action", "WAIT"),
                    float(signal.get("confidence", 0.0)), _json(signal), signal.get("status", "new"),
                ),
            )
            self._conn.execute(
                "DELETE FROM signals WHERE id NOT IN (SELECT id FROM signals ORDER BY created_at DESC LIMIT 500)"
            )

    def latest_signal(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute("SELECT payload FROM signals ORDER BY created_at DESC, rowid DESC LIMIT 1").fetchone()
        return _loads(row["payload"]) if row else None

    def set_signal_status(self, signal_id: str, status: str) -> None:
        with self._lock, self._conn:
            row = self._conn.execute("SELECT payload FROM signals WHERE id = ?", (signal_id,)).fetchone()
            if not row:
                return
            payload = _loads(row["payload"], {})
            payload["status"] = status
            self._conn.execute(
                "UPDATE signals SET status = ?, payload = ? WHERE id = ?", (status, _json(payload), signal_id)
            )

    def log(self, level: str, event: str, message: str, meta: Optional[Dict[str, Any]] = None) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO logs(ts, level, event, message, meta) VALUES (?, ?, ?, ?, ?)",
                (iso(), str(level).upper(), event, message, _json(meta) if meta else None),
            )
            self._conn.execute("DELETE FROM logs WHERE id NOT IN (SELECT id FROM logs ORDER BY id DESC LIMIT 3000)")

    def logs(self, limit: int = 60) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._lock:
            rows = self._conn.execute("SELECT * FROM logs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        result = []
        for row in reversed(rows):
            item = dict(row)
            item["meta"] = _loads(item.get("meta"), {})
            result.append(item)
        return result

    # ------------------------------------------------------------------
    # Backtests
    # ------------------------------------------------------------------
    def save_backtest(self, request: Dict[str, Any], result: Dict[str, Any]) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO backtests(created_at, request, result) VALUES (?, ?, ?)",
                (iso(), _json(request), _json(result)),
            )
            self._conn.execute(
                "DELETE FROM backtests WHERE id NOT IN (SELECT id FROM backtests ORDER BY id DESC LIMIT 10)"
            )

    def latest_backtest(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute("SELECT result FROM backtests ORDER BY id DESC LIMIT 1").fetchone()
        return _loads(row["result"]) if row else None
