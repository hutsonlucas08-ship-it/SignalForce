# SignalForce deployment and launch checklist

## Zero-cost beta architecture

- **Web app:** Render Free Docker web service from this directory's `render.yaml`.
- **Database:** Neon Free PostgreSQL. Use the pooled connection string as `DATABASE_URL`.
- **Public address:** Render's free `onrender.com` hostname initially. A Cloudflare Worker can be added as a proxy later.
- **No persistent disk:** Render Free filesystems are ephemeral. PostgreSQL is required for all durable customer data.

This is a beta setup, not production-grade hosting. Render Free services spin down after 15 minutes without traffic and can take about a minute to wake. Render Free services have 512 MB RAM and 750 free instance hours per workspace per month. Neon's Free plan currently includes 100 compute-hours and 1 GB storage per project, with compute scaling to zero after inactivity. Check provider limits before launch because free-tier terms can change.

## Required environment variables

Set these in Render's environment dashboard. Do not commit values or paste secrets into source files.

- `DATABASE_URL`: Neon pooled PostgreSQL connection string.
- `SIGNALFORGE_READ_KEYS`: comma-separated read-only API key(s) for legacy operator endpoints.
- `SIGNALFORGE_ADMIN_KEYS`: separate admin key(s), never place these in a browser.
- `SIGNALFORCE_CREDENTIAL_ENCRYPTION_KEY`: Fernet key used to encrypt customer Helius and Telegram credentials. Generate once with:
  `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
- `SIGNALFORGE_COOKIE_SECURE=true`: requires HTTPS for customer sessions.
- `SIGNALFORGE_PUBLIC_URL`: public HTTPS base URL, without a trailing slash, used in verification links.
- `RESEND_API_KEY`: Resend transactional email API key.
- `SIGNALFORCE_EMAIL_FROM`: sender address verified in Resend, e.g. `SignalForce <verify@yourdomain.com>`.
- `HELIUS_API_KEY`, `TELEGRAM_BOT_TOKEN`, and `TELEGRAM_CHAT_ID` are optional legacy operator integrations. Customer integrations are stored separately and encrypted.

The app intentionally refuses to start on Render without `DATABASE_URL`, rather than silently writing customer data to an ephemeral SQLite file.

**Back up the encryption key separately and keep it unchanged.** If it is lost or rotated without re-encrypting stored credentials, customer integration secrets cannot be decrypted.

## Setup sequence (after PostgreSQL verification)

1. Create a Neon project on the Free plan.
2. Copy the pooled connection string from Neon and add it to Render as `DATABASE_URL`. Never put it in GitHub or this chat.
3. In Render, create a Blueprint using `signalforge/render.yaml` and connect the GitHub repository.
4. Add the remaining secret environment variables listed above.
5. Deploy only after the PostgreSQL branch's automated tests and a clean-database integration test pass.
6. Set `SIGNALFORGE_PUBLIC_URL` to the Render HTTPS origin.
7. Verify your sender domain in Resend, then send and receive a real verification email.
8. Open the Render `onrender.com` URL and test registration, verification, sign-in, customer wallet tracking, encrypted integrations, and paper trading.

The current Render Blueprint defines a Free web service and does not attach a persistent disk. Do not deploy it with an empty `DATABASE_URL`.

## Customer account registration and verification

Customers register on the public home page, receive a one-time verification link by email, click it, then sign in with the same email and password. Verification tokens are stored only as hashes, expire after one hour, and can be used once. A resend endpoint is available for pending accounts. No customer needs GitHub or Codespaces access.

Email delivery uses Resend. Registration intentionally fails with a clear configuration error until `RESEND_API_KEY`, `SIGNALFORCE_EMAIL_FROM`, and `SIGNALFORGE_PUBLIC_URL` are configured. Verify the sender domain in Resend before inviting testers.

The public home page serves the customer experience; the operator dashboard is not served at `/`. Do not publish operator API keys or grant customers access to internal operator credentials. The existing operator dashboard and APIs remain legacy operator tooling and require a separate private access design before reuse.

## Pre-launch security checklist

- [ ] PostgreSQL migration tests pass against a clean Neon database.
- [ ] Render service starts with PostgreSQL and refuses to start without it.
- [ ] HTTPS and public URL verified.
- [ ] Encryption key backed up securely and kept stable.
- [ ] Read and admin API keys are different, long, and stored only as host secrets.
- [ ] Resend sender domain verified and a real test verification email received.
- [ ] Customer registration, verification, resend, and sign-in tested end to end.
- [ ] Tenant isolation and paper-trading tests pass on the deployed build.
- [ ] Provider keys tested using customer-owned credentials.
- [ ] Privacy policy, terms, risk disclosures, password recovery, and account deletion reviewed before broad public launch.

## Trading safety

Customer paper trades are local simulations only. Customer paper endpoints do not sign transactions, connect to wallets, or place real orders. Keep any future live execution service separate, disabled by default, and subject to a separate security review.
