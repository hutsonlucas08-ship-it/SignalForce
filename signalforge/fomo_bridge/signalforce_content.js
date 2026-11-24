// Runs only on the SignalForce origin. It forwards sanitized snapshots using
// the customer's existing HttpOnly session cookie and CSRF token.
chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (!message || message.type !== "FOMO_COPILOT_SNAPSHOT") return;
  (async () => {
    const csrf = sessionStorage.getItem("sf_customer_csrf") || "";
    if (!csrf) throw new Error("Sign in to SignalForce before pairing FOMO.");
    const response = await fetch("/v1/customer/fomo-copilot/snapshot", {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": csrf
      },
      body: JSON.stringify({
        snapshot_type: message.snapshot_type,
        observed_at: message.observed_at,
        payload: message.payload
      })
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(body.detail || "SignalForce rejected the FOMO snapshot.");
    }
    sendResponse({ ok: true });
  })().catch(error => sendResponse({ ok: false, error: error.message }));
  return true;
});
