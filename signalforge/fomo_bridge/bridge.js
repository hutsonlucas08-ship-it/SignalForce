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
const ALLOWED_CDP_METHODS = new Set(["Network.enable", "Network.getResponseBody", "Page.enable", "Page.navigate", "Page.captureScreenshot", "Page.reload"]);

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
  constructor(tab) {
    this.tab = tab;
    this.ws = null;
    this.nextId = 0;
    this.pending = new Map();
    this.requestUrls = new Map();
    this.earlyHeaders = new Map();
    this.authHeader = null;
    this.authCapturedAt = 0;
    this.userId = null;
    this.sessionBlocked = false;
    this.authWaiters = [];
    this.leaderboardRequests = new Set();
  }

  async connect() {
    this.ws = new WebSocket(this.tab.webSocketDebuggerUrl);
    await new Promise((resolve, reject) => {
      this.ws.addEventListener("open", resolve, { once: true });
      this.ws.addEventListener("error", () => reject(new Error("Could not connect to the local Chrome debugging session.")), { once: true });
    });
    this.ws.addEventListener("message", event => this.onMessage(event.data));
    await this.send("Network.enable");
    await this.send("Page.enable");
  }

  send(method, params = {}) {
    if (!ALLOWED_CDP_METHODS.has(method)) {
      throw new Error("This bridge blocks all browser commands outside its read-only allowlist.");
    }
    const id = ++this.nextId;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }

  onMessage(raw) {
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
      this.requestUrls.set(params.requestId, params.request?.url || "");
      const early = this.earlyHeaders.get(params.requestId);
      if (early) {
        this.earlyHeaders.delete(params.requestId);
        this.captureAuth(this.requestUrls.get(params.requestId), early);
      }
      const match = (params.request?.url || "").match(/^https:\/\/prod-api\.fomo\.family\/v2\/users\/([0-9a-f-]{36})\/(?:balances|swaps)(?:\?|$)/i);
      if (match) this.userId = match[1];
      this.resolveAuthWaiters();
    } else if (message.method === "Network.requestWillBeSentExtraInfo") {
      const url = this.requestUrls.get(params.requestId);
      if (url) this.captureAuth(url, params.headers || {});
      else this.earlyHeaders.set(params.requestId, params.headers || {});
    } else if (message.method === "Network.responseReceived") {
      const url = this.requestUrls.get(params.requestId) || params.response?.url || "";
      if (url.startsWith(FOMO_API + "/") && !/\/clans\//i.test(url) &&
          /leaderboard|top.?traders|traders/i.test(url) &&
          Number(params.response?.status) === 200 &&
          /json/i.test(params.response?.mimeType || "") &&
          !this.leaderboardRequests.has(params.requestId)) {
        this.leaderboardRequests.add(params.requestId);
      }
    } else if (message.method === "Network.loadingFinished" &&
               this.leaderboardRequests.has(params.requestId)) {
      this.leaderboardRequests.delete(params.requestId);
      const url = this.requestUrls.get(params.requestId) || "";
      this.captureTraderLeaderboard(params.requestId, url).catch(() => {});
    }
  }

  async captureTraderLeaderboard(requestId, url) {
    try {
      const response = await this.send("Network.getResponseBody", { requestId });
      const raw = response.base64Encoded
        ? Buffer.from(response.body || "", "base64").toString("utf8")
        : String(response.body || "");
      if (!raw || raw.length > 500_000) return;
      const payload = JSON.parse(raw);
      const findRows = (node, depth = 0) => {
        if (!node || typeof node !== "object" || depth > 5) return null;
        for (const [key, value] of Object.entries(node)) {
          if (Array.isArray(value) && /leaderboard|top.?traders|traders|users|items/i.test(key)) return value;
        }
        for (const value of Object.values(node)) {
          if (value && typeof value === "object" && !Array.isArray(value)) {
            const nested = findRows(value, depth + 1);
            if (nested) return nested;
          }
        }
        return null;
      };
      const rows = findRows(payload);
      if (!rows?.length) return;
      const traders = rows.slice(0, 50).map(row => {
        const user = row?.user || row?.profile || row?.trader || {};
        const stats = row?.stats || row?.performance || {};
        const clean = {
          ...pick(row, [
            "rank", "pnl", "pnlUsd", "profit", "profitUsd", "roi", "roiPct",
            "winRate", "totalTrades", "numTrades", "swapCount", "totalVolume",
            "volumeUsd", "followers", "verified"
          ]),
          user: pick(user, [
            "id", "userId", "userHandle", "displayName", "followers",
            "following", "numTrades", "swapCount", "totalVolume", "verified"
          ]),
          stats: pick(stats, [
            "pnl", "pnlUsd", "profit", "roi", "roiPct", "winRate",
            "totalTrades", "numTrades", "swapCount", "totalVolume", "volumeUsd"
          ])
        };
        return clean;
      }).filter(row => Object.keys(row).some(key =>
        key !== "user" && key !== "stats" && row[key] !== undefined
      ) || Object.keys(row.user).length > 0 || Object.keys(row.stats).length > 0);
      if (!traders.length) return;
      const urlObject = new URL(url);
      const window = urlObject.searchParams.get("window") || urlObject.searchParams.get("period") || "unknown";
      await sendSnapshot("trader_leaderboard", {
        traders,
        window: String(window).slice(0, 24),
        endpoint_path: urlObject.pathname.slice(0, 180),
        note: "Captured passively from the authenticated FOMO page. Field availability varies; rankings are not independently verified."
      });
      process.stdout.write("FOMO trader leaderboard snapshot captured · " + traders.length + " rows.\n");
    } catch (_) {
      // A response that is not JSON or whose body has already expired is ignored.
    }
  }

  captureAuth(url, headers) {
    if (!String(url || "").startsWith(FOMO_API + "/")) return;
    const entry = Object.entries(headers || {}).find(([name, value]) =>
      String(name).toLowerCase() === "authorization" &&
      typeof value === "string" && value.startsWith("Bearer ")
    );
    if (entry) {
      this.authHeader = entry[1];
      this.authCapturedAt = Date.now();
      this.sessionBlocked = false;
      this.resolveAuthWaiters();
    }
  }

  resolveAuthWaiters() {
    if (!this.authHeader || !this.userId) return;
    for (const resolve of this.authWaiters.splice(0)) resolve();
  }

  // Browser surface 1/4: navigation is restricted to FOMO coin/profile pages.
  async open(url) {
    const parsed = new URL(url);
    if (parsed.protocol !== "https:" || parsed.hostname !== "fomo.family" ||
        !/(^|\/)(coin|profile)(\/|$)/i.test(parsed.pathname)) {
      throw new Error("Only FOMO coin and profile pages may be opened.");
    }
    return this.send("Page.navigate", { url: parsed.href });
  }

  // Browser surface 2/4: screenshot is read-only and stays in memory.
  async screenshot() {
    return this.send("Page.captureScreenshot", { format: "png", fromSurface: true });
  }

  // Browser surface 3/4: observe FOMO's own network requests; never print the header.
  async read_network(timeoutMs = 30_000) {
    if (this.authHeader && this.userId) return;
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error(
        "No authenticated FOMO API request observed. Sign in to FOMO and refresh its page."
      )), timeoutMs);
      this.authWaiters.push(() => { clearTimeout(timer); resolve(); });
    });
  }

  // Browser surface 4/4: reload only the already-open FOMO page.
  async reload() {
    await this.send("Page.reload", { ignoreCache: true });
  }

  async refreshSession() {
    this.authHeader = null;
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
  const tab = tabs.find(item => item.type === "page" && /^https:\/\/fomo\.family\//i.test(item.url || ""));
  if (!tab?.webSocketDebuggerUrl) {
    throw new Error("Open FOMO in the dedicated Chrome profile on port 9334, then run this bridge again.");
  }
  const browser = new ReadOnlyFomoBrowser(tab);
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
  if (!browser.authHeader) throw new Error("FOMO session is not authenticated; sign in manually and refresh the FOMO page.");
  if (!path.startsWith("/v2/")) throw new Error("Only verified FOMO read endpoints are allowed.");
  for (let attempt = 0; attempt < 3; attempt += 1) {
    await waitForFomoRateSlot();
    const response = await fetch(FOMO_API + path, {
      method: "GET",
      headers: { Accept: "application/json", Authorization: browser.authHeader || "" },
      signal: AbortSignal.timeout(15_000),
      cache: "no-store"
    });
    if (response.status === 401 && allowAuthRefresh) {
      browser.authHeader = null;
      await browser.refreshSession();
      return fomoGet(browser, path, false);
    }
    if (response.status === 401) {
      browser.authHeader = null;
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
    "outTradeId", "provider"
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
    if (browser.sessionBlocked && !browser.authHeader) {
      process.stdout.write("FOMO session paused. Sign in manually in the FOMO Chrome window; reads resume after a fresh authenticated request.\n");
      return;
    }
    if (!browser.authHeader || !browser.userId ||
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
