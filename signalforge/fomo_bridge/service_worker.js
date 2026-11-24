// FOMO browser bridge: read-only requests only. Authentication headers remain
// in service-worker memory and are never persisted, logged, or forwarded.
const API = "https://prod-api.fomo.family";
const SIGNALFORCE = "https://signalforce-qwat.onrender.com";
let authHeaders = null;
let userId = null;
let pollBusy = false;

function captureRequest(details) {
  const match = details.url.match(/\/v2\/users\/([0-9a-f-]{36})\/(?:balances|swaps)/i);
  if (match) userId = match[1];
  const headers = {};
  for (const header of (details.requestHeaders || [])) {
    const name = String(header.name || "").toLowerCase();
    if (name === "authorization" || name === "x-auth-token") {
      if (header.value) headers[header.name] = header.value;
    }
  }
  if (Object.keys(headers).length) authHeaders = headers;
}

chrome.webRequest.onBeforeSendHeaders.addListener(
  captureRequest,
  { urls: ["https://prod-api.fomo.family/*"] },
  ["requestHeaders", "extraHeaders"]
);

chrome.runtime.onInstalled.addListener(() => {
  chrome.alarms.create("fomo-copilot-poll", { periodInMinutes: 1 });
});
chrome.runtime.onStartup.addListener(() => {
  chrome.alarms.create("fomo-copilot-poll", { periodInMinutes: 1 });
});
chrome.alarms.onAlarm.addListener(alarm => {
  if (alarm.name === "fomo-copilot-poll") pollFomo();
});

async function getJson(path) {
  if (!authHeaders) throw new Error("Waiting for an authenticated FOMO API request.");
  const response = await fetch(API + path, {
    method: "GET",
    headers: { Accept: "application/json", ...authHeaders },
    credentials: "omit",
    cache: "no-store"
  });
  if (response.status === 401 || response.status === 403) {
    authHeaders = null;
    throw new Error("FOMO session expired. Sign in to FOMO again.");
  }
  if (!response.ok) throw new Error("FOMO read request failed (" + response.status + ").");
  return response.json();
}

function pick(object, keys) {
  const out = {};
  for (const key of keys) {
    if (object && object[key] !== undefined && object[key] !== null) out[key] = object[key];
  }
  return out;
}

function cleanSwaps(body) {
  const rows = body?.responseObject?.swaps;
  if (!Array.isArray(rows)) return { swaps: [], note: "FOMO did not return a swaps array." };
  const fields = [
    "id", "createdAt", "display", "displayPriceUsd",
    "humanUsdAmountIn", "humanUsdAmountOut", "inAmount", "inHumanAmount",
    "inNetworkId", "inTokenAddress", "inTradeId", "isCrossmint",
    "isOffPlatform", "networkId", "outAmount", "outHumanAmount",
    "outNetworkId", "outTokenAddress", "outTradeId", "provider"
  ];
  return {
    swaps: rows.slice(0, 100).map(row => pick(row, fields)),
    has_next_page: Boolean(body?.responseObject?.hasNextPage),
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
        user: pick(member.user || {}, ["displayName", "followers", "following", "numTrades", "swapCount", "totalVolume", "userHandle", "verified"])
      })) : []
    }))
  };
}

async function pushSnapshot(snapshotType, payload) {
  const tabs = await chrome.tabs.query({ url: SIGNALFORCE + "/*" });
  const tab = tabs.find(item => item.url && item.url.startsWith(SIGNALFORCE));
  if (!tab?.id) return;
  await chrome.tabs.sendMessage(tab.id, {
    type: "FOMO_COPILOT_SNAPSHOT",
    snapshot_type: snapshotType,
    observed_at: new Date().toISOString(),
    payload
  });
}

async function pollFomo() {
  if (pollBusy || !authHeaders || !userId) return;
  pollBusy = true;
  try {
    const jobs = [
      ["spot_swaps", async () => {
        let cursor = null;
        const swaps = [];
        let hasNextPage = true;
        for (let page = 0; page < 4 && hasNextPage; page += 1) {
          const query = new URLSearchParams({ swapType: "spot" });
          if (cursor) query.set("lastSwapIdV2", cursor);
          const data = await getJson("/v2/users/" + encodeURIComponent(userId) + "/swaps?" + query.toString());
          const response = data?.responseObject || {};
          if (Array.isArray(response.swaps)) swaps.push(...response.swaps);
          hasNextPage = Boolean(response.hasNextPage);
          cursor = response.swaps?.length ? response.swaps[response.swaps.length - 1].id : null;
          if (!cursor) break;
        }
        return cleanSwaps({ responseObject: { swaps, hasNextPage } });
      }],
      ["clans_24h", async () => cleanClans(await getJson("/v2/clans/leaderboard?window=24h&limit=50"))],
      ["clans_7d", async () => cleanClans(await getJson("/v2/clans/leaderboard?window=7d&limit=50"))],
      ["clans_30d", async () => cleanClans(await getJson("/v2/clans/leaderboard?window=30d&limit=50"))]
    ];
    for (const [type, load] of jobs) {
      try {
        await pushSnapshot(type, await load());
      } catch (_) {
        // One unavailable FOMO endpoint must not block the other read-only feeds.
      }
    }
  } finally {
    pollBusy = false;
  }
}

// A manual refresh from the extension toolbar never clicks or types in FOMO.
chrome.action.onClicked.addListener(() => pollFomo());
