// X (Twitter) home timeline → RigBridge. Promoted posts are skipped.
RB.start("x", () => {
  if (!/^\/(home)?$/.test(location.pathname)) return [];
  const out = [], seen = new Set();
  for (const art of document.querySelectorAll('article[data-testid="tweet"]')) {
    if (art.querySelector('[data-testid="placementTracking"]') || /Promoted|프로모션|プロモーション/.test(RB.text(art.querySelector('[data-testid="User-Name"]')?.parentElement))) continue;
    const timeLink = art.querySelector('a[href*="/status/"] time')?.closest("a");
    const m = timeLink && timeLink.getAttribute("href").match(/^\/([^/]+)\/status\/(\d+)/);
    if (!m || seen.has(m[2])) continue;
    const media = art.querySelector('img[src*="pbs.twimg.com/media"], img[src*="ext_tw_video_thumb"], img[src*="amplify_video_thumb"], video[poster]');
    const video = !!art.querySelector('[data-testid="videoPlayer"], [data-testid="videoComponent"], video');
    const text = RB.text(art.querySelector('[data-testid="tweetText"]'));
    const author = RB.text(art.querySelector('[data-testid="User-Name"]')).split("@")[0].trim();
    if (!text && !media) continue;
    seen.add(m[2]);
    out.push({
      id: m[2], url: "https://x.com/" + m[1] + "/status/" + m[2], title: text || "(이미지)",
      author: author || "@" + m[1], thumb: media ? (media.poster || media.src) : "", video
    });
    if (out.length >= 48) break;
  }
  return out;
});
