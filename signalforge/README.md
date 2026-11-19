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

## Test
`python -m pytest -q`  (15 tests)

## Endpoints
| Endpoint | Key needed | Purpose |
|---|---|---|
| GET /health | none | process is up (reveals nothing else) |
| GET /v1/system/health | read | DB, halt flag, source problems, last data time |
| GET /v1/system/sources | read | each data source with ok / stale / failing / unknown |
| GET /v1/system/events | read | recent system events |
| GET /v1/control/halt | read | kill-switch state |
| POST /v1/control/halt, /resume | **admin** | kill switch (starts HALTED) |

Give Grok only a **read** key. Keep the **admin** key to yourself.

## Not done yet (later stages)
Data collectors, analysis, shadow-trade engine, dashboard, Telegram, Grok connection, cloud deployment, backups.
Database is SQLite for now; Stage 8 decides whether to move to Postgres for cloud hosting.
