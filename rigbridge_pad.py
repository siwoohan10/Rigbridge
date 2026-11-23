"""RigBridge 'Pad' module: browser recommendations (YouTube / X / BOOTH) → the room tablet in VRChat.

- The browser extension (RigBridge/extension) POSTs cards it sees on the user's own logged-in pages to
  http://127.0.0.1:<http_port>/pad/feed  (only requests from a browser extension origin are accepted).
- This module draws the current tab as a 3×2 card grid and streams it (video) to the tablet:
  operator: rtsp://localhost:8554/pad (low latency), friends: <tunnel>/pad/pad.m3u8 (HLS).
- Tablet buttons arrive as VRChat-log commands {"op":"pad", "a": tab|page|card|stop|refresh, ...}.
- Picking a YouTube / X video plays it into the same stream (yt-dlp → ffmpeg). BOOTH items and X posts
  without video open a detail card instead. Nothing is written back to the sites.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger("rigbridge.pad")

SERVICES = ["youtube", "x", "booth"]
LABEL = {"youtube": "YouTube", "x": "X", "booth": "BOOTH"}
COLS, ROWS = 3, 2
PER_PAGE = COLS * ROWS
W, H = 960, 540                  # stream frame (16:9) — the tablet maps its touch grid onto this
MAX_ITEMS = 60
EXT_ORIGINS = ("chrome-extension://", "moz-extension://", "extension://")

# grid geometry shared with the tablet (normalized; the tablet's invisible buttons use the same numbers)
GRID_MARGIN_X, GRID_MARGIN_Y, GRID_GAP = 0.02, 0.03, 0.02


def cell_rect(i: int) -> tuple[int, int, int, int]:
    cw = (1 - 2 * GRID_MARGIN_X - (COLS - 1) * GRID_GAP) / COLS
    ch = (1 - 2 * GRID_MARGIN_Y - (ROWS - 1) * GRID_GAP) / ROWS
    c, r = i % COLS, i // COLS
    x0 = GRID_MARGIN_X + c * (cw + GRID_GAP)
    y0 = GRID_MARGIN_Y + r * (ch + GRID_GAP)
    return int(x0 * W), int(y0 * H), int((x0 + cw) * W), int((y0 + ch) * H)


def _font(names, size):
    for n in names:
        for d in (Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts",):
            p = d / n
            if p.exists():
                return ImageFont.truetype(str(p), size)
    return ImageFont.load_default()


# --------------------------------------------------------------------------- feed store
class Feed:
    def __init__(self):
        self.lock = threading.Lock()
        self.items: dict[str, list[dict]] = {s: [] for s in SERVICES}
        self.updated: dict[str, float] = {s: 0.0 for s in SERVICES}

    def add(self, service: str, items: list[dict]) -> int:
        if service not in self.items:
            return 0
        clean = []
        for it in items[:80]:
            iid = str(it.get("id", ""))[:80]
            url = str(it.get("url", ""))[:400]
            if not iid or not url.startswith("https://"):
                continue
            clean.append({"id": iid, "url": url, "title": str(it.get("title", ""))[:200],
                          "author": str(it.get("author", ""))[:80], "thumb": str(it.get("thumb", ""))[:400],
                          "price": str(it.get("price", ""))[:40], "video": bool(it.get("video", service == "youtube"))})
        with self.lock:
            old = {x["id"]: x for x in self.items[service]}
            merged = []
            seen = set()
            for x in clean + self.items[service]:          # newest batch first, keep order otherwise
                if x["id"] in seen:
                    continue
                seen.add(x["id"])
                merged.append({**old.get(x["id"], {}), **x})
            self.items[service] = merged[:MAX_ITEMS]
            self.updated[service] = time.time()
            return len(clean)

    def page(self, service: str, page: int) -> tuple[list[dict], int]:
        with self.lock:
            lst = list(self.items.get(service, []))
        pages = max(1, (len(lst) + PER_PAGE - 1) // PER_PAGE)
        page = max(0, min(page, pages - 1))
        return lst[page * PER_PAGE:(page + 1) * PER_PAGE], pages

    def counts(self) -> dict:
        with self.lock:
            return {s: len(v) for s, v in self.items.items()}


# --------------------------------------------------------------------------- thumbnails
class Thumbs:
    """Downloads card images in the background (memory cache, small and bounded)."""

    def __init__(self, on_ready):
        self.cache: dict[str, Image.Image] = {}
        self.pending: set[str] = set()
        self.lock = threading.Lock()
        self.on_ready = on_ready

    def get(self, url: str):
        if not url:
            return None
        with self.lock:
            if url in self.cache:
                return self.cache[url]
            if url in self.pending:
                return None
            self.pending.add(url)
        threading.Thread(target=self._load, args=(url,), daemon=True).start()
        return None

    def _load(self, url: str):
        img = None
        try:
            headers = {"User-Agent": "Mozilla/5.0"}
            if "pximg.net" in url:                       # BOOTH images need the site as referer
                headers["Referer"] = "https://booth.pm/"
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as r:
                data = r.read(4_000_000)
            img = Image.open(io.BytesIO(data)).convert("RGB")
            img.thumbnail((640, 640))
        except Exception as e:  # noqa: BLE001
            log.info("thumb failed %s: %s", url[:80], e)
        with self.lock:
            self.pending.discard(url)
            if img is not None:
                if len(self.cache) > 300:
                    self.cache.pop(next(iter(self.cache)))
                self.cache[url] = img
        if img is not None:
            self.on_ready()


# --------------------------------------------------------------------------- renderer
class PadRenderer:
    BG = (16, 18, 28)

    def __init__(self):
        self.f_title = _font(["malgunbd.ttf", "malgun.ttf", "arialbd.ttf"], 19)
        self.f_small = _font(["malgun.ttf", "arial.ttf"], 15)
        self.f_big = _font(["malgunbd.ttf", "malgun.ttf", "arialbd.ttf"], 30)
        self.f_mid = _font(["malgun.ttf", "arial.ttf"], 22)

    @staticmethod
    def _cover(img: Image.Image, w: int, h: int) -> Image.Image:
        s = max(w / img.width, h / img.height)
        im = img.resize((max(w, int(img.width * s)), max(h, int(img.height * s))), Image.LANCZOS)
        x, y = (im.width - w) // 2, (im.height - h) // 2
        return im.crop((x, y, x + w, y + h))

    def _wrap(self, d, text, font, width, lines):
        out, cur = [], ""
        for ch in text.replace("\n", " "):
            if d.textlength(cur + ch, font=font) > width:
                out.append(cur)
                cur = ch
                if len(out) == lines:
                    break
            else:
                cur += ch
        if len(out) < lines and cur:
            out.append(cur)
        if len(out) == lines and d.textlength("".join(out), font=font) < d.textlength(text, font=font):
            last = out[-1]
            while last and d.textlength(last + "…", font=font) > width:
                last = last[:-1]
            out[-1] = last + "…"
        return out

    def grid(self, service: str, items: list[dict], page: int, pages: int, thumbs: Thumbs, note: str = "") -> Image.Image:
        img = Image.new("RGB", (W, H), self.BG)
        d = ImageDraw.Draw(img)
        if not items:
            msg = {"youtube": "PC 브라우저에서 YouTube 홈을 열어 두면\n추천 영상이 여기에 나와요",
                   "x": "PC 브라우저에서 X(트위터) 홈을 열어 두면\n타임라인이 여기에 나와요",
                   "booth": "PC 브라우저에서 BOOTH 좋아요(위시리스트)나\n상품 목록을 열어 두면 여기에 나와요"}[service]
            d.multiline_text((W // 2, H // 2 - 20), msg, fill=(220, 225, 240), font=self.f_mid, anchor="mm", align="center", spacing=10)
            d.text((W // 2, H // 2 + 60), "RigBridge 브라우저 확장이 필요해요", fill=(140, 150, 175), font=self.f_small, anchor="mm")
            return img
        for i, it in enumerate(items):
            x0, y0, x1, y1 = cell_rect(i)
            d.rounded_rectangle((x0, y0, x1, y1), 12, fill=(30, 33, 48))
            th_h = int((x1 - x0) * 9 / 16) if service != "booth" else int((y1 - y0) * 0.62)
            th_h = min(th_h, int((y1 - y0) * 0.64))
            t = thumbs.get(it.get("thumb", ""))
            if t is not None:
                tile = self._cover(t, x1 - x0, th_h)
                mask = Image.new("L", tile.size, 0)
                ImageDraw.Draw(mask).rounded_rectangle((0, 0, tile.width, tile.height + 12), 12, fill=255)
                img.paste(tile, (x0, y0), mask)
            else:
                d.rounded_rectangle((x0, y0, x1, y0 + th_h), 12, fill=(44, 48, 68))
            if it.get("video") and service != "booth":
                cx, cy = x1 - 26, y0 + th_h - 24
                d.ellipse((cx - 16, cy - 16, cx + 16, cy + 16), fill=(0, 0, 0))
                d.polygon([(cx - 5, cy - 9), (cx - 5, cy + 9), (cx + 9, cy)], fill=(255, 255, 255))
            ty = y0 + th_h + 8
            for ln in self._wrap(d, it.get("title") or "(제목 없음)", self.f_title, x1 - x0 - 20, 2):
                d.text((x0 + 10, ty), ln, fill=(240, 242, 250), font=self.f_title)
                ty += 24
            sub = it.get("price") if service == "booth" and it.get("price") else it.get("author", "")
            if sub:
                d.text((x0 + 10, y1 - 12), sub[:40], fill=(150, 160, 190), font=self.f_small, anchor="ls")
        footer = f"{LABEL[service]}  ·  {page + 1} / {pages}" + (f"  ·  {note}" if note else "")
        d.text((W - 14, H - 6), footer, fill=(110, 118, 145), font=self.f_small, anchor="rs")
        return img

    def detail(self, service: str, it: dict, thumbs: Thumbs, msg: str = "") -> Image.Image:
        img = Image.new("RGB", (W, H), self.BG)
        d = ImageDraw.Draw(img)
        t = thumbs.get(it.get("thumb", ""))
        if t is not None:
            tile = t.copy()
            tile.thumbnail((520, 500))
            img.paste(tile, (30 + (520 - tile.width) // 2, (H - tile.height) // 2))
        y = 50
        for ln in self._wrap(d, it.get("title") or "", self.f_big, 360, 5):
            d.text((580, y), ln, fill=(245, 245, 252), font=self.f_big)
            y += 42
        y += 10
        for s in (it.get("author", ""), it.get("price", "")):
            if s:
                d.text((580, y), s, fill=(170, 180, 205), font=self.f_mid)
                y += 34
        if msg:
            d.text((580, H - 40), msg, fill=(255, 205, 120), font=self.f_small)
        return img

    def message(self, title: str, sub: str = "") -> Image.Image:
        img = Image.new("RGB", (W, H), self.BG)
        d = ImageDraw.Draw(img)
        d.text((W // 2, H // 2 - 16), title, fill=(235, 238, 250), font=self.f_big, anchor="mm")
        if sub:
            d.text((W // 2, H // 2 + 30), sub, fill=(150, 160, 190), font=self.f_small, anchor="mm")
        return img


# --------------------------------------------------------------------------- stream + logic
class Pad(threading.Thread):
    """Owns the 'pad' stream. UI mode = rendered frames (+silence); play mode = a video via yt-dlp."""
    FPS_UI = 5

    def __init__(self, cfg: dict, find_exe, bind_child, out_dir: Path, rtsp_url: str = ""):
        super().__init__(daemon=True, name="pad")
        self.cfg, self.find_exe, self.bind_child = cfg, find_exe, bind_child
        self.out_dir, self.rtsp_url = out_dir, rtsp_url
        self.feed = Feed()
        self.render = PadRenderer()
        self.thumbs = Thumbs(self._dirty)
        self.service = "youtube"
        self.page_no = {s: 0 for s in SERVICES}
        self.view = "grid"                 # grid | detail | loading | playing
        self.detail_item: dict | None = None
        self.play_item: dict | None = None
        self.note = ""
        self._dirty_ev = threading.Event()
        self._switch = threading.Event()   # leave the current ffmpeg (mode change)
        self.proc: subprocess.Popen | None = None
        self.lock = threading.Lock()
        self.last_by = ""

    # ---- external API
    def add_feed(self, service: str, items: list[dict]) -> int:
        n = self.feed.add(service, items)
        if n and service == self.service and self.view == "grid":
            self._dirty()
        return n

    def command(self, cmd: dict):
        a = cmd.get("a")
        by = str(cmd.get("by", ""))
        with self.lock:
            self.last_by = by
            if a == "tab" and cmd.get("s") in SERVICES:
                self.service = cmd["s"]
                self._to_grid()
            elif a == "page":
                d = 1 if int(cmd.get("d", 1)) > 0 else -1
                _, pages = self.feed.page(self.service, 0)
                self.page_no[self.service] = (self.page_no[self.service] + d) % pages
                self._to_grid()
            elif a == "card":
                items, _ = self.feed.page(self.service, self.page_no[self.service])
                i = int(cmd.get("i", -1))
                if 0 <= i < len(items):
                    it = items[i]
                    if it.get("video") and self.service in ("youtube", "x"):
                        self.play_item, self.view = it, "loading"
                        self._switch.set()
                    else:
                        self.detail_item, self.view = it, "detail"
                        self._dirty()
            elif a in ("stop", "back"):
                self._to_grid()
            elif a == "refresh":
                self.note = "새로고침"
                self._to_grid()
        log.info("pad %s (%s)", cmd, by)

    def status(self) -> dict:
        return {"service": self.service, "view": self.view, "counts": self.feed.counts(),
                "page": self.page_no[self.service], "playing": (self.play_item or {}).get("title", "") if self.view == "playing" else ""}

    # ---- internals
    def _to_grid(self):
        was_playing = self.view in ("playing", "loading")
        self.view, self.detail_item = "grid", None
        if was_playing:
            self._switch.set()
        self._dirty()

    def _dirty(self):
        self._dirty_ev.set()

    def _frame(self) -> bytes:
        with self.lock:
            view, svc = self.view, self.service
            item = self.detail_item if view == "detail" else self.play_item
            page = self.page_no[svc]
        if view == "loading":
            img = self.render.message("불러오는 중…", (item or {}).get("title", "")[:50])
        elif view == "detail" and item:
            img = self.render.detail(svc, item, self.thumbs, "이 항목은 탭하면 정보만 보여요 · 목록으로 돌아가려면 ‹ 목록")
        else:
            items, pages = self.feed.page(svc, page)
            img = self.render.grid(svc, items, min(page, pages - 1), pages, self.thumbs, self.note)
        return img.tobytes()

    def _outputs(self, gop: int) -> list[str]:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        for f in self.out_dir.glob("*"):
            try:
                f.unlink()
            except OSError:
                pass
        hls = (f"hls_time=1:hls_list_size=4:hls_flags=delete_segments+independent_segments+omit_endlist"
               f":hls_segment_filename=pad_%05d.ts")
        enc = ["-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency", "-pix_fmt", "yuv420p",
               "-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0",
               "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2"]
        if self.rtsp_url:
            return enc + ["-flags", "+global_header", "-f", "tee", "-use_fifo", "1",
                          f"[f=hls:{hls}]pad.m3u8|[f=rtsp:rtsp_transport=tcp:onfail=ignore]{self.rtsp_url}"]
        return enc + ["-f", "hls", "-hls_time", "1", "-hls_list_size", "4",
                      "-hls_flags", "delete_segments+independent_segments+omit_endlist",
                      "-hls_segment_filename", "pad_%05d.ts", "pad.m3u8"]

    def _spawn(self, args: list[str], stdin=False) -> subprocess.Popen:
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        p = subprocess.Popen(args, stdin=subprocess.PIPE if stdin else subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, cwd=str(self.out_dir), creationflags=flags)
        self.bind_child(p)
        threading.Thread(target=self._pump, args=(p,), daemon=True).start()
        return p

    @staticmethod
    def _pump(p):
        for line in p.stderr:
            s = line.decode(errors="replace").strip()
            if s:
                log.info("pad ffmpeg: %s", s[:200])

    def run(self):
        ffmpeg = self.find_exe("ffmpeg")
        if not ffmpeg:
            log.error("pad: ffmpeg not found")
            return
        while True:
            try:
                if self.view in ("loading", "playing") and self.play_item:
                    self._run_play(ffmpeg)
                else:
                    self._run_ui(ffmpeg)
            except Exception as e:  # noqa: BLE001
                log.warning("pad stream: %s", e)
                time.sleep(2)
            finally:
                if self.proc and self.proc.poll() is None:
                    self.proc.kill()

    def _run_ui(self, ffmpeg: str):
        self._switch.clear()
        args = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-framerate", str(self.FPS_UI), "-i", "-",
                "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                "-map", "0:v", "-map", "1:a", "-b:v", "500k"] + self._outputs(self.FPS_UI)
        self.proc = self._spawn(args, stdin=True)
        log.info("pad: UI stream")
        frame = self._frame()
        t0, n = time.perf_counter(), 0
        while not self._switch.is_set() and self.proc.poll() is None:
            if self._dirty_ev.is_set():
                self._dirty_ev.clear()
                frame = self._frame()
            self.proc.stdin.write(frame)
            n += 1
            wait = t0 + n / self.FPS_UI - time.perf_counter()
            if wait > 0:
                self._switch.wait(wait)
            if n % (self.FPS_UI * 3) == 0:
                frame = self._frame()          # pick up thumbnails that arrived
        try:
            self.proc.stdin.close()
        except OSError:
            pass

    def _resolve(self, url: str) -> list[str]:
        cmd = [sys.executable, "-m", "yt_dlp", "-g", "--no-playlist", "--no-warnings",
               "-f", "bv*[height<=720][vcodec^=avc1]+ba/b[height<=720]/bv*+ba/b", url]
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=45, creationflags=flags)
        urls = [ln.strip() for ln in r.stdout.splitlines() if ln.strip().startswith("http")]
        if not urls:
            raise RuntimeError((r.stderr or "no stream url").strip().splitlines()[-1][:200])
        return urls[:2]

    def _run_play(self, ffmpeg: str):
        self._switch.clear()
        item = self.play_item
        # show "loading" frames while yt-dlp works (keeps the tablet player alive)
        loading = threading.Event()
        self.view = "loading"
        self._dirty()
        holder = {}

        def resolve():
            try:
                holder["urls"] = self._resolve(item["url"])
            except Exception as e:  # noqa: BLE001
                holder["err"] = str(e)
            loading.set()
        threading.Thread(target=resolve, daemon=True).start()
        args = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-framerate", str(self.FPS_UI), "-i", "-",
                "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-map", "0:v", "-map", "1:a", "-b:v", "400k"] + self._outputs(self.FPS_UI)
        self.proc = self._spawn(args, stdin=True)
        frame = self._frame()
        while not loading.is_set() and not self._switch.is_set() and self.proc.poll() is None:
            self.proc.stdin.write(frame)
            self._switch.wait(1 / self.FPS_UI)
        # the silent audio input never ends, so stop the "loading" encoder explicitly
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        self.proc.kill()
        self.proc.wait(timeout=5)
        if self._switch.is_set() or self.play_item is not item:
            return
        if "err" in holder:
            log.warning("pad: cannot play %s: %s", item.get("url"), holder["err"])
            with self.lock:
                self.view, self.detail_item = "detail", item
            self.note = ""
            self._dirty()
            return
        urls = holder["urls"]
        inputs = []
        for u in urls:
            inputs += ["-re", "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "4",
                       "-user_agent", "Mozilla/5.0", "-i", u]
        amap = ["-map", "0:v:0", "-map", ("1:a:0" if len(urls) > 1 else "0:a:0?")]
        fps = 30
        args = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"] + inputs + amap + \
               ["-vf", f"scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,fps={fps}",
                "-b:v", "1800k", "-maxrate", "2200k", "-bufsize", "3000k"] + self._outputs(fps)
        with self.lock:
            self.view = "playing"
        log.info("pad: playing %s", item.get("url"))
        self.proc = self._spawn(args)
        while not self._switch.is_set() and self.proc.poll() is None:
            self._switch.wait(0.5)
        if self.proc.poll() is not None and not self._switch.is_set():
            # finished on its own → back to the list
            with self.lock:
                if self.play_item is item:
                    self.view = "grid"
            self._dirty()
