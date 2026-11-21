# SignalForge Core — Stage 1 (foundation)

Read-only Solana market research backend. **No wallet, no private keys, no order execution exists in this code.**
Shadow (paper) trades are stored in separate tables that the database refuses to label as live.

## Run on your Mac
Open Terminal and run these from inside the `signalforge` folder (the `cd` matters: a
"venv/bin/activate: no such file" error means you're in the wrong folder or skipped step 2).

```bash
cd signalforge
python3 -m venv venv                  # 2. creates the venv folder
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                  # then edit .env: generate two different keys (see file)
set -a; source .env; set +a
uvicorn app.main:app_factory --factory --port 8000
```
Check it: `curl localhost:8000/health`, then open http://localhost:8000/docs.

## Run in GitHub Codespaces

For Codespaces, add `SIGNALFORGE_READ_KEYS` under **Repository Settings → Secrets and variables → Codespaces**. Do not use the Actions secrets page. Codespaces injects repository secrets into the environment when the Codespace is created; after adding or changing a secret, stop and recreate the Codespace (or create a fresh Codespace) so the environment receives it. Keep the key out of chat and source control.

In the Codespaces terminal, from the repository root:

```bash
cd /workspaces/SignalForce/signalforge
python3 -m venv venv 2>/dev/null || true
source venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app_factory --factory --host 0.0.0.0 --port 8000
```

**Important:** In Codespaces, do not run `source .env` after the Codespaces secret has been injected; that can overwrite the secret with values from a local template file. The dashboard now has a public, safe `/v1/auth/status` diagnostic that reports whether a read key is loaded without revealing its value. If it reports no read key, fix the Codespaces secret injection/recreate the Codespace rather than generating more keys.

## Run the live public-data collector

Open a **second terminal** from the `signalforge` folder, activate the same virtual
environment, and run:

```bash
python -m app.sources.collector
```

The collector subscribes only to PumpPortal's free `subscribeNewToken` and
`subscribeMigration` streams. It stores validated observations in the configured
SQLite database and reconnects with backoff after network failures. It does not
subscribe to paid trade streams and does not need a wallet or private key.

An optional `PUMPPORTAL_API_KEY` environment variable can be set if your PumpPortal
account requires a key. Never commit API keys to Git.

Stop the collector with Ctrl+C.

## Automatic paper bot and Pump.fun discovery

The API server starts the paper-bot worker in **standby** on startup; it does not scan or trade until a person explicitly starts a timed session in the dashboard. Choose virtual capital and a session duration (15 minutes, 30 minutes, 1 hour, 2 hours, or 4 hours). The worker scans on a 3-second target cycle while the session is active, chooses entries itself, and manages paper exits (10% stop loss / 20% take profit). It never connects a wallet or submits a live transaction. The browser does not need to select a token. The session automatically pauses at the selected end time; open positions remain visible.

The scanner uses PumpPortal-discovered launches plus DexScreener activity snapshots.
For Pump.fun's newest callouts and trending feed, SignalForge supports the official
Pump.fun Developer API. Apply at https://developers.pump.fun and request the
`content.read` scope. Access is free for approved organisations but requires review;
there are no instant test keys. Add the approved key to your local `.env` or the
Codespaces repository secret named `PUMPFUN_API_KEY`, then restart the API server.
The key is server-side only and must never be placed in the browser or committed to Git.

When configured, SignalForge reads the Pump.fun trending home feed (filtered to
Solana coins in a $10K–$5M market-cap band) and the newest-first Solana callouts feed.
The bot still requires live DexScreener activity and BubbleMaps launch health of at
least 70 before a paper entry. Missing launch-health evidence blocks entry. This is
a conservative filter, not a guarantee that a token cannot rug or that a trade will profit.

The PumpPortal collector still runs in a second terminal as described above; it adds
new-token and migration discoveries. The bot can remain ACTIVE while making zero
trades if no token passes every filter. It stops when the API server stops or sleeps.

## Public trader tracking and private Telegram alerts

SignalForge can store public Solana wallet addresses in a watchlist and retrieve parsed
swap history through Helius. Add `HELIUS_API_KEY` to the server-side environment to
enable history retrieval. This currently provides on-chain activity, not a verified
profit leaderboard or automatic wallet-mirroring strategy; accurate trader P&L requires
position reconstruction, historical pricing, token decimals, transfers, and realized
exit accounting. Do not treat raw wallet activity as proof of trading skill.

Private Telegram notifications use a bot created through https://t.me/BotFather.
Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` as server-side secrets. The dashboard
can send a test alert, and the paper worker sends simulated entry/exit alerts. Never
place bot credentials in frontend code or commit them to Git. For a private chat, open
the newly created bot in Telegram and press Start before testing delivery.

## Test
`python -m pytest -q`

## Endpoints
| Endpoint | Key needed | Purpose |
|---|---|---|
| GET /health | none | process is up (reveals nothing else) |
| GET /v1/system/health | read | DB, halt flag, source problems, last data time |
| GET /v1/system/sources | read | each data source with ok / stale / failing / unknown |
| GET /v1/system/events | read | recent system events |
| POST /v1/research/risk-screen | read | explainable, fail-closed token risk screening (not a trade signal) |
| GET /v1/research/assets | read | recently observed asset discovery list |
| GET /v1/research/trending-now | read | strongest live activity-qualified candidates |
| GET /v1/research/pumpfun-callouts | read | newest-first Pump.fun Solana callouts (requires approved API key) |
| GET /v1/paper/bot | read | automatic paper bot heartbeat and configuration |
| POST /v1/paper/bot/config | read | pause/resume bot or set virtual capital before first trade |
| GET /v1/research/assets/{asset_id}/analysis | read | evidence freshness, source diversity, and event-kind summary |
| GET /v1/control/halt | read | kill-switch state |
| POST /v1/control/halt, /resume | **admin** | kill switch (starts HALTED) |

Give Grok only a **read** key. Keep the **admin** key to yourself.

## Remaining work
Collector supervision inside the API process, expanded token-authority and wallet-cluster screening, deeper shadow-trade analytics, Telegram alerts, Grok connection, cloud deployment, backups.
Database is SQLite for now; Stage 8 decides whether to move to Postgres for cloud hosting.


### Brain research report

- `GET /v1/research/assets/{asset_id}/report` — combines the Analyst evidence summary with conservative risk-screen status and explicit limitations. Version 1 is deterministic; it does not invent missing risk data or issue trading instructions.
