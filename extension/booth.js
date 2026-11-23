// BOOTH item cards (likes / wish lists / shop and search pages) → RigBridge.
RB.start("booth", () => {
  const out = [], seen = new Set();
  for (const a of document.querySelectorAll('a[href*="/items/"]')) {
    const m = a.href.match(/\/items\/(\d+)/);
    if (!m || seen.has(m[1])) continue;
    const card = a.closest("li, [class*='item-card'], [class*='ItemCard'], [class*='item_card']") || a;
    const img = card.querySelector("img[src*='pximg'], img[data-original], [data-original], img");
    let thumb = (img && (img.dataset && img.dataset.original || img.getAttribute("src"))) || "";
    if (!thumb) {
      const bg = card.querySelector("[style*='background-image']");
      const bm = bg && bg.getAttribute("style").match(/url\(["']?([^"')]+)/);
      thumb = bm ? bm[1] : "";
    }
    const title = RB.text(card.querySelector("[class*='item-name'], [class*='title'], [class*='Title']")) || (img && img.getAttribute("alt")) || a.title || "";
    if (!title || !thumb.startsWith("https://")) continue;
    seen.add(m[1]);
    out.push({
      id: m[1], url: "https://booth.pm/ko/items/" + m[1], title,
      author: RB.text(card.querySelector("[class*='shop-name'], [class*='shop__name'], [class*='ShopName']")),
      price: RB.text(card.querySelector("[class*='price'], [class*='Price']")), thumb, video: false
    });
    if (out.length >= 48) break;
  }
  return out;
});
