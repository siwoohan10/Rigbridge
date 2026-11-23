// Shared by the content scripts: periodically collect visible cards and hand them to the background worker.
// Only card data shown on the page (title, thumbnail, link, author, price) is sent — no cookies, no account data.
window.RB = window.RB || {
  start(service, collect) {
    let lastSig = "";
    // NOTE: also sends while the tab is in the background — the user is usually in VRChat, not looking at Chrome.
    const send = () => {
      let items = [];
      try { items = collect() || []; } catch (e) { return; }
      if (!items.length) return;
      const sig = items.map(i => i.id).join(",");
      if (sig === lastSig && Date.now() - lastSent < 120000) return;   // nothing new (re-send every 2 min anyway)
      lastSig = sig;
      lastSent = Date.now();
      chrome.runtime.sendMessage({ type: "feed", service, items }).catch(() => { lastSig = ""; });
    };
    let lastSent = 0;
    let t = null;
    const soon = (ms) => { clearTimeout(t); t = setTimeout(send, ms); };
    soon(2500);
    setInterval(send, 15000);
    window.addEventListener("scroll", () => soon(1500), { passive: true });
    document.addEventListener("visibilitychange", () => soon(800));
    // single-page sites: re-run after navigation
    let path = location.pathname;
    setInterval(() => { if (location.pathname !== path) { path = location.pathname; lastSig = ""; soon(2500); } }, 1000);
  },
  text(el) { return (el && (el.innerText || el.textContent) || "").replace(/\s+/g, " ").trim(); }
};
