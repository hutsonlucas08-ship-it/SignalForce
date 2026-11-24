#!/usr/bin/env node
/**
 * Local, read-only FOMO bridge for SignalForce.
 * Requires Node.js 22+ (built-in fetch and WebSocket; no npm packages).
 * Browser control surface is limited to open(), screenshot(), read_network(), reload().
 * No DOM input, clicking, typing, signing, or transaction submission exists here.
 */
const CHROME_DEBUG = "http://127.0.0.1:9334";
const FOMO_API = "https://prod-api.fomo.family";
const SIGNALFORCE = "https://signalforce-qwat.onrender.com";
const POLL_MS = 60_000;
const MAX_SWAP_PAGES = 4;
const BASE_GAP_MS = 1_200;
const MAX_GAP_MS = 20_000;
const ALLOWED_CDP_METHODS = new Set(["Network.enable", "Page.enable", "Page.navigate", "Page.captureScreenshot", "Page.reload"]);

const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
let bridgeKey = "";
let rateGapMs = BASE_GAP_MS;
let lastFomoCallAt = 0;
let pollRunning = false;

function hiddenInput(promptText) {
  return new Promise((resolve, reject) => {
    const input = process.stdin;
    if (!input.isTTY || typeof input.setRawMode !== "function") {
      reject(new Error("Run this command in a normal Terminal window so the pairing code can be entered privately."));
      return;
    }
    process.stdout.write(promptText);
    input.setRawMode(true);
    input.resume();
    let value = "";
    const finish = result => {
      input.removeListener("data", onData);
      input.setRawMode(false);
      input.pause();
      process.stdout.write("\n");
      resolve(result);
    };
    const onData = chunk => {
      for (const char of chunk.toString("utf8")) {
        if (char === "\u0003") {
          input.setRawMode(false);
          process.stdout.write("\n");
          reject(new Error("Cancelled."));
          return;
        }
        if (char === "\r" || char === "\n") {
          finish(value.trim());
          return;
        }
        if (char === "\u007f" || char === "\b") value = value.slice(0, -1);
        else if (char >= " " && char <= "~") value += char;
      }
    };
    input.on("data", onData);
  });
}

class ReadOnlyFomoBrowser {
  constructor(pageTab, networkTabs) {
    this.pageTab = pageTab;
    this.networkTabs = networkTabs;
    this.pageWs = null;
    this.networkSockets = [];
    this.nextId = 0;
    this.pending = new Map();
    this.authHeader = null;
    this.cookieHeader = null; // Kept in local memory only; never included in snapshots.
    this.authCapturedAt = 0;
    this.userId = null;
    this.sessionBlocked = false;
    this.authWaiters = [];
  }

  async connect() {
    // Listen to the FOMO page AND its service-worker targets. FOMO may issue
    // authenticated API calls from a service worker rather than the page.
    for (const tab of this.networkTabs) {
      const ws = new WebSocket(tab.webSocketDebuggerUrl);
      await new Promise((resolve, reject) => {
        ws.addEventListener("open", resolve, { once: true });
        ws.addEventListener("error", () => reject(new Error("Could not connect to a local FOMO Chrome target.")), { once: true });
      });
      const state = { ws, requestUrls: new Map(), earlyHeaders: new Map(), isPage: tab.id === this.pageTab.id };
      ws.addEventListener("message", event => this.onMessage(state, event.data));
      this.networkSockets.push(state);
      await this.send(state, "Network.enable");
      if (state.isPage) {
        this.pageWs = ws;
        await this.send(state, "Page.enable");
      }
    }
    if (!this.pageWs) throw new Error("Could not attach to the FOMO page target.");
  }

  send(state, method, params = {}) {
    if (!ALLOWED_CDP_METHODS.has(method)) {
      throw new Error("This bridge blocks all browser commands outside its read-only allowlist.");
    }
    const id = ++this.nextId;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      state.ws.send(JSON.stringify({ id, method, params }));
    });
  }

  onMessage(state, raw) {
    let message;
    try { message = JSON.parse(String(raw)); } catch (_) { return; }
    if (message.id) {
      const waiter = this.pending.get(message.id);
      if (!waiter) return;
      this.pending.delete(message.id);
      if (message.error) waiter.reject(new Error(message.error.message || "Chrome debugging command failed."));
      else waiter.resolve(message.result || {});
      return;
    }
    const params = message.params || {};
    if (message.method === "Network.requestWillBeSent") {
      const url = params.request?.url || "";
      state.requestUrls.set(params.requestId, url);
      const early = state.earlyHeaders.get(params.requestId);
      if (early) {
        state.earlyHeaders.delete(params.requestId);
        this.captureCredentials(url, early);
      }
      const match = url.match(/^https:\/\/prod-api\.fomo\.family\/v2\/users\/([0-9a-f-]{36})\/(?:balances|swaps)(?:\?|$)/i);
      if (match) this.userId = match[1];
      this.resolveAuthWaiters();
    } else if (message.method === "Network.requestWillBeSentExtraInfo") {
      const url = state.requestUrls.get(params.requestId);
      if (url) this.captureCredentials(url, params.headers || {});
      else state.earlyHeaders.set(params.requestId, params.headers || {});
    }
  }

  captureCredentials(url, headers) {
    if (!String(url || "").startsWith(FOMO_API + "/")) return;
    const entries = Object.entries(headers || {});
    const authorization = entries.find(([name, value]) =>
      String(name).toLowerCase() === "authorization" &&
      typeof value === "string" && value.startsWith("Bearer ")
    );
    const cookie = entries.find(([name, value]) =>
      String(name).toLowerCase() === "cookie" &&
      typeof value === "string" && value.length > 0
    );
    if (authorization) this.authHeader = authorization[1];
    if (cookie) this.cookieHeader = cookie[1];
    if (authorization || cookie) {
      // Credentials stay in this local process and are used only for FOMO GET requests.
      // Never log them or include them in SignalForce snapshots.
      this.authCapturedAt = Date.now();
      this.sessionBlocked = false;
      this.resolveAuthWaiters();
    }
  }

  hasSessionCredential() {
    return Boolean(this.authHeader || this.cookieHeader);
  }

  resolveAuthWaiters() {
    if (!this.hasSessionCredential() || !this.userId) return;
    for (const resolve of this.authWaiters.splice(0)) resolve();
  }

  async open(url) {
    const parsed = new URL(url);
    if (parsed.protocol !== "https:" || parsed.hostname !== "fomo.family" ||
        !/(^|\/)(coin|profile)(\/|$)/i.test(parsed.pathname)) {
      throw new Error("Only FOMO coin and profile pages may be opened.");
    }
    const pageState = this.networkSockets.find(state => state.isPage);
    return this.send(pageState, "Page.navigate", { url: parsed.href });
  }

  async screenshot() {
    const pageState = this.networkSockets.find(state => state.isPage);
    return this.send(pageState, "Page.captureScreenshot", { format: "png", fromSurface: true });
  }

  async read_network(timeoutMs = 45_000) {
    if (this.hasSessionCredential() && this.userId) return;
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error(
        "No authenticated FOMO balances/swaps request observed in the page or service worker. Open your FOMO profile or transactions and refresh."
      )), timeoutMs);
      this.authWaiters.push(() => { clearTimeout(timer); resolve(); });
    });
  }

  async reload() {
    const pageState = this.networkSockets.find(state => state.isPage);
    return this.send(pageState, "Page.reload", { ignoreCache: true });
  }

  async refreshSession() {
    this.authHeader = null;
    this.cookieHeader = null;
    this.userId = null;
    try {
      await this.reload();
      await this.read_network();
    } catch (error) {
      this.sessionBlocked = true;
      throw error;
    }
  }
}

async function connectFomoBrowser() {
  const response = await fetch(CHROME_DEBUG + "/json/list");
  if (!response.ok) throw new Error("Chrome debugging endpoint is unavailable on 127.0.0.1:9334.");
  const tabs = await response.json();
  const pageTab = tabs.find(item => item.type === "page" && /^https:\/\/fomo\.family\//i.test(item.url || ""));
  if (!pageTab?.webSocketDebuggerUrl) {
    throw new Error("Open FOMO in the dedicated Chrome profile on port 9334, then run this bridge again.");
  }
  const networkTabs = tabs.filter(item =>
    item.webSocketDebuggerUrl &&
    ((item.type === "page" && /^https:\/\/fomo\.family\//i.test(item.url || "")) ||
     (item.type === "service_worker" && /^https:\/\/fomo\.family\//i.test(item.url || "")))
  );
  const browser = new ReadOnlyFomoBrowser(pageTab, networkTabs);
  await browser.connect();
  return browser;
}

async function pairBridge(pairingCode) {
  const response = await fetch(SIGNALFORCE + "/v1/customer/fomo-copilot/pair", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ pairing_code: pairingCode })
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok || !body.bridge_key) {
    throw new Error(body.detail || "SignalForce pairing failed. Generate a fresh code in the FOMO Copilot tab.");
  }
  return body.bridge_key;
}

async function waitForFomoRateSlot() {
  const wait = Math.max(0, rateGapMs - (Date.now() - lastFomoCallAt));
  if (wait) await sleep(wait);
  lastFomoCallAt = Date.now();
}

async function fomoGet(browser, path, allowAuthRefresh = true) {
  if (!browser.hasSessionCredential()) throw new Error("FOMO session is not authenticated; sign in manually and refresh the FOMO page.");
  if (!path.startsWith("/v2/")) throw new Error("Only verified FOMO read endpoints are allowed.");
  for (let attempt = 0; attempt < 3; attempt += 1) {
    await waitForFomoRateSlot();
    const requestHeaders = { Accept: "application/json" };
    if (browser.authHeader) requestHeaders.Authorization = browser.authHeader;
    if (browser.cookieHeader) requestHeaders.Cookie = browser.cookieHeader;
    const response = await fetch(FOMO_API + path, {
      method: "GET",
      headers: requestHeaders,
      signal: AbortSignal.timeout(15_000),
      cache: "no-store"
    });
    if (response.status === 401 && allowAuthRefresh) {
      browser.authHeader = null;
      browser.cookieHeader = null;
      await browser.refreshSession();
      return fomoGet(browser, path, false);
    }
    if (response.status === 401) {
      browser.authHeader = null;
      browser.cookieHeader = null;
      browser.sessionBlocked = true;
      throw new Error("FOMO session expired after one refresh attempt. Sign in manually in Chrome; reads are paused.");
    }
    if (response.status === 429) {
      rateGapMs = Math.min(MAX_GAP_MS, rateGapMs * 2);
      throw new Error("FOMO rate limit reached; the bridge widened its request interval.");
    }
    if (response.ok) {
      rateGapMs = Math.max(BASE_GAP_MS, Math.floor(rateGapMs * 0.9));
      return response.json();
    }
    if (response.status >= 500 && attempt < 2) {
      await sleep(500 * (attempt + 1));
      continue;
    }
    throw new Error("FOMO read request failed (" + response.status + ").");
  }
  throw new Error("FOMO read request failed after retries.");
}

function pick(source, fields) {
  const output = {};
  for (const field of fields) {
    if (source && source[field] !== undefined && source[field] !== null) output[field] = source[field];
  }
  return output;
}

function cleanSwaps(rows, hasNextPage) {
  const fields = [
    "id", "createdAt", "displayPriceUsd", "humanUsdAmountIn",
    "humanUsdAmountOut", "inAmount", "inHumanAmount", "inNetworkId",
    "inTokenAddress", "inTradeId", "isCrossmint", "isOffPlatform", "networkId",
    "outAmount", "outHumanAmount", "outNetworkId", "outTokenAddress",
    "outTradeId", "provider", "signature", "txHash", "transactionHash"
  ];
  return {
    swaps: rows.slice(0, 100).map(row => {
      const clean = pick(row, fields);
      if (row.display && typeof row.display === "object") clean.display = pick(row.display, ["side"]);
      else if (typeof row.display === "string") clean.display = row.display;
      return clean;
    }),
    has_next_page: Boolean(hasNextPage),
    count: Math.min(rows.length, 100)
  };
}

function cleanClans(body) {
  const rows = body?.responseObject?.leaderboard;
  if (!Array.isArray(rows)) return { clans: [], note: "FOMO did not return a clan leaderboard array." };
  return {
    clans: rows.slice(0, 50).map(row => ({
      ...pick(row, ["id", "name", "description", "memberCount", "pnl", "rank"]),
      topMembers: Array.isArray(row.topMembers) ? row.topMembers.slice(0, 5).map(member => ({
        ...pick(member, ["pnl", "role"]),
        user: pick(member.user || {}, [
          "displayName", "followers", "following", "numTrades", "swapCount",
          "totalVolume", "userHandle", "verified"
        ])
      })) : []
    }))
  };
}

async function sendSnapshot(snapshotType, payload) {
  const response = await fetch(SIGNALFORCE + "/v1/customer/fomo-copilot/bridge-snapshot", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Fomo-Bridge-Key": bridgeKey
    },
    body: JSON.stringify({
      snapshot_type: snapshotType,
      observed_at: new Date().toISOString(),
      payload
    }),
    signal: AbortSignal.timeout(15_000)
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || "SignalForce did not accept the sanitized snapshot.");
}

async function loadSpotSwaps(browser) {
  const swaps = [];
  let cursor = null;
  let hasNextPage = true;
  for (let page = 0; page < MAX_SWAP_PAGES && hasNextPage; page += 1) {
    const query = new URLSearchParams({ swapType: "spot" });
    if (cursor) query.set("lastSwapIdV2", cursor);
    const data = await fomoGet(browser,
      "/v2/users/" + encodeURIComponent(browser.userId) + "/swaps?" + query.toString());
    const response = data?.responseObject || {};
    if (Array.isArray(response.swaps)) swaps.push(...response.swaps);
    hasNextPage = Boolean(response.hasNextPage);
    cursor = response.swaps?.length ? response.swaps[response.swaps.length - 1].id : null;
    if (!cursor) break;
  }
  return cleanSwaps(swaps, hasNextPage);
}

async function pollOnce(browser) {
  if (pollRunning) return;
  pollRunning = true;
  try {
    if (browser.sessionBlocked && !browser.hasSessionCredential()) {
      process.stdout.write("FOMO session paused. Sign in manually in the FOMO Chrome window; reads resume after a fresh authenticated request.\n");
      return;
    }
    if (!browser.hasSessionCredential() || !browser.userId ||
        Date.now() - browser.authCapturedAt > 55 * 60 * 1000) {
      await browser.refreshSession();
    }
    const jobs = [
      ["spot_swaps", () => loadSpotSwaps(browser)],
      ["clans_24h", async () => cleanClans(await fomoGet(browser, "/v2/clans/leaderboard?window=24h&limit=50"))],
      ["clans_7d", async () => cleanClans(await fomoGet(browser, "/v2/clans/leaderboard?window=7d&limit=50"))],
      ["clans_30d", async () => cleanClans(await fomoGet(browser, "/v2/clans/leaderboard?window=30d&limit=50"))]
    ];
    for (const [type, load] of jobs) {
      try {
        await sendSnapshot(type, await load());
      } catch (error) {
        process.stdout.write(type + ": " + error.message + "\n");
      }
    }
    process.stdout.write("FOMO snapshots checked · next poll in 60 seconds.\n");
  } finally {
    pollRunning = false;
  }
}

async function main() {
  const major = Number(process.versions.node.split(".")[0]);
  if (major < 22 || typeof WebSocket === "undefined") {
    throw new Error("Node.js 22 or newer is required. No npm packages are used.");
  }
  const pairingCode = await hiddenInput("Paste the 5-minute SignalForce pairing code (input hidden): ");
  if (!pairingCode) throw new Error("No pairing code entered.");
  bridgeKey = await pairBridge(pairingCode);
  const browser = await connectFomoBrowser();
  await browser.reload();
  await browser.read_network();
  process.stdout.write("Local FOMO bridge paired. FOMO auth stays on this Mac; read-only mode is enforced.\n");
  await pollOnce(browser);
  setInterval(() => pollOnce(browser).catch(error => {
    process.stdout.write("Bridge paused: " + error.message + "\n");
  }), POLL_MS);
}

main().catch(error => {
  process.stderr.write(error.message + "\n");
  process.exitCode = 1;
});
