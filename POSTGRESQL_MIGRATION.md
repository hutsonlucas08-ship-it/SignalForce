# PostgreSQL migration status

## Current implementation

The migration branch now includes an initial dual-backend database layer:

- SQLite remains the default for local development and existing tests.
- A PostgreSQL connection adapter uses Psycopg 3 when `DATABASE_URL` is configured.
- PostgreSQL rows support both mapping access (`row["field"]`) and legacy integer indexing (`row[0]`).
- Existing qmark SQL parameters are translated safely outside quoted strings.
- The checked-in SQLite migrations are translated for the SQLite-specific constructs currently present: `AUTOINCREMENT`, `INSERT OR IGNORE`, `COLLATE NOCASE`, and the UTC `strftime` default.
- Migration checksums are calculated from the original checked-in SQL, not the translated PostgreSQL SQL.
- The paper-bot lease uses a PostgreSQL row lock when the adapter is active.
- Runtime inserts in the audited paths use portable `ON CONFLICT DO NOTHING` and `RETURNING id` forms.

This is an implementation slice, not a production-ready cutover. GitHub Actions run #206 passed the full SQLite suite (202 passed, 5 skipped) and the PostgreSQL 16 integration suite (5 passed). The PostgreSQL tests cover migration application, case-insensitive email uniqueness, generated IDs, conflict handling, the paper-bot row lock, and FastAPI health startup. A live Neon project has not yet been tested, and no production data migration has been performed.

## Target deployment (zero-cost beta)

- Application: Render free web service, subject to sleep/cold-start and resource limits.
- Database: Neon free PostgreSQL, using a pooled connection URL where appropriate.
- Public hostname: Cloudflare Worker on `workers.dev`, forwarding to the application origin.
- No persistent local disk is assumed. Customer state and encrypted credentials must live in PostgreSQL.

Free-tier quotas, inactivity policies, and availability can change. This is a low-volume beta architecture, not a production availability guarantee.

## Remaining implementation and verification

1. Complete the audit of every SQL call site and ensure all runtime statements work with both SQLite and PostgreSQL.
2. Run the full existing SQLite suite unchanged.
3. Add and run PostgreSQL integration tests for migrations, customer registration, email verification, sessions, tenant isolation, encrypted integrations, paper accounts, positions, trades, and bot state.
4. Verify migration behavior against a clean Neon database and test restart/redeploy persistence.
5. Only after the tests pass, configure the Render service with `DATABASE_URL`. Do not point production at PostgreSQL before verification.
6. If any existing SQLite customer data must be retained, design and test a separate export/import with backup and row-count verification before cutover.

## Data safety rules

- Never deploy customer accounts against ephemeral SQLite storage.
- Never run destructive schema changes or automatically copy an existing database without an explicit backup and verification.
- Keep secrets in the hosting provider's environment-variable store; never commit credentials.
- Keep real-money trading and signing out of scope. This migration is for customer accounts and paper-trading records only.
