// Relays cards from the content scripts to RigBridge on this PC (127.0.0.1 only).
let status = { ok: false, last: "", counts: {}, error: "아직 보낸 적 없음" };

async function settings() {
  const s = await chrome.storage.local.get(["port", "enabled"]);
  return { port: s.port || 8810, enabled: Object.assign({ youtube: true, x: true, booth: true }, s.enabled || {}) };
}

async function post(service, items) {
  const { port, enabled } = await settings();
  if (enabled[service] === false) return { ok: false, skipped: true };
  try {
    const r = await fetch(`http://127.0.0.1:${port}/pad/feed`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ service, items })
    });
    const j = await r.json();
    status = { ok: !!j.ok, last: new Date().toLocaleTimeString(), counts: j.counts || {}, error: j.error || "" };
    const total = Object.values(status.counts).reduce((a, b) => a + b, 0);
    chrome.action.setBadgeBackgroundColor({ color: j.ok ? "#3a8f5c" : "#b8433a" });
    chrome.action.setBadgeText({ text: j.ok ? String(total) : "!" });
    return j;
  } catch (e) {
    status = { ok: false, last: new Date().toLocaleTimeString(), counts: {}, error: "RigBridge에 연결할 수 없어요 (켜져 있나요?)" };
    chrome.action.setBadgeBackgroundColor({ color: "#b8433a" });
    chrome.action.setBadgeText({ text: "!" });
    return { ok: false };
  }
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg && msg.type === "feed" && Array.isArray(msg.items)) {
    post(String(msg.service), msg.items.slice(0, 60)).then(sendResponse);
    return true;
  }
  if (msg && msg.type === "status") {
    settings().then(s => sendResponse(Object.assign({}, status, { enabled: s.enabled, port: s.port })));
    return true;
  }
  return false;
});
