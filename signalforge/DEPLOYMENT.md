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
- `HELIUS_API_KEY`, `TELEGRAM_BOT_TOKEN`, and `TELEGRAM_CHAT_ID` are optional legacy operator integrations. Customer integrations are stored separately and encrypted.

**Back up the encryption key separately and keep it unchanged.** If it is lost or rotated without re-encrypting stored credentials, customer integration secrets cannot be decrypted. Back up the persistent database and test restores before launch.

## Render deployment

1. Push the `smart-money-tracker` branch to GitHub.
2. In Render, create a new Blueprint and select this repository's `signalforge/render.yaml`.
3. Enter all required secret values in Render. Do not paste secrets into GitHub source files or customer-facing pages.
4. Deploy and wait for the `/health` check to pass.
5. Open `https://YOUR-SERVICE.onrender.com/account` for the customer workspace and `/` for the existing operator dashboard.
6. Add a custom domain and verify HTTPS before inviting customers.

The Blueprint uses a paid Starter service and a persistent disk. Confirm current hosting prices and region availability in Render before creating the service.

## Customer account registration

Beta self-registration creates an active account immediately. Customers can create an account and sign in using the same email address and password; no administrator activation or Codespaces access is required.

Email ownership is not verified yet, and password reset/account recovery is not implemented. Treat this as a controlled beta, not production-ready public registration. Before broad public launch, add email verification, password recovery, account deletion, abuse monitoring, and an administrator support workflow. The protected administrator activation endpoint remains available for administrative account controls.

## Pre-launch security checklist

- [ ] HTTPS and custom domain verified.
- [ ] Persistent database backups configured and a restore tested.
- [ ] Encryption key backed up securely and kept stable.
- [ ] Read and admin API keys are different, long, and stored only as host secrets.
- [ ] Customer account activation and support process established.
- [ ] Email verification, password reset, and account deletion workflows implemented before public launch.
- [ ] Rate limits, logging, monitoring, and abuse alerts reviewed under production traffic.
- [ ] Tenant isolation and paper trading tests pass on the deployed build.
- [ ] Provider keys tested using customer-owned credentials.
- [ ] Privacy policy, terms, and risk disclosures reviewed before accepting customers.

## Trading safety

Customer paper trades are local simulations only. The customer paper endpoints do not sign transactions, connect to wallets, or place real orders. Keep any future live execution service separate, disabled by default, and subject to a separate security review.
