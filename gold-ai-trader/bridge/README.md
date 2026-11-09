# AURUM MT5 Windows bridge

A small HTTP bridge around the official **Windows-only** [`MetaTrader5`](https://pypi.org/project/MetaTrader5/)
Python package. It lets the AURUM dashboard run on Linux/Docker/native Python while MT5 remains on a
Windows machine.

The bridge is intentionally minimal: one symbol, token authentication, read endpoints, and
market-order open/close. It does not expose MQL execution, file access or arbitrary terminal
commands.

## Requirements

- Windows 10/11 or Windows Server with MetaTrader 5 installed.
- Python 3.9+ on the same machine.
- A logged-in MT5 account and the target symbol (default `XAUUSD`) added to Market Watch.
- Network reachability from the dashboard to `BRIDGE_HOST:BRIDGE_PORT` (loopback or a private
  tunnel).

## Install

```bat
cd bridge
py -3 -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
copy bridge.env.example bridge.env
```

Edit `bridge.env` with a text editor:

```dotenv
BRIDGE_HOST=127.0.0.1
BRIDGE_PORT=8765
BRIDGE_TOKEN=a-long-random-token
MT5_LOGIN=12345678
MT5_PASSWORD=your-demo-password
MT5_SERVER=YourBroker-Demo
MT5_PATH=C:\Program Files\MetaTrader 5\terminal64.exe
MT5_SYMBOL=XAUUSD
BRIDGE_ALLOW_LIVE=false
BRIDGE_MAGIC=20261101
```

`BRIDGE_TOKEN` must be at least 16 characters. Generate one with:

```bat
py -c "import secrets; print(secrets.token_urlsafe(32))"
```

Use the **same token** in the dashboard `.env` as `AURUM_BRIDGE_TOKEN`.

## Run

```bat
python mt5_bridge.py
```

Keep the console open and keep MetaTrader 5 running/logged in. The bridge prints its bind address
but never prints the token or password. It reconnects on the next request if the terminal was
restarted.

Quick local test in another terminal:

```bat
curl -H "Authorization: Bearer YOUR_TOKEN" http://127.0.0.1:8765/health
```

The health response reports the symbol, server, account type, bridge version and whether live orders
are enabled. It requires the token too.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `BRIDGE_HOST` | `127.0.0.1` | Bind address. Keep loopback unless using a private tunnel. |
| `BRIDGE_PORT` | `8765` | Bridge HTTP port. |
| `BRIDGE_TOKEN` | — | Required bearer token (≥16 chars). |
| `MT5_LOGIN` | — | MT5 account number. |
| `MT5_PASSWORD` | — | MT5 password. Stored only in this environment file. |
| `MT5_SERVER` | — | Exact broker server name from the MT5 login dialog. |
| `MT5_PATH` | — | Optional terminal executable path. |
| `MT5_SYMBOL` | `XAUUSD` | Exactly the symbol shown in Market Watch. |
| `BRIDGE_ALLOW_LIVE` | `false` | Allow real-account orders. Keep false for demo work. |
| `BRIDGE_MAGIC` | `20261101` | MT5 magic number for bridge orders. |
| `BRIDGE_LOG_LEVEL` | `INFO` | Logging level. |

The bridge loads `bridge.env` from its own directory by default. You can point it elsewhere with
`BRIDGE_ENV_FILE`. Actual process environment variables take precedence.

## Safety gates

- Every route requires `Authorization: Bearer <BRIDGE_TOKEN>`.
- Demo accounts may be traded with a valid token; **real accounts are refused** unless
  `BRIDGE_ALLOW_LIVE=true` **and** the request includes `X-Live-Confirm: YES`.
- AURUM's dashboard only sends that header after its own explicit arm process.
- Market orders must carry a valid SL and TP, correct direction, broker volume constraints and
  minimum stop distance.
- `/modify` accepts only a bracket that keeps price between stop and target, respects the broker's
  minimum stop distance, never moves a long stop down / a short stop up, and never pulls a target
  closer. The dashboard's bracket planner sends only improving changes.
- An 800ms order throttle limits burst order submissions. `request_id` values are remembered in
  memory to make immediate retries idempotent.
- The bridge is not a substitute for account-level risk controls. Use a demo account and small
  sizes while testing.

## Endpoints

All endpoints require the bearer token.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/health` | Version, symbol, account type, live permission. |
| GET | `/account` | Balance, equity, margin, free margin, leverage, account type. |
| GET | `/quote` | Bid/ask/time for the configured symbol. |
| GET | `/candles?timeframe=M5&limit=240` | Closed/current MT5 rates for M5/M15/M30/H1. |
| GET | `/positions` | Open positions for the configured symbol. |
| GET | `/history?days=7` | Closed position aggregates from MT5 deal history. |
| POST | `/order` | Market order with SL/TP, request id and comment. |
| POST | `/close` | Close one position by ticket. |
| POST | `/modify` | Update SL/TP for one position (risk-reducing stop changes and target extensions). |
| POST | `/close_all` | Close all bridge-symbol positions. |

The dashboard's `MT5BridgeBroker` validates the bridge symbol against `AURUM_SYMBOL` at connection
time. A mismatch is rejected deliberately.

## Connecting the dashboard

### Same Windows machine (recommended)

```dotenv
AURUM_BRIDGE_URL=http://127.0.0.1:8765
AURUM_BRIDGE_TOKEN=the-same-token
```

Run the dashboard natively with `python -m app.main` (or `./run.sh` under Git Bash/WSL). This
avoids tunnels and is the easiest way to keep the bridge on loopback.

### Docker on the same Windows machine

The container's `127.0.0.1` is not the host. Set:

```dotenv
AURUM_BRIDGE_URL=http://host.docker.internal:8765
```

The compose file already adds `host.docker.internal` via `host-gateway`. If the bridge is bound only
to `127.0.0.1` and the container cannot reach it, adjust the computer's firewall/bridge bind
carefully or use a tunnel. Never open 8765 to the public internet.

### Remote Windows VPS

Use an SSH tunnel from the Docker host:

```bash
ssh -N -L 127.0.0.1:8765:127.0.0.1:8765 YOUR_USER@YOUR_WINDOWS_HOST
```

On Docker Desktop, `AURUM_BRIDGE_URL=http://host.docker.internal:8765` usually reaches the host
tunnel. On Linux, bind the tunnel to a restricted host interface or use WireGuard/Tailscale and
point the container at that private address. The bridge remains bound to `127.0.0.1` on Windows.

## Testing the live path safely

1. Demo account, `BRIDGE_ALLOW_LIVE=false`.
2. Dashboard `AURUM_LIVE_ENABLED=true` (arming is still required for demo execution).
3. Connect MT5 (view-only), arm, start auto with a very small setting.
4. Watch the first order appear in both the dashboard and MT5.
5. Test **Pause**, **Halt**, **Flatten all**, and manual close.
6. Restart the dashboard and confirm it returns to paper mode.
7. Rotate the bridge token and confirm old tokens stop working.

Only after that should you consider `BRIDGE_ALLOW_LIVE=true` on a real account.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| `MetaTrader5 package is missing` | Run `pip install -r requirements.txt` in the bridge venv on Windows. |
| `MT5 initialize() failed` | Is MT5 running/logged in? Are login/server/path correct? Is the terminal allowed to start? |
| Symbol not found | Add the exact `MT5_SYMBOL` to Market Watch; broker suffixes matter (e.g. `XAUUSD.a`). |
| `Unsupported filling mode` / order rejected | Broker-specific execution mode; inspect the MT5 Experts log and retcode. |
| Bridge unreachable from Docker | Use the correct host address/tunnel, not `127.0.0.1` inside the container. |
| Dashboard says symbol mismatch | Set `AURUM_SYMBOL` and `MT5_SYMBOL` to the same exact string. |
| Dashboard shows bridge offline | Check `/health` with the token, then use Reconnect. Auto pauses on disconnect. |
| Positions don't reconcile | The bridge imports close deals from `/history` after a local live trade disappears; inspect MT5 account history. |
| Port already in use | Change `BRIDGE_PORT` and both sides of the dashboard URL. |

## Limitations

- One symbol per bridge instance (run multiple bridges on different ports for multiple symbols, with
  separate dashboard instances/ports).
- Market orders only; no pending orders or partial closes. Automatic SL/TP modification is supported
  through `/modify`; the dashboard drives the trail/extension logic.
- HTTP only. Use a private network/tunnel; TLS termination is not built in.
- In-memory idempotency does not survive a bridge restart.
- Closing uses the position's current profit value for the local record; MT5's own account history is
  the final authority.
- The `MetaTrader5` package and terminal are Windows-specific; Linux/Wine setups are unsupported.
