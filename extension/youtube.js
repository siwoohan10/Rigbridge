// YouTube home (and /feed/*) recommendations → RigBridge.
RB.start("youtube", () => {
  if (location.pathname !== "/" && !location.pathname.startsWith("/feed/")) return [];
  const out = [], seen = new Set();
  for (const a of document.querySelectorAll('a[href*="/watch?v="]')) {
    const m = a.href.match(/[?&]v=([\w-]{11})/);
    if (!m || seen.has(m[1])) continue;
    const box = a.closest("ytd-rich-item-renderer, yt-lockup-view-model, ytd-video-renderer, ytd-grid-video-renderer");
    if (!box || box.closest("ytd-ad-slot-renderer, ytd-in-feed-ad-layout-renderer")) continue;
    const boxText = RB.text(box);
    if (/회원 전용|Members only|メンバー限定/.test(boxText)) continue;   // members-only videos cannot be played
    const titleEl = box.querySelector("#video-title, h3 a, h3, [id='video-title-link']");
    const title = RB.text(titleEl) || a.title || a.getAttribute("aria-label") || "";
    if (!title) continue;
    const authorEl = box.querySelector("ytd-channel-name #text, ytd-channel-name a, a[href^='/@'], .yt-content-metadata-view-model-wiz__metadata-text, .yt-content-metadata-view-model__metadata-text");
    seen.add(m[1]);
    out.push({
      id: m[1], url: "https://www.youtube.com/watch?v=" + m[1], title,
      author: RB.text(authorEl), thumb: "https://i.ytimg.com/vi/" + m[1] + "/hqdefault.jpg", video: true
    });
    if (out.length >= 48) break;
  }
  return out;
});
