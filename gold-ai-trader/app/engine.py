"""The autonomous trading engine and its dashboard state assembly."""
from __future__ import annotations

import hmac
import logging
import threading
import time
from dataclasses import asdict
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .ai import AIAnalyst
from .backtest import run_backtest
from .brackets import plan_bracket_update
from .broker import BaseBroker, BrokerError, MT5BridgeBroker, PaperBroker
from .config import APP_NAME, APP_VERSION, Config
from .db import Store
from .ea import EABroker, EAProtocolError
from .importer import CSVParseError, parse_mt5_csv
from .metrics import daily_realised, public_trade, trade_stats
from .models import Account, Position, Quote, RiskAssessment, Signal
from .risk import CONTRACT_SIZE, RiskContext, RiskGovernor, open_risk
from .settings import public_schema
from .strategy import QuantStrategy
from .util import iso, parse_iso, utcnow

LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL_SECONDS = 20
PAUSED_SCAN_INTERVAL_SECONDS = 120
MARKET_TICK_SECONDS = 2
BRACKET_CHECK_INTERVAL_SECONDS = 5
BRACKET_TICKET_COOLDOWN_SECONDS = 12
EQUITY_SNAPSHOT_SECONDS = 30
LIVE_ARM_HOURS = 12
EXTERNAL_MODES = {"mt5", "mt4"}


class EngineError(RuntimeError):
    pass


class TradingEngine:
    def __init__(self, config: Config, store: Store) -> None:
        self.config = config
        self.store = store
        self.settings = store.get_settings()
        self.paper = PaperBroker(store, config)
        self.mt5: Optional[MT5BridgeBroker] = MT5BridgeBroker(config) if config.bridge_configured else None
        self.ea: Optional[EABroker] = EABroker(config) if config.ea_token else None
        self.strategy = QuantStrategy(config.symbol)
        self.risk = RiskGovernor(config.symbol)
        self.ai = AIAnalyst(config)
        self.mode = "paper"
        self.connected = True
        self.bot_running = False
        self.kill_switch = bool(store.get_runtime("kill_switch", False))
        self._armed_until: Optional[datetime] = None
        self._lock = threading.RLock()
        self._scan_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._account: Optional[Account] = None
        self._quote: Optional[Quote] = None
        self._positions: List[Position] = []
        self._last_signal: Optional[Signal] = None
        self._last_assessment: Optional[RiskAssessment] = None
        self._last_error = ""
        self._last_error_log_at = 0.0
        self._last_scan_mono = 0.0
        self._last_scan_at: Optional[datetime] = None
        self._last_scan_log_at = 0.0
        self._last_candidate_log: Dict[str, Any] = {}
        self._needs_initial_scan = True
        self._scan_requested = False
        self._last_history_sync = 0.0
        self._last_bracket_check = 0.0
        self._bracket_ticket_until: Dict[str, float] = {}
        self._started_at = utcnow()
        self._last_tick_at = utcnow()
        self._scans = 0
        self._scan_errors = 0
        self._latest_backtest = store.latest_backtest()
        self._refresh_count = 0
        self.store.set_runtime("engine_mode", self.mode)
        self.store.set_runtime("bot_running", False)
        self.store.log("INFO", "engine_start", f"{APP_NAME} engine initialised in paper mode.")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        # Prime the UI quickly; the heavier initial analysis runs in the loop.
        try:
            self.refresh()
            self._seed_equity()
        except Exception as exc:
            LOGGER.warning("Initial market refresh failed: %s", type(exc).__name__)
        self._thread = threading.Thread(target=self._loop, name="aurum-engine", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        self.paper.persist()
        self.store.log("INFO", "engine_stop", "AURUM engine stopped.")

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:
                LOGGER.exception("Unhandled engine loop error")
                self._set_error(f"Engine error: {type(exc).__name__}")
            self._stop.wait(MARKET_TICK_SECONDS)

    def _tick(self) -> None:
        self._last_tick_at = utcnow()
        refreshed = self.refresh()
        if self.mode == "paper" and refreshed:
            closed = self.paper.sweep_stops()
            if closed:
                self.refresh()
        if self.mode in EXTERNAL_MODES and refreshed:
            self._reconcile_live_trades()
        if refreshed:
            self._manage_brackets()
        self._check_arm_expiry()
        self._record_equity_if_due()
        if self.mode == "paper":
            self.paper.persist()

        now_mono = time.monotonic()
        should_scan = False
        if self._scan_requested or self._needs_initial_scan:
            should_scan = True
        elif self.bot_running and now_mono - self._last_scan_mono >= SCAN_INTERVAL_SECONDS:
            should_scan = True
        elif not self.bot_running and now_mono - self._last_scan_mono >= PAUSED_SCAN_INTERVAL_SECONDS:
            # Keep the displayed signal and guardrail evaluation fresh while
            # auto-trading is paused. These scans can never execute an order.
            should_scan = True
        if should_scan:
            self._scan_requested = False
            self._needs_initial_scan = False
            try:
                self._analyze(execute=self.bot_running)
            except Exception as exc:
                self._scan_errors += 1
                self._set_error(f"Analysis failed: {type(exc).__name__}")
                LOGGER.exception("Analysis failed")
                if self.bot_running:
                    self.pause_bot(reason=f"Analysis error: {type(exc).__name__}")
            finally:
                # A failed scan must also advance the clock; otherwise a broker
                # outage would trigger a retry every tick instead of on cadence.
                self._last_scan_mono = time.monotonic()

    # ------------------------------------------------------------------
    # Market data and bookkeeping
    # ------------------------------------------------------------------
    @property
    def active_broker(self) -> BaseBroker:
        if self.mode == "mt5":
            if self.mt5 is None:
                raise EngineError("MT5 bridge is not configured")
            return self.mt5
        if self.mode == "mt4":
            if self.ea is None:
                raise EngineError("MT4 EA bridge is not configured")
            return self.ea
        return self.paper

    @property
    def trade_mode(self) -> str:
        """Trades are stored as paper/live; engine modes are paper/mt5/mt4."""
        return "live" if self.mode in EXTERNAL_MODES else "paper"

    @property
    def platform_label(self) -> str:
        return {"mt5": "MetaTrader 5", "mt4": "MetaTrader 4", "paper": "Paper"}.get(self.mode, self.mode)

    def refresh(self) -> bool:
        try:
            if self.mode == "paper":
                account = self.paper.account()
                quote = self.paper.quote()
                positions = self.paper.positions()
                # The simulator advances by a few cents between the account and
                # position reads. Recompute the headline equity from the same
                # position snapshot so the dashboard is internally consistent.
                unrealised = sum(position.pnl for position in positions)
                account.equity = account.balance + unrealised
                account.margin = sum(position.lots * CONTRACT_SIZE * position.current_price for position in positions) / float(account.leverage or 100)
                account.free_margin = max(0.0, account.equity - account.margin)
                account.margin_level = (account.equity / account.margin * 100.0) if account.margin > 0 else 0.0
                self.connected = True
            elif self.mode == "mt5":
                if self.mt5 is None or not self.mt5.connected:
                    self.connected = False
                    return False
                account = self.mt5.account()
                quote = self.mt5.quote()
                age = (utcnow() - quote.time).total_seconds()
                if age > 180:
                    raise BrokerError("MT5 quote is stale (>180s)")
                positions = self.mt5.positions()
                self.connected = True
                self._last_error = ""
            else:
                if self.ea is None or not self.ea.configured:
                    self.connected = False
                    return False
                account = self.ea.account()
                quote = self.ea.quote()
                positions = self.ea.positions()
                self.connected = True
                self._last_error = ""
            self._account = account
            self._quote = quote
            self._positions = positions
            self._refresh_count += 1
            self._update_equity_baselines(account)
            return True
        except Exception as exc:
            self.connected = False
            self._set_error(str(exc)[:300])
            if self.mode in EXTERNAL_MODES and self.bot_running:
                self.pause_bot(reason="Broker connection lost")
            return False

    def _update_equity_baselines(self, account: Account) -> None:
        peak_key = f"peak_equity_{self.mode}"
        stored_peak = self.store.get_runtime(peak_key)
        try:
            peak = max(float(stored_peak), account.equity)
        except (TypeError, ValueError):
            peak = account.equity
        if peak <= 0:
            peak = account.equity
        if abs(float(stored_peak or 0.0) - peak) > 0.001:
            self.store.set_runtime(peak_key, round(peak, 2))
        if self.mode == "paper":
            try:
                paper = self.store.get_paper_account()
                if paper["peak_equity"] < peak:
                    self.store.save_paper_account(paper["balance"], peak)
            except Exception:
                pass
        today = utcnow().date().isoformat()
        date_key = f"day_start_date_{self.mode}"
        equity_key = f"day_start_equity_{self.mode}"
        stored_date = self.store.get_runtime(date_key)
        if stored_date != today:
            self.store.set_runtime(date_key, today)
            self.store.set_runtime(equity_key, round(account.equity, 2))
        elif self.store.get_runtime(equity_key) is None:
            self.store.set_runtime(equity_key, round(account.equity, 2))

    def _record_equity_if_due(self, force: bool = False) -> None:
        if self._account is None or not self.connected:
            return
        last_raw = self.store.get_runtime("last_equity_snapshot")
        last = parse_iso(last_raw) if isinstance(last_raw, str) else None
        now = utcnow()
        if not force and last and (now - last).total_seconds() < EQUITY_SNAPSHOT_SECONDS:
            return
        peak = self._peak_equity()
        drawdown = (peak - self._account.equity) / peak * 100.0 if peak > 0 else 0.0
        self.store.record_equity(self._account.equity, self._account.balance, drawdown, self.mode)
        self.store.set_runtime("last_equity_snapshot", iso(now))

    def _seed_equity(self) -> None:
        if self._account is None:
            return
        series = self.store.equity_series(1, self.mode)
        if not series:
            self._record_equity_if_due(force=True)

    def _peak_equity(self) -> float:
        stored = self.store.get_runtime(f"peak_equity_{self.mode}")
        try:
            peak = float(stored)
        except (TypeError, ValueError):
            peak = 0.0
        if self._account is not None:
            peak = max(peak, self._account.equity)
        return peak

    def _day_start_equity(self) -> float:
        stored = self.store.get_runtime(f"day_start_equity_{self.mode}")
        try:
            value = float(stored)
            if value > 0:
                return value
        except (TypeError, ValueError):
            pass
        return self._account.equity if self._account else 0.0

    def _set_error(self, message: str) -> None:
        self._last_error = message
        now = time.monotonic()
        if now - self._last_error_log_at > 30:
            self._last_error_log_at = now
            self.store.log("ERROR", "engine_error", message[:500])

    # ------------------------------------------------------------------
    # Signals and execution
    # ------------------------------------------------------------------
    def analyze_now(self) -> Dict[str, Any]:
        # Run synchronously so the "Analyze now" button feels immediate. This
        # deliberate manual action never executes an order.
        self.refresh()
        self._analyze(execute=False)
        self._needs_initial_scan = False
        return self.dashboard_state()

    def _analyze(self, execute: bool = False) -> Optional[Signal]:
        if not self._scan_lock.acquire(blocking=False):
            return self._last_signal
        try:
            if not self.refresh():
                return None
            if self._account is None or self._quote is None:
                return None
            self.settings = self.store.get_settings()
            timeframe = self.settings["timeframe"]
            try:
                candles = self.active_broker.candles(timeframe, 320)
            except Exception as exc:
                self._set_error(f"Candle feed unavailable: {str(exc)[:220]}")
                return None
            if not candles or len(candles) < 30:
                return None
            closed = candles[:-1]
            signal = self.strategy.evaluate(
                closed_candles=closed,
                quote=self._quote,
                timeframe=timeframe,
                min_confidence=float(self.settings["min_confidence"]),
                rr_ratio=float(self.settings["rr_ratio"]),
                atr_stop_mult=float(self.settings["atr_stop_mult"]),
            )
            if signal.action in {"BUY", "SELL"} and self.settings.get("ai_overlay", True):
                review = self.ai.review(signal, enabled=True)
                signal = self.strategy.apply_ai(signal, review, float(self.settings["min_confidence"]))
                if review.get("status") == "error":
                    self.store.log("WARN", "ai_error", "AI overlay unavailable; quant signal retained.", {"detail": review.get("detail")})

            self._last_signal = signal
            self._last_scan_mono = time.monotonic()
            self._last_scan_at = utcnow()
            self._scans += 1
            self.store.save_signal(signal.to_dict())
            self.store.set_runtime("last_analysis_ts", iso(signal.created_at))
            self.store.set_runtime("last_signal_id", signal.id)

            assessment = self._assess(signal)
            self._last_assessment = assessment
            if signal.action in {"BUY", "SELL"}:
                if execute and self.bot_running and assessment.allowed:
                    signal.status = "ready"
                    self._execute(signal, assessment)
                elif execute and self.bot_running:
                    signal.status = "blocked"
                elif not execute or not self.bot_running:
                    signal.status = "ready" if assessment.allowed else "blocked"
                self.store.save_signal(signal.to_dict())
                self.store.set_signal_status(signal.id, signal.status)
                self._log_candidate(signal, assessment)
            else:
                self.store.set_signal_status(signal.id, "wait")
                if time.monotonic() - self._last_scan_log_at > 300:
                    self._last_scan_log_at = time.monotonic()
                    self.store.log(
                        "INFO", "scan_wait",
                        f"Scan complete: WAIT · {signal.reasons[0] if signal.reasons else 'No setup'}",
                        {"confidence": round(signal.confidence, 3), "regime": signal.regime},
                    )
            return signal
        finally:
            self._scan_lock.release()

    def _log_candidate(self, signal: Signal, assessment: RiskAssessment) -> None:
        if signal.action not in {"BUY", "SELL"}:
            return
        blocker = assessment.blockers[0] if assessment.blockers else ""
        key = f"{signal.action}:{round(signal.confidence, 2)}:{blocker}:{self.bot_running}"
        last = self._last_candidate_log
        if last.get("key") == key and time.monotonic() - float(last.get("at", 0)) < 180:
            return
        self._last_candidate_log = {"key": key, "at": time.monotonic()}
        if assessment.allowed and self.bot_running:
            state = "approved"
        elif assessment.allowed:
            state = "ready (auto-trade paused)"
        else:
            state = f"blocked — {blocker}"
        ai_bias = signal.ai.get("bias") if signal.ai else None
        detail = f"{signal.action} {signal.confidence * 100:.0f}% · {signal.regime} · {state}"
        if ai_bias:
            detail += f" · AI {ai_bias}"
        self.store.log(
            "INFO" if assessment.allowed else "WARN",
            "signal",
            detail,
            {
                "signal_id": signal.id,
                "confidence": round(signal.confidence, 3),
                "blockers": assessment.blockers,
                "ai": ai_bias,
            },
        )

    def _execute(self, signal: Signal, assessment: RiskAssessment) -> None:
        if not self._guard_execution():
            return
        try:
            result = self.active_broker.open_market(
                signal_id=signal.id,
                side=signal.action,
                lots=assessment.lots,
                sl=assessment.sl,
                tp=assessment.tp,
                confidence=signal.confidence,
                reason="; ".join(signal.reasons[:2]),
            )
            if not result.ok:
                raise BrokerError(result.message or "Order rejected")
            if self.mode in EXTERNAL_MODES:
                self.store.insert_trade({
                    "ticket": result.ticket or f"LIVE-{int(time.time())}",
                    "signal_id": signal.id,
                    "symbol": self.config.symbol,
                    "side": signal.action,
                    "lots": float(result.lots or assessment.lots),
                    "entry_price": float(result.price or assessment.entry),
                    "sl": assessment.sl,
                    "tp": assessment.tp,
                    "opened_at": iso(),
                    "status": "open",
                    "pnl": 0.0,
                    "fees": 0.0,
                    "confidence": signal.confidence,
                    "reason": "; ".join(signal.reasons[:2])[:240],
                    "mode": "live",
                    "source": "quant+ai" if signal.ai.get("used") else "quant",
                })
            signal.status = "executed"
            self.store.save_signal(signal.to_dict())
            self.store.set_signal_status(signal.id, "executed")
            self.store.set_runtime("last_order_at", iso())
            self.store.log(
                "INFO", "order_executed",
                f"{signal.action} {assessment.lots:.2f} lots XAUUSD @ {result.price or assessment.entry:.2f} · SL {assessment.sl:.2f} · TP {assessment.tp:.2f}",
                {"ticket": result.ticket, "signal_id": signal.id, "confidence": round(signal.confidence, 3)},
            )
            self.refresh()
            self._record_equity_if_due(force=True)
        except Exception as exc:
            self._set_error(f"Order failed: {str(exc)[:240]}")
            self.store.set_signal_status(signal.id, "error")
            self.store.log("ERROR", "order_failed", f"Execution failed: {str(exc)[:300]}")
            if self.bot_running:
                self.pause_bot(reason="Order execution failed")

    def _guard_execution(self) -> bool:
        if not self.bot_running:
            return False
        if self.kill_switch:
            return False
        if not self.connected:
            return False
        if self.mode in EXTERNAL_MODES and not self._armed_active():
            return False
        return True

    def _assess(self, signal: Optional[Signal] = None) -> RiskAssessment:
        signal = signal or self._last_signal or self.strategy.wait_signal(
            self._quote or Quote(self.config.symbol, 0.0, 0.0), self.settings.get("timeframe", "M5"), "No scan completed yet."
        )
        account = self._account or Account(balance=0.0, equity=0.0)
        quote = self._quote or Quote(self.config.symbol, 0.0, 0.0)
        last_closed_raw = self.store.get_runtime("last_closed_at")
        last_closed = parse_iso(last_closed_raw) if isinstance(last_closed_raw, str) else None
        if last_closed is None:
            last_closed = parse_iso(self.store.last_close_time())
        signal_age = None
        if signal.created_at:
            signal_age = max(0.0, (utcnow() - signal.created_at).total_seconds())
        ctx = RiskContext(
            account=account,
            quote=quote,
            positions=self._positions,
            settings=self.settings,
            day_start_equity=self._day_start_equity(),
            peak_equity=self._peak_equity(),
            last_closed_at=last_closed,
            now=utcnow(),
            kill_switch=self.kill_switch,
            connected=self.connected,
            live=self.mode in EXTERNAL_MODES,
            armed=self._armed_active(),
            signal_age_seconds=signal_age,
        )
        return self.risk.assess(signal, ctx)

    # ------------------------------------------------------------------
    # Dynamic protection
    # ------------------------------------------------------------------
    def _manage_brackets(self) -> None:
        """Trail stops and extend targets for locally tracked open positions."""
        settings = self.settings
        if not bool(settings.get("auto_adjust_brackets", True)):
            return
        if not self.connected or self._account is None or self._quote is None:
            return
        if self.mode in EXTERNAL_MODES and not self._armed_active():
            return
        if not RiskGovernor.market_open(utcnow()):
            return
        now = time.monotonic()
        if now - self._last_bracket_check < BRACKET_CHECK_INTERVAL_SECONDS:
            return
        self._last_bracket_check = now
        signal = self._last_signal
        if signal is None:
            return
        try:
            atr = float(signal.features.get("atr", 0.0))
            score = float(signal.raw_score or 0.0)
        except (TypeError, ValueError):
            return
        if atr <= 0:
            return
        by_ticket = {str(position.ticket): position for position in self._positions}
        for trade in self.store.get_open_trades(mode=self.trade_mode):
            ticket = str(trade["ticket"])
            if now < self._bracket_ticket_until.get(ticket, 0.0):
                continue
            remote = by_ticket.get(ticket)
            if self.mode in EXTERNAL_MODES and remote is None:
                # Not visible at the broker (closed, or a stale local record).
                # Reconciliation handles the close path separately.
                continue
            current_sl = remote.sl if remote is not None and remote.sl > 0 else float(trade.get("sl") or 0.0)
            current_tp = remote.tp if remote is not None and remote.tp > 0 else float(trade.get("tp") or 0.0)
            current_price = self._quote.bid if trade["side"] == "BUY" else self._quote.ask
            plan = plan_bracket_update(
                side=str(trade["side"]),
                entry=float(trade["entry_price"]),
                initial_sl=trade.get("initial_sl") or trade.get("sl"),
                sl=current_sl,
                tp=current_tp,
                price=current_price,
                atr=atr,
                settings=settings,
                score=score,
                extensions=int(trade.get("tp_extensions") or 0),
            )
            if plan is None or not plan.changed:
                continue
            try:
                self.active_broker.modify_protection(ticket, plan.sl, plan.tp)
                updated = self.store.update_protection(ticket, plan.sl, plan.tp, plan.extensions)
            except Exception as exc:
                self._bracket_ticket_until[ticket] = now + 30
                self.store.log(
                    "WARN", "bracket_failed",
                    f"Could not update protection for {ticket}: {str(exc)[:180]}",
                    {"ticket": ticket},
                )
                continue
            if not updated:
                continue
            self._bracket_ticket_until[ticket] = now + BRACKET_TICKET_COOLDOWN_SECONDS
            self.store.log(
                "INFO", "bracket_adjust",
                f"{ticket}: " + "; ".join(plan.reasons) + f" ({plan.r_multiple:+.2f}R)",
                {
                    "ticket": ticket,
                    "sl": plan.sl,
                    "tp": plan.tp,
                    "extensions": plan.extensions,
                    "r_multiple": plan.r_multiple,
                },
            )

    # ------------------------------------------------------------------
    # Controls
    # ------------------------------------------------------------------
    def start_bot(self) -> Dict[str, Any]:
        if self.kill_switch:
            raise EngineError("Kill switch is active. Clear the halt before starting.")
        if not self.refresh():
            raise EngineError("Market data is unavailable.")
        if self.mode in EXTERNAL_MODES and not self._armed_active():
            raise EngineError("Live trading is not armed. Arm this session first.")
        self.bot_running = True
        self._needs_initial_scan = True
        self._scan_requested = True
        self.store.set_runtime("bot_running", True)
        live = " LIVE " + self.platform_label + " MODE" if self.mode in EXTERNAL_MODES else " Paper mode."
        self.store.log("INFO", "bot_started", "Autonomous trading started." + live)
        return {"ok": True, "running": True}

    def pause_bot(self, reason: str = "Paused by user") -> Dict[str, Any]:
        was_running = self.bot_running
        self.bot_running = False
        self.store.set_runtime("bot_running", False)
        if was_running:
            self.store.log("INFO", "bot_paused", f"Autonomous trading paused. {reason}")
        return {"ok": True, "running": False}

    def emergency_halt(self) -> Dict[str, Any]:
        self.bot_running = False
        self.kill_switch = True
        self.store.set_runtime("bot_running", False)
        self.store.set_runtime("kill_switch", True)
        self.store.log("WARN", "kill_switch", "EMERGENCY HALT — no new orders will be accepted.")
        return {"ok": True, "kill_switch": True, "bot_running": False}

    def clear_halt(self) -> Dict[str, Any]:
        self.kill_switch = False
        self.store.set_runtime("kill_switch", False)
        self.store.log("INFO", "kill_switch_cleared", "Kill switch cleared by operator.")
        return {"ok": True, "kill_switch": False}

    def _set_broker_armed(self, armed: bool) -> None:
        if self.mt5 is not None:
            self.mt5.armed = bool(armed and self.mode == "mt5")
        if self.ea is not None:
            self.ea.armed = bool(armed and self.mode == "mt4")

    def _live_positions_for_safety(self) -> List[Position]:
        if self.mode not in EXTERNAL_MODES:
            return []
        try:
            return self.active_broker.positions()
        except BrokerError:
            return []

    def switch_mode(self, target: str) -> Dict[str, Any]:
        target = str(target).lower()
        if target not in {"paper", "mt5", "mt4"}:
            raise EngineError("Mode must be paper, mt5 or mt4.")
        if target == self.mode:
            return self.connection_state()
        if target == "paper":
            local_live = self.store.get_open_trades(mode="live")
            if local_live:
                raise EngineError("Close all live positions before switching to paper.")
            remote_positions = self._live_positions_for_safety()
            if remote_positions:
                raise EngineError("Close all live positions before switching to paper.")
            self.pause_bot("Mode switch to paper")
            self._set_broker_armed(False)
            if self.mt5:
                self.mt5.disconnect()
            if self.ea:
                self.ea.disconnect()
            self.mode = "paper"
            self.connected = True
            self._armed_until = None
            self.store.set_runtime("engine_mode", "paper")
            self.store.set_runtime("live_armed_until", None)
            self.store.log("WARN", "mode_switch", "Switched to PAPER trading. No real orders can be placed.")
            self.refresh()
            self._scan_requested = True
            return self.connection_state()

        # Switching to an external terminal (MT5 Python bridge or MT4 EA).
        if self.store.get_open_trades(mode="paper"):
            raise EngineError("Close all paper positions before switching to a live terminal.")
        if self.mode in EXTERNAL_MODES and self.mode != target:
            if self.store.get_open_trades(mode="live") or self._live_positions_for_safety():
                raise EngineError("Close all live positions before switching between MT4 and MT5.")
        if target == "mt5":
            if self.mode == "mt5":
                return self.connection_state()
            if not self.config.bridge_configured:
                raise EngineError("Set AURUM_BRIDGE_URL and AURUM_BRIDGE_TOKEN to connect MetaTrader 5.")
            if self.mt5 is None:
                self.mt5 = MT5BridgeBroker(self.config)
            self.pause_bot("Mode switch to MT5")
            try:
                self.mt5.connect()
            except BrokerError as exc:
                raise EngineError(str(exc))
            self.mode = "mt5"
            self.mt5.armed = False
            self.store.set_runtime("engine_mode", "mt5")
            self.store.log("WARN", "mode_switch", "Connected to MetaTrader 5 bridge in VIEW-ONLY mode. Arm the session to enable live execution.")
        else:
            # Local MT4 EA
            if not self.config.ea_token or self.ea is None:
                raise EngineError("Set AURUM_EA_TOKEN in the dashboard .env and restart to enable the MT4 EA bridge.")
            self.pause_bot("Mode switch to MT4")
            try:
                self.ea.connect()
            except BrokerError as exc:
                raise EngineError(str(exc))
            self.mode = "mt4"
            self.ea.armed = False
            self.store.set_runtime("engine_mode", "mt4")
            self.store.log("WARN", "mode_switch", "Connected to MetaTrader 4 EA in VIEW-ONLY mode. Arm the session to enable live execution.")
        self.connected = True
        self._armed_until = None
        self._set_broker_armed(False)
        self.store.set_runtime("live_armed_until", None)
        self.refresh()
        self._scan_requested = True
        return self.connection_state()

    def reconnect(self) -> Dict[str, Any]:
        if self.mode == "mt5" and self.mt5 is not None:
            try:
                self.mt5.connect()
                self.connected = True
                self.refresh()
                self.store.log("INFO", "bridge_reconnected", "MT5 bridge connection restored.")
            except BrokerError as exc:
                self.connected = False
                self._set_error(str(exc))
                raise EngineError(str(exc))
            return self.connection_state()
        if self.mode == "mt4" and self.ea is not None:
            try:
                self.ea.connect()
                self.connected = True
                self.refresh()
                self.store.log("INFO", "ea_reconnected", "MT4 EA heartbeat restored.")
            except BrokerError as exc:
                self.connected = False
                self._set_error(str(exc))
                raise EngineError(str(exc))
            return self.connection_state()
        self.refresh()
        return self.connection_state()

    def _broker_link_ok(self) -> bool:
        if self.mode == "mt5":
            return bool(self.mt5 and self.mt5.connected)
        if self.mode == "mt4":
            if self.ea is None:
                return False
            try:
                self.ea.connect()
                return True
            except BrokerError:
                return False
        return True

    def arm_live(self, phrase: str) -> Dict[str, Any]:
        if self.mode not in EXTERNAL_MODES:
            raise EngineError("Switch to MT4 or MT5 mode before arming live execution.")
        if not self.config.live_enabled:
            raise EngineError("Live trading is disabled on the server (AURUM_LIVE_ENABLED=false).")
        if not self._broker_link_ok():
            raise EngineError(f"Connect {self.platform_label} before arming.")
        if not hmac.compare_digest(str(phrase or ""), self.config.live_arm_phrase):
            self.store.log("WARN", "live_arm_denied", "Live arm phrase did not match.")
            raise EngineError("Live arm phrase is incorrect.")
        self._armed_until = utcnow() + timedelta(hours=LIVE_ARM_HOURS)
        self._set_broker_armed(True)
        self.store.set_runtime("live_armed_until", iso(self._armed_until))
        self.store.log("WARN", "live_armed", f"LIVE execution armed on {self.platform_label} until {iso(self._armed_until)}. Real orders are now possible.")
        self._scan_requested = True
        return {"ok": True, "armed": True, "until": iso(self._armed_until)}

    def disarm_live(self) -> Dict[str, Any]:
        self.pause_bot("Live session disarmed")
        self._armed_until = None
        self._set_broker_armed(False)
        self.store.set_runtime("live_armed_until", None)
        self.store.log("INFO", "live_disarmed", "Live execution disarmed.")
        self._scan_requested = True
        return {"ok": True, "armed": False}

    def _armed_active(self) -> bool:
        if self.mode not in EXTERNAL_MODES:
            return False
        if self._armed_until is None:
            raw = self.store.get_runtime("live_armed_until")
            if isinstance(raw, str):
                self._armed_until = parse_iso(raw)
        if self._armed_until and self._armed_until > utcnow():
            self._set_broker_armed(True)
            return True
        self._set_broker_armed(False)
        return False

    def _check_arm_expiry(self) -> None:
        if self.mode not in EXTERNAL_MODES:
            return
        if self._armed_until and self._armed_until <= utcnow():
            if self.bot_running:
                self.pause_bot("Live arm expired")
            self._armed_until = None
            self._set_broker_armed(False)
            self.store.set_runtime("live_armed_until", None)
            self.store.log("WARN", "live_arm_expired", "The 12-hour live arm window expired; execution is disarmed.")

    # ------------------------------------------------------------------
    # Manual trade management
    # ------------------------------------------------------------------
    def close_position(self, ticket: str, reason: str = "Manual close") -> Dict[str, Any]:
        if not ticket:
            raise EngineError("A ticket is required.")
        if self.mode in EXTERNAL_MODES and not self._armed_active():
            raise EngineError(f"Arm the live session before closing a {self.platform_label} position.")
        try:
            result = self.active_broker.close_position(str(ticket), reason)
        except BrokerError as exc:
            raise EngineError(str(exc))
        if self.mode in EXTERNAL_MODES:
            local = self.store.get_trade(str(ticket))
            if local and local.get("status") == "open":
                self.store.close_trade(
                    str(ticket), float(result.get("price") or 0.0), float(result.get("pnl") or 0.0),
                    float(local.get("fees") or 0.0), reason,
                )
            self.store.set_runtime("last_closed_at", iso())
            self.store.log("INFO", "trade_closed", f"{self.platform_label} position {ticket} closed manually for {float(result.get('pnl') or 0.0):+.2f}.", {"ticket": ticket})
        self.refresh()
        self._record_equity_if_due(force=True)
        return result

    def close_all_positions(self, reason: str = "Flatten all") -> Dict[str, Any]:
        if self.mode in EXTERNAL_MODES and not self._armed_active():
            raise EngineError(f"Arm the live session before flattening {self.platform_label} positions.")
        try:
            results = self.active_broker.close_all(reason)
        except BrokerError as exc:
            raise EngineError(str(exc))
        if self.mode in EXTERNAL_MODES:
            for item in results:
                if not item.get("ok", True):
                    continue
                ticket = str(item.get("ticket", ""))
                local = self.store.get_trade(ticket) if ticket else None
                if local and local.get("status") == "open":
                    self.store.close_trade(
                        ticket, float(item.get("price") or 0.0), float(item.get("pnl") or 0.0),
                        float(local.get("fees") or 0.0), reason,
                    )
            self.store.set_runtime("last_closed_at", iso())
            self.store.log("INFO", "flatten_all", f"Flattened {sum(1 for x in results if x.get('ok', True))} {self.platform_label} position(s).")
        self.refresh()
        self._record_equity_if_due(force=True)
        return {"ok": True, "closed": len(results), "results": results}

    def reset_paper(self, balance: Optional[float] = None) -> Dict[str, Any]:
        if self.mode != "paper":
            raise EngineError("Paper reset is only available in paper mode.")
        self.pause_bot("Paper account reset")
        try:
            self.paper.close_all("Paper reset")
        except Exception:
            pass
        new_balance = float(balance) if balance else self.config.paper_balance
        if new_balance < 100 or new_balance > 10_000_000:
            raise EngineError("Starting balance must be between 100 and 10,000,000.")
        self.paper.reset(new_balance)
        self._last_signal = None
        self._last_assessment = None
        self.kill_switch = False
        self.store.set_runtime("kill_switch", False)
        self.store.set_runtime("last_equity_snapshot", None)
        for key in ("peak_equity_paper", "day_start_equity_paper", "day_start_date_paper", "last_closed_at"):
            self.store.set_runtime(key, None)
        self.store.log("WARN", "paper_reset", f"Paper account reset to {new_balance:,.2f} USD.")
        self.refresh()
        self._seed_equity()
        self._needs_initial_scan = True
        return {"ok": True, "balance": new_balance}

    def _reconcile_live_trades(self) -> None:
        local_open = self.store.get_open_trades(mode="live")
        if not local_open:
            return
        remote_tickets = {p.ticket for p in self._positions}
        missing = [trade for trade in local_open if str(trade["ticket"]) not in remote_tickets]
        if not missing:
            return
        now = time.monotonic()
        if now - self._last_history_sync < 30:
            return
        self._last_history_sync = now
        if self.mode not in EXTERNAL_MODES:
            return
        try:
            history = self.active_broker.history(days=7)  # type: ignore[attr-defined]
        except (BrokerError, AttributeError):
            return
        by_ticket = {str(item.get("ticket", item.get("position_id", ""))): item for item in history}
        for trade in missing:
            ticket = str(trade["ticket"])
            item = by_ticket.get(ticket)
            if not item:
                continue
            profit = float(item.get("profit", 0) or 0) + float(item.get("commission", 0) or 0) + float(item.get("swap", 0) or 0)
            self.store.close_trade(
                ticket,
                float(item.get("exit_price", item.get("price", 0)) or 0),
                round(profit, 2),
                0.0,
                f"Closed at {self.platform_label}",
                item.get("close_time"),
            )
            self.store.set_runtime("last_closed_at", iso())
            self.store.log("INFO", "trade_closed", f"{self.platform_label} position {ticket} closed at broker for {profit:+.2f}.", {"ticket": ticket})

    # ------------------------------------------------------------------
    # Settings and backtest
    # ------------------------------------------------------------------
    def update_settings(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        try:
            self.settings = self.store.update_settings(updates)
        except ValueError as exc:
            raise EngineError(str(exc))
        changed = ", ".join(f"{key}={value}" for key, value in updates.items())
        self.store.log("INFO", "settings_updated", f"Settings updated: {changed[:400]}")
        self._scan_requested = True
        return self.settings

    def run_csv_backtest(self, csv_text: str, *, filename: str = "upload.csv", timeframe: str = "M5",
                         utc_offset_minutes: int = 120, spread: float = 0.20,
                         commission_per_lot: float = 7.0, slippage: float = 0.02) -> Dict[str, Any]:
        if not isinstance(csv_text, str) or not csv_text.strip():
            raise EngineError("No CSV data was uploaded.")
        if len(csv_text.encode("utf-8", "ignore")) > 8 * 1024 * 1024:
            raise EngineError("CSV is too large. Limit is 8 MB.")
        try:
            offset = max(-720, min(840, int(utc_offset_minutes)))
            spread = max(0.0, min(10.0, float(spread)))
            commission = max(0.0, min(200.0, float(commission_per_lot)))
            slip = max(0.0, min(5.0, float(slippage)))
            candles, meta = parse_mt5_csv(
                csv_text,
                symbol=self.config.symbol,
                timeframe=timeframe,
                utc_offset_minutes=offset,
            )
        except (CSVParseError, ValueError, TypeError) as exc:
            raise EngineError(str(exc))
        starting = self.config.paper_balance
        if self.mode == "paper":
            try:
                starting = self.store.get_paper_account()["initial_balance"]
            except Exception:
                pass
        elif self._account:
            starting = self._account.balance
        safe_name = Path(str(filename or "upload.csv")).name[:100]
        result = run_backtest(
            start_balance=starting,
            settings=self.store.get_settings(),
            symbol=self.config.symbol,
            candles=candles,
            data_source="mt5_csv",
            timeframe=meta["timeframe"],
            commission_per_lot=commission,
            spread=spread,
            slippage=slip,
            source_label=f"MT5 history · {self.config.symbol} · {meta['timeframe']} · {safe_name}",
        )
        result["import"] = {**meta, "filename": safe_name, "costs": {
            "spread": spread,
            "commission_per_lot": commission,
            "slippage": slip,
            "utc_offset_minutes": offset,
        }}
        self._latest_backtest = result
        self.store.save_backtest(
            {"source": "mt5_csv", "filename": safe_name, "bars": meta["bars"], "timeframe": meta["timeframe"]},
            result,
        )
        self.store.log(
            "INFO", "backtest_csv",
            f"Real-data backtest complete: {meta['bars']} {meta['timeframe']} bars, {result['summary']['trades']} trades.",
        )
        return result

    def run_backtest(self, days: int) -> Dict[str, Any]:
        try:
            days = int(days)
        except (TypeError, ValueError):
            raise EngineError("Days must be a number.")
        days = max(1, min(days, 60))
        starting = self.config.paper_balance
        if self.mode == "paper":
            try:
                starting = self.store.get_paper_account()["initial_balance"]
            except Exception:
                pass
        result = run_backtest(days=days, start_balance=starting, settings=self.store.get_settings(), symbol=self.config.symbol)
        self._latest_backtest = result
        self.store.save_backtest({"days": days, "starting_balance": starting}, result)
        self.store.log("INFO", "backtest_run", f"Backtest complete: {days} simulated days, {result['summary']['trades']} trades.")
        return result

    # ------------------------------------------------------------------
    # Read models for the API
    # ------------------------------------------------------------------
    def candles(self, timeframe: Optional[str] = None, limit: int = 240) -> Dict[str, Any]:
        timeframe = (timeframe or self.settings["timeframe"]).upper()
        if timeframe not in {"M5", "M15", "M30", "H1"}:
            raise EngineError("Unsupported timeframe.")
        limit = max(40, min(int(limit), 500))
        try:
            data = self.active_broker.candles(timeframe, limit)
        except BrokerError as exc:
            raise EngineError(str(exc))
        return {
            "timeframe": timeframe,
            "symbol": self.config.symbol,
            "candles": data,
            "simulated": self.mode == "paper",
            "source": "Simulated feed" if self.mode == "paper" else self.platform_label,
        }

    def connection_state(self) -> Dict[str, Any]:
        try:
            status = self.active_broker.status()
        except Exception as exc:
            status = {"connected": False, "status": "error", "label": "Unavailable", "error": str(exc)[:200]}
        ea_status = self.ea.status() if self.ea is not None else {
            "connected": False, "heartbeat_age": None, "last_seen": None, "ea_symbol": "",
            "ea_configured": bool(self.config.ea_token), "ea_polls": 0,
        }
        status.update({
            "mode": self.mode,
            "platform": self.platform_label,
            "armed": self._armed_active(),
            "arm_expires_at": iso(self._armed_until) if self._armed_until and self._armed_active() else None,
            "live_enabled": self.config.live_enabled,
            "bridge_configured": self.config.bridge_configured,
            "bridge_host": self.config.public_connection()["bridge_host"],
            "ea_configured": bool(self.config.ea_token),
            "ea_heartbeat_age": ea_status.get("heartbeat_age"),
            "ea_last_seen": ea_status.get("last_seen"),
            "ea_symbol": ea_status.get("ea_symbol"),
            "ea_polls": ea_status.get("ea_polls", 0),
            "data_source": "Simulated ticks" if self.mode == "paper" else self.platform_label,
        })
        return status

    def dashboard_state(self) -> Dict[str, Any]:
        now = utcnow()
        account = self._account
        quote = self._quote
        signal = self._last_signal
        assessment = self._assess(signal)
        self._last_assessment = assessment
        settings = self.settings
        closed_trades = self.store.closed_trades(mode=self.trade_mode)
        if account is not None:
            if self.mode == "paper":
                starting = self.store.get_paper_account()["initial_balance"]
            else:
                net = sum(float(t.get("pnl") or 0.0) for t in closed_trades)
                starting = max(1.0, account.balance - net)
            stats = trade_stats(closed_trades, starting)
            day_start = self._day_start_equity()
            daily_pnl = account.equity - day_start
            daily_loss_pct = max(0.0, -daily_pnl / day_start * 100.0) if day_start > 0 else 0.0
            peak = self._peak_equity()
            drawdown_pct = max(0.0, (peak - account.equity) / peak * 100.0) if peak > 0 else 0.0
        else:
            stats = trade_stats([], 0.0)
            daily_pnl = 0.0
            daily_loss_pct = 0.0
            peak = 0.0
            drawdown_pct = 0.0
        open_risk_value = open_risk(self._positions)
        risk_payload = assessment.to_dict()
        risk_payload.update({
            "daily_pnl": round(daily_pnl, 2),
            "daily_realised": daily_realised(closed_trades, now),
            "daily_loss_pct": round(daily_loss_pct, 2),
            "daily_limit_pct": float(settings["max_daily_loss_pct"]),
            "drawdown_pct": round(drawdown_pct, 2),
            "drawdown_limit_pct": float(settings["max_drawdown_pct"]),
            "peak_equity": round(peak, 2),
            "open_risk": round(open_risk_value, 2),
            "risk_per_trade_pct": float(settings["risk_per_trade_pct"]),
            "risk_budget": round((account.equity if account else 0.0) * float(settings["risk_per_trade_pct"]) / 100.0, 2),
        })
        market_open = RiskGovernor.market_open(now)
        market_stats: Dict[str, Any] = {"session": "open" if market_open else "closed"}
        if self.mode == "paper":
            try:
                market_stats.update(self.paper.market_stats())
            except Exception:
                pass
        next_scan_seconds = 0
        if self.bot_running:
            elapsed = time.monotonic() - self._last_scan_mono
            next_scan_seconds = max(0, int(SCAN_INTERVAL_SECONDS - elapsed))
        connection = self.connection_state()
        recent = self.store.recent_trades(limit=40, mode=self.trade_mode)
        closed_for_ui = [public_trade(t) for t in recent if t.get("status") == "closed"][:25]
        latest_backtest = self._latest_backtest or {}
        state = {
            "meta": {
                "app": APP_NAME,
                "version": APP_VERSION,
                "server_time": iso(now),
                "mode": self.mode,
                "is_live": self.mode in EXTERNAL_MODES,
                "simulated": self.mode == "paper",
                "symbol": self.config.symbol,
                "live_enabled": self.config.live_enabled,
                "auth_enabled": self.config.auth_enabled,
                "uptime_seconds": int((now - self._started_at).total_seconds()),
                "scan_interval": SCAN_INTERVAL_SECONDS,
            },
            "connection": connection,
            "account": account.to_dict() if account else None,
            "quote": quote.to_dict() if quote else None,
            "market": {
                **market_stats,
                "symbol": self.config.symbol,
                "timeframe": settings["timeframe"],
                "data_source": "Simulated" if self.mode == "paper" else self.platform_label,
            },
            "signal": signal.to_dict() if signal else None,
            "risk": risk_payload,
            "positions": [position.to_dict() for position in self._positions],
            "trades": closed_for_ui,
            "equity": self.store.equity_series(limit=180, mode=self.mode),
            "performance": stats,
            "bot": {
                "running": self.bot_running,
                "kill_switch": self.kill_switch,
                "last_scan": iso(self._last_scan_at) if getattr(self, "_last_scan_at", None) else None,
                "next_scan_seconds": next_scan_seconds,
                "scans": self._scans,
                "errors": self._scan_errors,
                "last_error": self._last_error or None,
            },
            "logs": self.store.logs(50),
            "settings": settings,
            "ai": self._ai_payload(settings),
            "backtest": latest_backtest.get("summary") if latest_backtest else None,
            "backtest_meta": {
                "started_at": latest_backtest.get("started_at"),
                "label": latest_backtest.get("label"),
                "request": latest_backtest.get("request"),
                "data_source": latest_backtest.get("data_source", "synthetic"),
                "import": latest_backtest.get("import"),
            } if latest_backtest else None,
        }
        return state

    def _ai_payload(self, settings: Dict[str, Any]) -> Dict[str, Any]:
        payload = self.ai.status()
        payload["enabled"] = bool(settings.get("ai_overlay", True))
        return payload

    def bootstrap(self) -> Dict[str, Any]:
        return {
            "app": APP_NAME,
            "version": APP_VERSION,
            "schema": public_schema(),
            "connection": self.config.public_connection(),
            "auth_enabled": self.config.auth_enabled,
            "symbol": self.config.symbol,
            "scan_interval": SCAN_INTERVAL_SECONDS,
        }
