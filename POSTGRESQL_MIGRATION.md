# PostgreSQL migration work plan

## Current state

The application is SQLite-specific today. The database layer in `app/db.py` uses `sqlite3.Connection`, SQLite pragmas, and `executescript`. Application modules also depend on SQLite behavior, including:

- qmark (`?`) SQL parameters and SQLite row objects;
- `INSERT OR IGNORE` and `ON CONFLICT` upserts;
- `cursor.lastrowid`;
- SQLite `AUTOINCREMENT`, `strftime`, and `substr`;
- `BEGIN IMMEDIATE` for the paper-bot lease;
- SQLite migration DDL and schema-migration checksums.

Adding a PostgreSQL URL or installing a PostgreSQL driver alone would therefore **not** make this service PostgreSQL-compatible.

## Target deployment (zero-cost tier)

- Application: Render free web service, subject to its sleep/cold-start and resource limits.
- Database: Neon free PostgreSQL, with a pooled connection URL for the web process.
- Public hostname: Cloudflare Worker on `workers.dev`, forwarding to the application origin.
- No persistent local disk is assumed. All customer state and credentials must live in PostgreSQL.

Free-tier quotas, inactivity policies, and availability can change. This design is for a low-volume beta, not a production availability guarantee.

## Required implementation sequence

1. Introduce a small database interface and retain SQLite as the default local/test backend.
2. Add PostgreSQL connection support using psycopg 3 and explicit dict-row behavior.
3. Port every migration to PostgreSQL-compatible DDL without editing already-applied SQLite migrations. Keep backend-specific migration directories and checksums.
4. Audit and port all SQL call sites, especially generated IDs, conflict handling, date expressions, and the paper-bot lease/locking transaction.
5. Add PostgreSQL integration tests that exercise customer registration, email verification, sessions, tenant isolation, encrypted integrations, paper accounts, positions, trades, and bot state.
6. Run the existing SQLite suite unchanged to guard local development.
7. Only after both suites pass, change the Render blueprint to free tier and configure `DATABASE_URL`. Do not remove the SQLite file backend until the production database has been verified.

## Data safety rules

- Never deploy customer accounts against ephemeral SQLite storage.
- Never run destructive schema changes or automatically copy an existing database without an explicit backup and verification.
- Keep secrets in the hosting provider's environment-variable store; never commit credentials.
- Keep real-money trading and signing out of scope. This database migration is for customer accounts and paper-trading records only.

## Current blocker

The existing SQL surface is spread across the core store, paper simulator, paper bot, customer authentication, customer data, and customer integrations. A safe migration must update and test those paths together; a partial connection-string change is not a valid deployment.
