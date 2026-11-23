"""YouTube captions for the room's video player (VizVid).

World -> RigBridge : the operator's client logs  [RIGBRIDGE] {"op":"cap","seq":N,"url":"<now playing>"}
RigBridge          : resolves the YouTube video id (direct YouTube URL, or an api.u2b.cx/vrcurl slot -> 302),
                     picks a caption track with yt-dlp and converts YouTube's json3 captions (timing + position)
World <- RigBridge : http://localhost:8810/cap.json  (local host only)
                     {"seq":N, "status":"loading|ok|none|error", "lang":"ko", "pos":[[ap,ah,av],...],
                      "styles":[[font, outline_rgb, shadow_rgb, bg_rgba, justify, size_pct], ...],
                      "cues":[[start_ms, end_ms, pos_index, "rich text", style_index], ...]}
                     outline/shadow: -2 = default look, -1 = none.  bg: -1 = none.  justify: -1 = from position.
                     rich text: Unity <color=#RRGGBBAA>, <b>, <i>, and <size=N%> (the world turns % into pixels).

Only video ids are ever handed to yt-dlp (never the raw URL from the world).
"""
from __future__ import annotations

import http.client
import json
import logging
import re
import subprocess
import sys
import threading
import urllib.request

log = logging.getLogger("rigbridge.cap")

ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
YT_RES = [
    re.compile(r"^https?://(?:www\.|m\.|music\.)?youtube\.com/watch\?(?:.*&)?v=([A-Za-z0-9_-]{11})"),
    re.compile(r"^https?://(?:www\.|m\.)?youtube\.com/(?:shorts|live|embed)/([A-Za-z0-9_-]{11})"),
    re.compile(r"^https?://youtu\.be/([A-Za-z0-9_-]{11})"),
]
U2B_RE = re.compile(r"^https://api\.u2b\.cx/vrcurl/[A-Za-z0-9_-]{1,64}/\d{1,6}$")
MAX_CUES = 4000


def _no_window() -> dict:
    if sys.platform == "win32":
        return {"creationflags": 0x08000000}       # CREATE_NO_WINDOW
    return {}


def video_id(url: str) -> str:
    url = (url or "").strip()
    for rx in YT_RES:
        m = rx.match(url)
        if m:
            return m.group(1)
    if U2B_RE.match(url):
        path = url[len("https://api.u2b.cx"):]
        c = http.client.HTTPSConnection("api.u2b.cx", timeout=15)
        try:
            c.request("GET", path, headers={"User-Agent": "RigBridge"})
            r = c.getresponse()
            loc = r.getheader("Location") or ""
        finally:
            c.close()
        for rx in YT_RES:
            m = rx.match(loc)
            if m:
                return m.group(1)
    return ""


def _pick_track(info: dict):
    """Korean first: manual ko > Korean speech recognition > auto-translated ko > other manual > original ASR."""
    subs = info.get("subtitles") or {}
    auto = info.get("automatic_captions") or {}
    lang = (info.get("language") or "").split("-")[0]

    def j3(tracks):
        for t in tracks or []:
            if t.get("ext") == "json3" and t.get("url"):
                return t["url"]
        return None

    order = []
    order += [("sub", k) for k in subs if k.lower().startswith("ko") and k != "live_chat"]
    order += [("auto", "ko-orig")] if lang == "ko" else []
    order += [("auto", "ko")]
    for k in ([lang] if lang else []) + ["en"] + list(subs):
        if k in subs and k != "live_chat":
            order.append(("sub", k))
    for k in ([lang + "-orig", lang] if lang else []) + ["en-orig", "en"]:
        order.append(("auto", k))
    for kind, k in order:
        u = j3((subs if kind == "sub" else auto).get(k))
        if u:
            return k, u
    return None, None


ZW = "​‌‍﻿"
LEAD_RE = re.compile(r"^((?:<[^>]+>)*)\s+")
TRAIL_RE = re.compile(r"\s+((?:</[^>]+>)*)$")
DEFAULT_STYLE = [0, -2, -2, -1, -1, 100]        # font, outline rgb, shadow rgb, bg rgba, justify, size %


def _size_pct(sz) -> int:
    """YouTube draws pen size sz% as 100 + (sz-100)/4 % of the normal caption size."""
    try:
        sz = int(sz)
    except (TypeError, ValueError):
        return 100
    return max(40, min(400, 100 + (sz - 100) // 4))


def _rgba(rgb: int, alpha254: int) -> str:
    a = max(0, min(255, round(int(alpha254) * 255 / 254)))
    return f"{int(rgb) & 0xFFFFFF:06X}{a:02X}"


def _clean(s: str) -> str:
    for z in ZW:
        s = s.replace(z, "")
    return s.replace("<", "＜")          # never let caption text open a rich-text tag


def parse_json3(data: dict):
    """-> (positions [[ap,ah,av]], styles [[font,outline,shadow,bg,ju,size]], cues [[start,end,pos,rich_text,style]])

    Handles styled tracks (made with tools like YTSubConverter): every line is usually drawn twice at the same
    time and place (an outline layer and a shadow layer) -> merged into one line with both edges; many lines
    can be on screen at once in different places; pens carry colour, opacity, bold/italic, size, font, edges
    and background.
    """
    wps = data.get("wpWinPositions") or [{}]
    pos = [[int(w.get("apPoint", 7)), int(w.get("ahHorPos", 50)), int(w.get("avVerPos", 100))] for w in wps]
    pens = data.get("pens") or [{}]
    wss = data.get("wsWinStyles") or [{}]
    styled = len(pens) > 1
    styles, style_ix = [list(DEFAULT_STYLE)], {tuple(DEFAULT_STYLE): 0}

    def pen(i):
        return pens[i] if isinstance(i, int) and 0 <= i < len(pens) else {}

    merged = {}          # (start, end, pos, plain) -> cue
    cues = []
    asr = False
    for e in data.get("events") or []:
        segs = e.get("segs")
        if not segs or e.get("aAppend"):
            continue
        if "wWinId" in e:
            asr = True
        start = int(e.get("tStartMs", 0))
        end = start + int(e.get("dDurationMs", 0))
        p = int(e.get("wpWinPosId", 0))
        p = p if 0 <= p < len(pos) else 0
        ju = int(wss[e.get("wsWinStyleId", 0)].get("juJustifCode", -1)) if 0 <= int(e.get("wsWinStyleId", 0)) < len(wss) else -1

        plain, rich, first = "", "", None
        for s in segs:
            txt = _clean(s.get("utf8", ""))
            if not txt:
                continue
            pn = pen(s.get("pPenId", e.get("pPenId", 0)))
            if styled and int(pn.get("foForeAlpha", 254)) == 0 and not txt.strip():
                if "\n" in txt:                                 # invisible spacer, but keep its line break
                    plain += "\n"
                    rich += "\n"
                continue
            if int(pn.get("fsFontStyle", 0)) == 7:
                txt = txt.upper()                               # small caps
            plain += txt
            if not txt.strip():
                rich += txt
                continue
            if first is None:
                first = pn
            body = txt
            if styled:
                if pn.get("iAttr"):
                    body = f"<i>{body}</i>"
                if pn.get("bAttr"):
                    body = f"<b>{body}</b>"
                sp = _size_pct(pn.get("szPenSize", 100))
                if first is not None and sp != _size_pct(first.get("szPenSize", 100)):
                    body = f"<size={sp}%>{body}</size>"
                body = f"<color=#{_rgba(pn.get('fcForeColor', 0xFFFFFF), pn.get('foForeAlpha', 254))}>{body}</color>"
            rich += body
        plain = "\n".join(l.strip() for l in plain.split("\n")).strip()
        # padding spaces at the start/end of each line (also when they sit inside the first/last tag)
        rich = "\n".join(TRAIL_RE.sub(r"\1", LEAD_RE.sub(r"\1", l)) for l in rich.split("\n")).strip("\n")
        if not plain:
            continue

        # line style (taken from the first visible piece of text)
        if styled and first is not None:
            et = int(first.get("etEdgeType", 0)) if "etEdgeType" in first else 0
            ec = int(first.get("ecEdgeColor", 0)) & 0xFFFFFF
            outline = ec if et == 3 else -1
            shadow = ec if et in (1, 2, 4) else -1
            bo = int(first.get("boBackAlpha", 0)) if "boBackAlpha" in first else 0
            bg = int(_rgba(first.get("bcBackColor", 0x080808), bo), 16) if bo > 0 else -1
            st = [int(first.get("fsFontStyle", 0)) % 8, outline, shadow, bg, ju, _size_pct(first.get("szPenSize", 100))]
        else:
            st = list(DEFAULT_STYLE)
            st[4] = ju

        key = (start, end, p, plain)
        if key in merged:                                       # same line drawn again as another layer
            c = merged[key]
            old = styles[c[4]]
            st = [old[0], old[1] if old[1] >= 0 else st[1], old[2] if old[2] >= 0 else st[2],
                  old[3] if old[3] >= 0 else st[3], old[4], old[5]]
            c[4] = style_ix.setdefault(tuple(st), len(styles))
            if c[4] == len(styles):
                styles.append(st)
            continue
        si = style_ix.setdefault(tuple(st), len(styles))
        if si == len(styles):
            styles.append(st)
        c = [start, end, p, rich, si]
        merged[key] = c
        cues.append(c)
        if len(cues) >= MAX_CUES:
            break

    cues.sort(key=lambda c: (c[0], c[2]))
    if asr:
        # speech recognition: lines roll in one window; cut each line when the line after next starts (2 lines)
        by_pos = {}
        for c in cues:
            by_pos.setdefault(c[2], []).append(c)
        for lst in by_pos.values():
            for i in range(len(lst) - 2):
                if lst[i][1] > lst[i + 2][0]:
                    lst[i][1] = lst[i + 2][0]
    return pos, styles, cues


class Captions:
    def __init__(self):
        self.lock = threading.Lock()
        self.data = {"seq": 0, "status": "none", "lang": "", "pos": [[7, 50, 100]], "cues": []}
        self.latest = 0

    def status(self) -> dict:
        with self.lock:
            return self.data

    def command(self, cmd: dict):
        try:
            seq = int(cmd.get("seq", 0))
        except (TypeError, ValueError):
            return
        url = str(cmd.get("url", ""))[:400]
        with self.lock:
            self.latest = seq
            self.data = {"seq": seq, "status": "loading", "lang": "", "pos": [[7, 50, 100]], "cues": []}
        threading.Thread(target=self._work, args=(seq, url), daemon=True, name="captions").start()

    def _finish(self, seq: int, **kw):
        with self.lock:
            if seq != self.latest:          # a newer video started meanwhile
                return
            d = {"seq": seq, "status": "none", "lang": "", "pos": [[7, 50, 100]], "cues": []}
            d.update(kw)
            self.data = d

    def _work(self, seq: int, url: str):
        try:
            vid = video_id(url)
            if not ID_RE.match(vid or ""):
                self._finish(seq, status="none")
                return
            r = subprocess.run([sys.executable, "-m", "yt_dlp", "-J", "--skip-download", "--no-playlist",
                                "--no-warnings", f"https://www.youtube.com/watch?v={vid}"],
                               capture_output=True, text=True, encoding="utf-8", timeout=90, **_no_window())
            info = json.loads(r.stdout or "{}")
            lang, u = _pick_track(info)
            if not u:
                log.info("captions: none for %s", vid)
                self._finish(seq, status="none")
                return
            req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            pos, styles, cues = parse_json3(data)
            log.info("captions: %s %s, %d lines, %d styles", vid, lang, len(cues), len(styles))
            self._finish(seq, status="ok" if cues else "none", lang=lang, pos=pos, styles=styles, cues=cues)
        except Exception as e:  # noqa: BLE001
            log.warning("captions failed: %s", e)
            self._finish(seq, status="error")
