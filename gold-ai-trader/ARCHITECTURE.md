# Architecture

AURUM AI is a single-operator, single-symbol trading desk. It deliberately uses a small number of
clear components with explicit trust boundaries instead of a broad framework.

## Runtime view

```text
┌────────────────────────────────────────────────────────────────────────────┐
│ Browser                                                                    │
│  static HTML/CSS/JS · polling state every 2.5s · candles every 12s         │
└───────────────────────────────┬────────────────────────────────────────────┘
                                │ same-origin HTTP · signed HttpOnly session
┌───────────────────────────────▼────────────────────────────────────────────┐
│ AURUM dashboard process (Python standard library)                          │
│                                                                            │
│  api.py         auth, CSRF, static files, JSON routes, error mapping       │
│  engine.py      scan loop · broker snapshot · mode/arm gates · controls    │
│  strategy.py    indicators → signed score → signal → capped AI overlay     │
│  risk.py        deterministic all-checks-must-pass order governor          │
│  broker.py      PaperBroker or MT5BridgeBroker                             │
│  market.py      deterministic simulated XAU/USD feed                       │
│  backtest.py    isolated synthetic event backtester                        │
│  db.py          SQLite WAL persistence, one lock, one connection           │
│  ai.py          optional OpenAI-compatible analyst; no tools, no execution  │
└───────────┬───────────────────────────────────────────┬────────────────────┘
            │ in-process                                 │ bearer-token HTTP
┌───────────▼──────────────────┐            ┌────────────▼───────────────────┐
│ Paper simulation             │            │ Windows MT5 bridge             │
│ synthetic ticks · fills      │            │ mt5_bridge.py · 127.0.0.1:8765 │
│ SQLite book · SL/TP sweeps   │            │ MetaTrader5 Python package     │
└──────────────────────────────┘            └────────────┬───────────────────┘
                                                        │ native API
                                            ┌───────────▼────────────────────┐
                                            │ MetaTrader 5 terminal          │
                                            │ demo/real account · XAUUSD     │
                                            └────────────────────────────────┘
```

The AI provider is an optional outbound HTTPS call from `ai.py`. It never receives broker
credentials or session cookies, has no tools, and its response is validated before it can alter a
signal's confidence.

## Why this shape

- **MT5 is Windows-only.** The official `MetaTrader5` Python wheel will not load in a Linux
  container. The bridge keeps MT5 logic on Windows and lets the dashboard run anywhere.
- **No runtime dependencies.** The dashboard and bridge client use only Python's standard library.
  This makes the container small, auditable, and trivially reproducible. The bridge alone needs the
  official MT5 package.
- **Server-side authority.** Every policy decision, execution attempt and validation happens in
  Python. The browser is a view/controller, not a source of truth.
- **No generic order route.** There is no `/api/order`. Orders come only from the engine after the
  risk governor has evaluated the current signal. This prevents the UI from bypassing policy.
- **Explicit live gates.** Connecting, viewing, arming and starting are separate state transitions.

## Request flow: dashboard state

1. The engine thread refreshes the active broker snapshot every 2 seconds.
2. For paper mode, `PaperBroker` advances the simulated feed and exposes account, quote and
   positions from the same SQLite book.
3. For MT5 mode, `MT5BridgeBroker` fetches `/account`, `/quote` and `/positions` from the bridge,
   applies a stale-quote check (180 seconds), and caches the snapshot.
4. The engine updates peak equity, UTC day-start equity, and records an equity snapshot every
   30 seconds.
5. `GET /api/state` assembles a read model from cached values plus SQLite: risk assessment, stats,
   recent trades, logs, settings and connection state.
6. The browser renders, polling every 2.5 seconds. It does not recompute authoritative P&L or risk.

## Signal and execution flow

```text
closed candles + quote
        │
        ▼
QuantStrategy.evaluate
  EMA20/50/200 · RSI · MACD · ATR · ADX · Bollinger
  regime classification + breakout / range-fade sleeves
  signed score → confidence → BUY/SELL/WAIT + ATR bracket
        │
        ├── if candidate and AI configured ──> AIAnalyst.review
        │                                      strict JSON, max 30% blend
        │                                      can only temper/veto, never originate
        ▼
save signal + persist audit log
        │
        ▼
RiskGovernor.assess
  connect · market hours · session · live arm · spread · position slots
  setup freshness · daily loss · drawdown · cooldown · sizing · R:R
        │
        ├── any failure → signal blocked; no broker call
        ▼
TradingEngine._execute
  guard: bot running + connected + not halted + live session armed
  broker.open_market(...)
  persist order/trade, refresh snapshot, record equity
        │
        ▼
Bracket manager every 5s while positions are open
  trail stop after breakeven_at_r · extend TP near target only if risk-free + momentum agrees
  paper: update local book · live: bridge /modify + local protection record
```

The cycle runs every 20 seconds while auto is running. When paused it still refreshes the displayed
signal every 120 seconds **without** any execution path. A new server boot starts paused.

## Mode state machine

```text
                 switch to mt5 (bridge configured)
  ┌────────┐ ────────────────────────────────────────► ┌────────────┐
  │ PAPER  │                                           │ MT5 VIEW   │
  └────────┘ ◄──────────────────────────────────────── │ (read-only)│
      ▲      close all live positions first            └─────┬──────┘
      │                                                     │ arm phrase
      │            disarm / 12h expiry / restart            ▼
      │                                               ┌────────────┐
      └────────────────────────────────────────────── │ MT5 ARMED  │
                 close all live positions first       └────────────┘
                                                            │
                                                     start auto
                                                            ▼
                                                      ┌────────────┐
                                                      │ LIVE LOOP  │
                                                      └────────────┘
```

Important invariants:

- The mode resets to **paper** on every server restart. Orphaned live positions remain protected by
  their broker-side SL/TP and can be managed after reconnecting.
- Switching back to paper is blocked while local or remote live positions exist.
- Arming requires `AURUM_LIVE_ENABLED=true`, a connected bridge, and the configured phrase.
- Arming expires after 12 hours; the engine pauses itself on expiry.
- The bridge separately refuses **real-account** orders unless `BRIDGE_ALLOW_LIVE=true` and the
  request carries `X-Live-Confirm: YES`.
- Manual `Start auto` is required after every restart; `bot_running` is not restored.
- The kill switch is persistent and blocks entries but does not flatten positions.

## Persistence

SQLite in WAL mode, protected by one process-level re-entrant lock. The connection uses
`check_same_thread=False`; every repository operation is serialised.

| Table | Purpose |
| --- | --- |
| `settings` | Validated JSON values for the strategy/risk schema. |
| `paper_account` | Initial balance, balance, peak equity for the simulator. |
| `trades` | Open and closed paper/live trades, ticket, bracket, original bracket, extension count, P&L, mode. |
| `signals` | Last 500 signal payloads and execution status. |
| `equity` | Equity/drawdown snapshots for the chart, capped per mode. |
| `logs` | Last 3,000 audit events. |
| `runtime` | Mode, kill switch, day-start equity, peaks, last scan/close timestamps, spot. |
| `backtests` | Last 10 backtest results. |

The only supported paper reset path (`POST /api/reset-paper`) deletes paper trades, paper equity
and signals, restores the starting balance and clears runtime risk baselines. Backtest history and
audit logs are retained.

## Failure behaviour

| Failure | Behaviour |
| --- | --- |
| Simulator exception | Logged; snapshot may be stale; engine scan reports an error. |
| Bridge unreachable / stale quote | Connection marked offline; auto is paused; no new orders. |
| Candles unavailable | Signal not refreshed; auto pauses on repeated analysis exception. |
| Order rejection / sent failure | Order failure logged; signal marked error; auto pauses for operator review. |
| Live position disappears remotely | Engine queries bridge `/history` and closes the local record when a matching close deal is found. |
| Bracket update rejected by broker | Logged and retried later; trading is not paused and existing protection remains. |
| Process restart | Mode returns to paper, auto is paused, persistent kill switch survives, paper/live trade history remains. |
| Risk breach | New entries blocked; existing positions keep brackets. No automatic forced liquidation. |

The risk governor never automatically flattens a book. That is an explicit operator action because
unexpected liquidation can itself realise large losses. The **Flatten all** control and MT5 itself
are the risk-reducing paths.

## Backtest isolation

`run_backtest()` accepts either its own synthetic candle series or an imported MT5 CSV parsed by
`importer.py`. It has its own quant strategy instance and never touches the live engine, broker or
AI overlay. It simulates next-bar fills with adverse spread, slippage and commission, models
break-even/trailing/target-extension rules at bar close, assumes the stop fills first when both
levels are touched within a bar, applies the same session/daily-loss/drawdown gates, and
force-closes at the end. Imported timestamps are converted from broker server time to UTC with the
operator-supplied offset. Results are stored as research artefacts and execution is labelled
simulated everywhere, even when the candles are real.

## Testing strategy

- Indicator unit tests, including a no-look-ahead EMA check.
- Strategy direction, brackets, warm-up and AI-veto tests.
- Every risk check tested for its blocking behaviour.
- Paper broker round trip and reset persistence.
- Auth/session tamper and login rate-limit tests.
- Backtest shape/clamp and synthetic-data disclaimer.
- A mock HTTP MT5 bridge integration test covering connect → view-only → arm → order → close →
  blocked paper switch.

The real `MetaTrader5` API cannot be exercised on macOS/Linux; the bridge is deliberately thin so
its remaining platform-specific surface is order construction and field mapping.
