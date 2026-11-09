# Security & operational safety

AURUM AI controls a trading account. Treat it like production infrastructure, not a demo web app.

## Trust boundaries

1. **Browser** — holds only a signed session cookie. It never receives the bridge token, broker
   password, AI key or environment secrets. It can request engine actions but cannot bypass the
   server-side risk governor.
2. **Dashboard process** — trusted. Holds session signing secret, bridge token and optional AI key.
   Exposes authenticated JSON endpoints and serves static assets.
3. **MT5 bridge** — trusted process on the Windows machine. Holds the MT5 credentials and has the
   official MetaTrader5 API. Exposes a token-protected HTTP API on loopback.
4. **MetaTrader 5 terminal** — ultimate authority for live orders and account state.
5. **AI provider** — untrusted external service. Receives only numeric market features / a compact
   JSON prompt and returns untrusted text. Its response is parsed, length-limited and clamped.
   It has no execution capability.

## Authentication and web protections

- Password login (`AURUM_DASHBOARD_PASSWORD`) with a server-side signed token. The cookie is
  `HttpOnly`, `SameSite=Strict`, and optionally `Secure` via `AURUM_COOKIE_SECURE=true`.
- Sessions expire after 12 hours. `AURUM_SECRET_KEY` should remain stable across restarts; if it
  is absent, an ephemeral key is generated and all sessions become invalid on restart.
- Login attempts are rate-limited in memory: 6 failures per IP per 5 minutes.
- State-changing `/api/*` requests require `X-AURUM-CSRF: 1`; this blocks simple cross-site form
  submissions. The cookie is also `SameSite=Strict`.
- Static responses set CSP (`default-src 'self'`), `X-Frame-Options: DENY`, `nosniff`,
  `Referrer-Policy: no-referrer` and a restrictive permissions policy.
- Unauthenticated dashboards are only allowed on loopback. The process **refuses to start** with
  `AURUM_HOST=0.0.0.0` (or another non-loopback host) and no dashboard password.
- No external fonts, scripts, analytics or CDN assets are loaded. The UI is fully local.
- Query/body sizes are bounded; the authenticated CSV backtest route accepts up to 8 MB. Uploaded
  CSV data is parsed in memory and is not written to disk. Errors are mapped to generic messages;
  stack traces stay in logs.

## Live trading gates

Three independent controls must align before a real order can exist:

1. **Dashboard server**: `AURUM_LIVE_ENABLED=true`, dashboard mode set to MT5, a successful bridge
   connection, and a successful arm with `AURUM_LIVE_ARM_PHRASE`.
2. **Engine**: bot running, kill switch clear, auto not paused, risk assessment fully green.
3. **Bridge**: valid bearer token; for a **real** account, `BRIDGE_ALLOW_LIVE=true` and
   `X-Live-Confirm: YES`. A demo account can execute without `ALLOW_LIVE` (but still requires
   dashboard arming to send the request at all).

Additional invariants:

- Switching to paper is refused while any local or remote live positions remain open.
- The mode resets to paper on restart; live cannot silently resume.
- Arming expires after 12 hours and pauses the engine.
- The kill switch is persistent across restarts and disables new entries.
- The risk governor cannot be bypassed by the AI or the browser. There is no public generic order
  endpoint.
- All live orders must include a stop-loss and take-profit. The bridge validates direction and
  minimum stop distance. Automatic protection updates use the same arm/bridge gate, can only
  improve the stop in the trade's favour, and cannot pull a target closer.

## Secrets

| Secret | Where it should live |
| --- | --- |
| Dashboard password | `.env` (mode 600) or the process environment |
| Session signing key | `.env`; 64 random hex chars |
| Bridge token | dashboard `.env` and `bridge/bridge.env`; never the browser |
| MT5 login/password/server | `bridge/bridge.env` on Windows only |
| AI API key | dashboard `.env` only |

Generated `.env`, bridge env files, databases and caches are ignored from git. Do not paste these
into screenshots, issues or logs. `setup.sh` creates `.env` with `umask 077`.

## Bridge security

- Bind `BRIDGE_HOST=127.0.0.1` by default. The bridge speaks HTTP, not HTTPS; do not expose it
  directly to the internet.
- Every route requires a bearer token of at least 16 characters; comparison is constant-time.
- Reach a remote bridge via SSH tunnel, Tailscale/WireGuard or another encrypted private network.
- If you bind the bridge to a non-loopback address, firewall it to the dashboard host only.
- `BRIDGE_ALLOW_LIVE=true` is an explicit dangerous switch. Keep it false until demo testing and
  small live verification are complete.
- The bridge process runs with your Windows user's permissions and can control MT5. Keep Windows
  patched, limit interactive access, and do not run untrusted software on that host.
- The bridge does not store request history; the dashboard's audit database is the primary log.

## Container hardening

The provided container:

- runs as non-root UID/GID `10001`;
- has a read-only root filesystem;
- drops all Linux capabilities;
- sets `no-new-privileges`;
- writes only to `/data` (named volume) and `/tmp` (tmpfs);
- exposes a health endpoint and a bounded JSON log driver.

Pin the image digest in a production environment if you build your own registry. The base image is
`python:3.12-slim`; Docker builds should be repeated and reviewed after base-image updates. Keep
host Docker access restricted: anyone who can control the Docker daemon can control the container.

## Deployment checklist

- [ ] `./setup.sh` generated unique password/secret values; `.env` is mode 600 and untracked.
- [ ] Dashboard behind HTTPS (reverse proxy) with `AURUM_COOKIE_SECURE=true` for any remote use.
- [ ] Firewall only exposes the dashboard proxy, not the bridge port.
- [ ] Bridge bind is loopback or private VPN only.
- [ ] Demo account tested: connection, view-only, arming, tiny order, close, halt, flatten.
- [ ] `AURUM_LIVE_ARM_PHRASE` changed from the default.
- [ ] Risk settings match the account size; not larger than you can tolerate losing.
- [ ] Kill switch procedure and direct MT5 access are available to you.
- [ ] Data volume backup/restore tested.
- [ ] Logs monitored for `order_failed`, `engine_error`, `login_failed`, `kill_switch`.
- [ ] If using AI, review provider data retention and calculate cost limits.
- [ ] You have independent access to close positions in MetaTrader 5 if the bridge fails.

## Incident response

1. **Stop new risk** — press **Halt** (kill switch). The engine will not accept new entries.
2. **Flatten if appropriate** — use **Flatten all** when armed, or close in MetaTrader 5 directly if
   the bridge/dashboard is unavailable. Flattening realises P&L; make the decision deliberately.
3. **Disarm** the live session, or stop the container/server.
4. **Rotate secrets** if you suspect compromise: dashboard password, session secret (invalidates
   sessions), bridge token, MT5 password. Restart bridge and dashboard.
5. **Review** the Activity log, SQLite `logs`/`trades`, and MT5 account history.
6. **Do not resume automation** until you understand the failure and have tested the fix on demo.

The bridge can only be as safe as the Windows host and the MT5 account permissions. AURUM cannot
protect against a compromised terminal, a malicious broker plugin, or account credentials leaking.

## Privacy

When the optional AI overlay is enabled, a compact numeric summary of the signal and indicators is
sent to the configured OpenAI-compatible endpoint. Account numbers, broker credentials, passwords,
cookies and bridge tokens are never sent. The overlay is optional; leaving the AI key empty keeps
all data local.

## Reporting

There is no external telemetry or update checker in this project. If you deploy AURUM for others,
add your own security contact and responsible-disclosure process.
