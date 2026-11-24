# SignalForce FOMO Copilot Bridge (local Chrome extension)

This bridge makes read-only FOMO API requests from the browser where you are already signed in.
It does not click, type, sign, submit transactions, or place trades.

## Install

1. Use Chrome with the same profile for FOMO and SignalForce.
2. Open `chrome://extensions` and enable **Developer mode**.
3. Choose **Load unpacked** and select this `fomo_bridge` folder.
4. Sign in to FOMO and open a page that loads your account data.
5. Sign in to SignalForce in another tab in the same Chrome profile.
6. The extension observes FOMO's authenticated API requests, keeps the auth header in
   extension memory only, and polls the confirmed read-only spot-swaps and clan-leaderboard
   endpoints once per minute.
7. Keep both sites open in the same Chrome profile. Use the extension toolbar icon to
   request an immediate refresh.

## Security boundary

- FOMO authorization headers are held only in service-worker memory. They are never
  written to extension storage, logs, SignalForce, or Telegram.
- The extension uses `credentials: "omit"` for FOMO API calls and sends only allowlisted,
  sanitized swap and clan-leaderboard fields to SignalForce.
- SignalForce receives snapshots through the customer's same-origin HttpOnly session
  and CSRF-protected endpoint.
- The bridge does not request or store wallet private keys, seed phrases, or signing
  permissions. It has no transaction submission code.
- A failed/expired FOMO session stops polling until the browser observes a fresh
  authenticated FOMO request.
- All FOMO metadata is treated as untrusted data. It is rendered as text by SignalForce.

## Limitations

FOMO's API is not a stable public API. If its endpoints or authentication headers change,
the bridge will stop updating rather than guess or silently switch to another source.
The clan leaderboard is a clan leaderboard, not an individual trader profitability
leaderboard. The swap feed may include cross-chain or off-platform activity; network IDs,
token addresses, and provider fields must be interpreted before treating a row as a
Solana memecoin trade.
