# SignalForce FOMO Copilot — local read-only bridge

This bridge runs on your Mac and reads the FOMO session from a dedicated Chrome
remote-debugging port bound to **127.0.0.1 only**. It uses Node.js 22's built-in
WebSocket and fetch APIs; there are no npm dependencies and no Chrome extension.

## One-time setup

1. Confirm Node.js is available: `node --version`. Version 22 or newer is required.
2. Launch the dedicated FOMO Chrome profile on the local-only debugging port. If it
   is already open on port 9334, leave it running. Otherwise:

   ```bash
   mkdir -p "$HOME/fomo-copilot/chrome"
   open -na "Google Chrome" --args \
     --user-data-dir="$HOME/fomo-copilot/chrome" \
     --remote-debugging-address=127.0.0.1 \
     --remote-debugging-port=9334 \
     --new-window "https://fomo.family/r/Quickscout"
   ```

3. Sign in to FOMO yourself in that Chrome window. Never give the bridge a password,
   2FA code, seed phrase, or private key.
4. Sign in to SignalForce in your normal browser and open the **FOMO Copilot** tab.
5. Click **Generate pairing code** in SignalForce. It expires in 5 minutes.
6. In Terminal, enter this folder and start the bridge:

   ```bash
   cd signalforge/fomo_bridge
   node bridge.js
   ```

7. Paste the one-time pairing code when prompted. Input is hidden. The bridge key
   returned by SignalForce remains in process memory only; restart pairing if the
   process exits.

## What the bridge does

- Connects only to the already-open FOMO tab through `127.0.0.1:9334`.
- Observes FOMO's own network events and reads only the `Authorization: Bearer`
  header from requests to `prod-api.fomo.family`. It never prints, logs, saves,
  or sends that FOMO session token to SignalForce.
- Makes GET-only requests for the user's spot swaps and the confirmed 24h, 7d and
  30d clan leaderboards. It enforces a 1.2-second minimum gap between FOMO calls,
  widens the gap after a 429, and refreshes the session once after a 401.
- Sends only allowlisted swap and clan fields to the tenant-scoped SignalForce
  snapshot endpoint. SignalForce stores only a hash of the bridge key.
- Polls once per minute. Stop it with Ctrl+C.

## Strict hands-off boundary

The browser control surface has exactly four operations: `open(url)` (FOMO coin
and profile pages only), `screenshot()`, `read_network()`, and `reload()`.
There is no click, type, press, keyboard, mouse, DOM input, transaction, wallet,
signing, swap, buy, sell, withdraw, or deposit operation in this bridge.

FOMO auth stays on your Mac. The bridge key is separate, scoped to your SignalForce
customer account, stored as a hash server-side, expires after 30 days, and can be
revoked by **Clear imported data** in the FOMO Copilot tab.

## Verified-source limits

The spot swaps endpoint and clan leaderboard endpoints were observed returning
successful responses in the user's FOMO session. The clan leaderboard is **not**
the individual trader P&L leaderboard required for the planned whale-selection
rules. The individual leaderboard endpoint and the FOMO-to-chain feed proof remain
unverified. SignalForce must not invent individual win rates, median holds, or whale
rankings from clan data. Cross-chain and off-platform swaps remain distinguishable
from Solana memecoin trades.
