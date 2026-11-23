# SignalForce — Product Direction & Consolidated Requirements

**Product name:** SignalForce  
**Target domain:** https://signalforce.com  
**Repository:** SignalForce / `smart-money-tracker`  
**Current operating mode:** public-data research + paper trading only. No wallet signing or live order execution.

This document consolidates the product decisions discussed through November 23, 2026. It is the source of truth for the next product build stages.

## 1. Brand and customer experience

- Use **SignalForce** consistently in the website title, navigation, app metadata, emails, alerts, and customer-facing documentation.
- Target the public domain **signalforce.com** with HTTPS. Code branding does not register the domain or configure DNS/hosting; production cutover requires domain ownership, DNS records, hosting, TLS, and a production environment.
- Build a distinctive, professional trading terminal: compact information-dense market rows, deliberate hierarchy, consistent spacing, polished loading/empty/error states, and original visual identity rather than generic AI-template styling.
- Customers must never need access to the owner's GitHub repository, Codespaces, terminal, or server environment.

## 2. Customer accounts and private integrations

Each customer must have an individual account and isolated settings. The current shared environment-secret setup is suitable only for the present single-operator development environment; it is **not** the final customer integration architecture.

### Helius connection
1. In SignalForce Settings, the customer chooses **Connect Helius**.
2. Explain how to create a Helius account and obtain an API key from the official Helius dashboard.
3. The customer pastes their own key into a secure, authenticated connection form.
4. SignalForce sends it only to the backend over HTTPS, validates it with a test request, and stores it encrypted at rest, scoped to that account.
5. Show connected / failed / last-checked status and allow replacement or disconnect.
6. Never return the key to the browser after save, log it, put it in URLs, or share it with another account.

Official starting point: https://www.helius.dev/

### Telegram connection
1. In SignalForce Settings, the customer chooses **Connect Telegram**.
2. Link directly to https://t.me/BotFather and guide them through `/newbot`, naming the bot, and copying the bot token.
3. Guide them to open their new bot and press **Start**.
4. Explain how to obtain their private chat ID through the bot's supported Telegram setup flow.
5. They submit their own bot token and chat ID through the authenticated HTTPS form.
6. SignalForce validates the connection with a test message, stores credentials encrypted and account-scoped, and provides test, replace, and disconnect controls.
7. Never put Telegram tokens in browser JavaScript, local/session storage, public URLs, logs, or shared server-wide environment variables for customer accounts.

Official setup: https://t.me/BotFather

### Required security foundation before customer launch
- Account registration/login, secure password hashing, session management, CSRF protection, rate limits, and account recovery.
- Account-scoped encrypted credential storage with a server-managed encryption key held outside the database and source control.
- Per-account authorization on every wallet, alert, paper session, and integration endpoint; tests must prove cross-account access is denied.
- Secrets redaction in logs/errors, credential rotation and deletion, audit events, and backup encryption.
- Production database and deployment suitable for concurrent customers. The current SQLite / global API-key setup is not a finished multi-tenant SaaS architecture.
- Do not present the current Codespaces secret instructions as a customer onboarding path.

## 3. Market intelligence and discovery

- Cover Solana activity from every reliable, authorized source available to the platform, including Pump.fun, Raydium, other DEX liquidity venues, new token launches, wallet activity, whale flows, liquidity changes, and suspicious activity.
- Use provider adapters and show source, observation time, freshness, and missing-data states. Do not claim complete chain coverage when a provider does not supply it.
- Keep **Market Movers** and **Top 10 Trending Now** populated from live market-wide discovery, not only locally collected assets.
- Provide **1h / 5h / 24h** time-window views for trending and activity comparisons.
- Use compact exchange-style rows: token image and name, shortened/copyable contract, live market cap and price, volume, liquidity, buys/sells, holder count only when a source supports it, source/rank, and risk/launch warnings.
- Keep the Research & Simulation discovery panel concise, emphasizing the most recent 10 relevant memecoins rather than an oversized token catalog.
- Remove the obsolete “Latest 10 Discovered Tokens” section and do not depend on unavailable Pump.fun endpoints/API keys for the main trending experience.
- Every token card/list/detail/trade/position should show its actual token artwork whenever a reliable provider or metadata URI supplies it. Resolve artwork through supported metadata sources, cache results, handle IPFS/Arweave safely, and use a deliberate branded fallback only when no published artwork can be resolved.
- Every relevant coin should have a direct **Open on Pump.fun** link when the mint is a Pump.fun token or the link is valid. Do not label every non-Pump.fun token as a Pump.fun launch.
- Market-cap values must be sourced explicitly; do not silently substitute FDV and label it market cap. Display unavailable when no market-cap value is supplied.

## 4. Paper trading

- Keep the existing automatic paper-trading strategy as the development priority.
- Show session profit (realized + unrealized), invested this session, realized P&L, open-position P&L, number of trades, session duration, open positions, and execution history.
- Display fresh open-position quotes and explicit live market caps; distinguish the 5-second display quote refresh from the slower strategy/exit evaluation cycle.
- Keep the direct token link and token artwork in open positions and trade history.
- Continue recording the reason for every simulated entry and exit, fees, slippage assumptions, and risk-control decisions.
- Improve entry/exit responsiveness with event-driven price/liquidity updates and faster exit evaluation only after reliability, provider limits, and backtest/paper evidence support it.
- Never force a trade to make the bot look active. Zero trades is valid when no candidate qualifies.
- Measure performance honestly across enough trades and market conditions; never promise a 99.8% win rate or guaranteed profit.

## 5. Telegram notifications

- Customer alerts must route to that customer's own Telegram bot/chat, not the developer's bot.
- Support paper entries, exits, stop-loss/profit events, connection failures, and relevant wallet/research alerts with user-configurable preferences.
- The current developer test bot is a development connection only and must not become the default customer destination.

## 6. Future real-money automation — separate product and permission boundary

- Do not enable live trading as an extension of the paper bot.
- Design it as a separate, disabled-by-default system with separate configuration, permissions, risk limits, audit logs, and explicit user opt-in.
- Require a separate wallet-connection/signing architecture and clear transaction previews/limits before any live execution exists.
- Paper-trading results and Telegram delivery tests do not establish live-trading readiness.
- Never store seed phrases or private keys in ordinary settings or logs. Do not build or activate transaction signing without a separate approved design and explicit authorization.

## 7. Domain and production launch checklist

- Confirm ownership/control of `signalforce.com`.
- Choose production hosting and managed database; do not expose a development Codespace as the production service.
- Configure DNS records for the selected host, TLS/HTTPS, canonical host redirects, and environment-specific secrets.
- Configure production database migrations, encrypted backups, monitoring, error reporting, and restore testing.
- Add privacy policy, terms, risk disclosures, support/contact path, and clear statement that paper trading is simulated.
- Verify signup, login, account isolation, Helius connect/disconnect, Telegram connect/test/disconnect, and billing/access controls before public launch.

## 8. Build sequence

1. **Brand + baseline:** SignalForce naming, customer-facing copy, consolidated product requirements, and current dashboard polish.
2. **Artwork + market presentation:** resolve/cache real token art, consistent artwork in all token surfaces, accurate live market-cap labels, and reliable 1h/5h/24h market views.
3. **Multi-user foundation:** account auth, sessions, tenant isolation, encrypted per-account secrets, customer Helius and Telegram onboarding.
4. **Paper-trading reliability:** telemetry, responsive quote/exit loop, fees/slippage, diagnostics, and measured strategy evaluation.
5. **Production launch:** cloud hosting, database, domain/DNS/TLS, security, legal pages, monitoring, and backups.
6. **Separate live-trading design:** only after paper strategy validation and a separate explicit safety review.

## 9. Current development facts

- The existing repository has a working single-operator read API-key gate and global environment-based Helius/Telegram integration. It does not yet provide customer accounts or per-customer encrypted credential storage.
- The existing paper bot is simulated; it does not sign or submit real transactions.
- GeckoTerminal and DEX Screener are used for market discovery/quotes; provider coverage and rate limits remain real constraints.
- Existing token image metadata resolution exists in the API, but artwork still needs consistent use across all UI surfaces and better fallback handling.
- Domain branding in code does not itself make the public domain resolve to the app.
