const SVC = ["youtube", "x", "booth"];
function render(s) {
  const st = document.getElementById("st");
  st.innerHTML = s.ok ? `<span class="ok">● RigBridge 연결됨</span> · ${s.last}` : `<span class="bad">● ${s.error || "연결 안 됨"}</span>${s.last ? " · " + s.last : ""}`;
  for (const k of SVC) {
    document.getElementById("e-" + k).checked = s.enabled ? s.enabled[k] !== false : true;
    const n = s.counts && s.counts[k];
    document.getElementById("n-" + k).textContent = n ? `${n}개` : "";
  }
}
chrome.runtime.sendMessage({ type: "status" }, render);
for (const k of SVC) {
  document.getElementById("e-" + k).addEventListener("change", async (ev) => {
    const cur = (await chrome.storage.local.get("enabled")).enabled || {};
    cur[k] = ev.target.checked;
    await chrome.storage.local.set({ enabled: cur });
  });
}
