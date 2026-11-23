# SignalForce deployment and launch checklist

## Included in this repository

- Docker image based on Python 3.12, running as a non-root user.
- Health check at `/health`.
- Render Blueprint at `render.yaml` with a persistent disk mounted at `/var/data`.
- SQLite database path configured on that persistent disk.
- Secrets are declared as dashboard-managed environment variables, never committed here.

## Required environment variables

Set these in the hosting provider's secret/environment dashboard:

- `SIGNALFORGE_READ_KEYS`: comma-separated read-only API key(s) for the legacy operator dashboard.
- `SIGNALFORGE_ADMIN_KEYS`: separate admin key(s), never place these in a browser.
- `SIGNALFORCE_CREDENTIAL_ENCRYPTION_KEY`: Fernet key used to encrypt customer Helius and Telegram credentials. Generate once with:
  `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
- `SIGNALFORGE_COOKIE_SECURE=true`: requires HTTPS for customer sessions.
- `SIGNALFORGE_PUBLIC_URL`: the public HTTPS base URL, with no trailing slash, used in verification links.
- `RESEND_API_KEY`: Resend transactional email API key.
- `SIGNALFORCE_EMAIL_FROM`: sender address verified in Resend, e.g. `SignalForce <verify@yourdomain.com>`.
- `HELIUS_API_KEY`, `TELEGRAM_BOT_TOKEN`, and `TELEGRAM_CHAT_ID` are optional legacy operator integrations. Customer integrations are stored separately and encrypted.

**Back up the encryption key separately and keep it unchanged.** If it is lost or rotated without re-encrypting stored credentials, customer integration secrets cannot be decrypted. Back up the persistent database and test restores before launch.

## Render deployment

1. Push the `smart-money-tracker` branch to GitHub.
2. In Render, create a new Blueprint and select this repository's `signalforge/render.yaml`.
3. Enter all required secret values in Render. Do not paste secrets into GitHub source files or customer-facing pages.
4. Deploy and wait for the `/health` check to pass.
5. Set `SIGNALFORGE_PUBLIC_URL` to the deployed HTTPS origin and verify the sender domain in Resend.
6. Open `https://YOUR-SERVICE.onrender.com/` for the public SignalForce customer experience. Fresh Solana launches appear above account sign-in.
6. Add a custom domain and verify HTTPS before inviting customers.

The Blueprint uses a paid Starter service and a persistent disk. Confirm current hosting prices and region availability in Render before creating the service.

## Customer account registration and verification

Customers register on the public home page, receive a one-time verification link by email, click it, then sign in with the same email and password. Verification tokens are stored only as hashes, expire after one hour, and can be used once. A resend endpoint is available for pending accounts. No customer needs GitHub or Codespaces access.

Email delivery uses Resend. Registration intentionally fails with a clear configuration error until `RESEND_API_KEY`, `SIGNALFORCE_EMAIL_FROM`, and `SIGNALFORGE_PUBLIC_URL` are configured. Verify the sender domain in Resend before inviting testers.

The public home page now serves the customer experience; the former operator dashboard HTML is no longer served at `/`. Do not publish operator API keys or grant customers access to internal operator credentials. The existing operator dashboard and its APIs remain legacy operator tooling and require a separate private deployment/access design before reuse.

Password recovery, account deletion, and production abuse monitoring remain outstanding. Run a small invite-only beta after email delivery, HTTPS, and persistent storage are configured.

## Pre-launch security checklist

- [ ] HTTPS and custom domain verified.
- [ ] Persistent database backups configured and a restore tested.
- [ ] Encryption key backed up securely and kept stable.
- [ ] Read and admin API keys are different, long, and stored only as host secrets.
- [ ] Resend sender domain verified and test verification email received.
- [ ] Customer registration, verification, resend, and sign-in tested end to end.
- [ ] Password reset and account deletion workflows implemented before broad public launch.
- [ ] Rate limits, logging, monitoring, and abuse alerts reviewed under production traffic.
- [ ] Tenant isolation and paper trading tests pass on the deployed build.
- [ ] Provider keys tested using customer-owned credentials.
- [ ] Privacy policy, terms, and risk disclosures reviewed before accepting customers.

## Trading safety

Customer paper trades are local simulations only. The customer paper endpoints do not sign transactions, connect to wallets, or place real orders. Keep any future live execution service separate, disabled by default, and subject to a separate security review.
