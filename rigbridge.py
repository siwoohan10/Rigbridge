"""
RigBridge - VRChat "My home" world  <->  wfview (IC-7300) bridge.

World -> rig : the world writes `[RIGBRIDGE] {json}` lines to the operator's VRChat log
               (Udon Debug.Log). This app tails the log and sends rigctld commands to wfview.
Rig -> world : this app renders the radio LCD (freq / mode / S-meter / TX) and mixes it with
               the received audio (wfview TCI audio stream, or an input device) into an HLS
               stream that the world's AVPro player shows on the radio's LCD.
Friends      : optional Cloudflare quick tunnel exposes the HLS stream to the internet.

Safety: commands are accepted only while the operator is in the configured world, PTT is
limited to the operator and the `ptt_allowed` names, and PTT is released automatically
(timeout, world change, bridge exit, rig disconnect).
"""
from __future__ import annotations

import asyncio
import collections
import glob
import http.server
import json
import logging
import os
import re
import shutil
import socket
import socketserver
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

BASE = Path(__file__).resolve().parent
STREAM_DIR = BASE / "stream"
CONFIG_PATH = BASE / "config.json"
MARKER = "[RIGBRIDGE]"
SR = 48000          # stream audio sample rate
CH = 2              # stream audio channels

log = logging.getLogger("rigbridge")


# --------------------------------------------------------------------------- child process cleanup
_JOB = None


def _bind_child(proc: subprocess.Popen):
    """Put a child (ffmpeg, cloudflared) in a Windows job that is killed when RigBridge exits, even if killed."""
    global _JOB
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.OpenProcess.restype = wintypes.HANDLE
        if _JOB is None:
            class BASIC(ctypes.Structure):
                _fields_ = [("a", ctypes.c_int64), ("b", ctypes.c_int64), ("LimitFlags", wintypes.DWORD),
                            ("c", ctypes.c_size_t), ("d", ctypes.c_size_t), ("e", wintypes.DWORD),
                            ("f", ctypes.c_size_t), ("g", wintypes.DWORD), ("h", wintypes.DWORD)]

            class EXT(ctypes.Structure):
                _fields_ = [("Basic", BASIC), ("Io", ctypes.c_uint64 * 6), ("p", ctypes.c_size_t),
                            ("j", ctypes.c_size_t), ("pp", ctypes.c_size_t), ("pj", ctypes.c_size_t)]
            job = k32.CreateJobObjectW(None, None)
            info = EXT()
            info.Basic.LimitFlags = 0x2000          # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
                raise OSError(ctypes.get_last_error())
            _JOB = job
        h = k32.OpenProcess(0x0001 | 0x0100, False, proc.pid)   # PROCESS_TERMINATE | PROCESS_SET_QUOTA
        if not k32.AssignProcessToJobObject(_JOB, h):
            raise OSError(ctypes.get_last_error())
        k32.CloseHandle(h)
    except Exception as e:  # noqa: BLE001
        log.warning("could not bind child process to job: %s", e)


# --------------------------------------------------------------------------- config / state
def load_config() -> dict:
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    if not cfg.get("vrchat_log_dir"):
        cfg["vrchat_log_dir"] = str(Path.home() / "AppData" / "LocalLow" / "VRChat" / "VRChat")
    return cfg


@dataclass
class RigState:
    connected: bool = False
    freq: int = 0
    mode: str = "---"
    smeter_db: float = -54.0      # dB relative to S9 (rigctld STRENGTH)
    ptt: bool = False
    ptt_by: str = ""
    ptt_since: float = 0.0
    step_index: int = 1
    in_world: bool = False
    world_id: str = ""
    audio_ok: bool = False
    public_url: str = ""
    last_cmd: str = ""
    link_ok: bool = True          # network path to the remote rig (wfview server on the Pi)
    link_host: str = ""
    unkey_pending: bool = False   # a PTT release that must be re-sent until confirmed
    tuner_on: bool = False        # internal antenna tuner (ATU) enabled
    tune_until: float = 0.0       # time.time() until which an ATU tune is considered running
    tune_seen_tx: bool = False    # the rig went to TX after a tune request (= tuning really started)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


# --------------------------------------------------------------------------- rigctld client
class RigCtl(threading.Thread):
    """Talks Hamlib NET rigctl protocol to wfview's rigctld server."""

    def __init__(self, cfg: dict, state: RigState):
        super().__init__(daemon=True, name="rigctl")
        self.cfg, self.state = cfg, state
        self.cmds: "collections.deque[tuple]" = collections.deque()
        self.sock: socket.socket | None = None
        self.buf = b""
        self.stop = threading.Event()

    # ---- low level
    def _connect(self):
        s = socket.create_connection((self.cfg["rigctld_host"], self.cfg["rigctld_port"]), timeout=2.0)
        s.settimeout(2.0)
        self.sock, self.buf = s, b""

    def _readline(self) -> str:
        while b"\n" not in self.buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("rigctld closed")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return line.decode(errors="replace").strip()

    def _query(self, cmd: str, lines: int = 1) -> list[str]:
        self.sock.sendall((cmd + "\n").encode())
        return [self._readline() for _ in range(lines)]

    def _set(self, cmd: str) -> bool:
        resp = self._query(cmd)[0]
        ok = resp.startswith("RPRT 0")
        if not ok:
            log.warning("rigctld %r -> %s", cmd, resp)
        return ok

    # ---- public (thread-safe) API
    def submit(self, kind: str, value=None):
        self.cmds.append((kind, value))

    # ---- loop
    def run(self):
        while not self.stop.is_set():
            try:
                self._connect()
                log.info("rigctld connected %s:%s", self.cfg["rigctld_host"], self.cfg["rigctld_port"])
                with self.state.lock:
                    self.state.connected = True
                self._loop()
            except Exception as e:  # noqa: BLE001
                with self.state.lock:
                    was = self.state.connected
                    if self.state.ptt or self.state.ptt_since:
                        self.state.unkey_pending = True   # re-send T 0 once wfview is back
                        self.state.ptt_since = 0.0
                    self.state.connected = False
                    self.state.ptt = False
                if was:
                    log.warning("rigctld disconnected: %s", e)
                try:
                    if self.sock:
                        self.sock.close()
                except OSError:
                    pass
                self.sock = None
                self.stop.wait(3.0)

    UNKEY_CONFIRM = 3     # successful "T 0" sends (1 s apart, link up) before a pending release is cleared

    def _retry_unkey(self):
        """Keep sending T 0 while a release is pending; only counts while the Pi link is up."""
        with self.state.lock:
            pending, link = self.state.unkey_pending, self.state.link_ok
        if not pending:
            self._unkey_ok = 0
            return
        if not link:
            self._unkey_ok = 0
            return
        now = time.monotonic()
        if now - getattr(self, "_unkey_at", 0.0) < 1.0:
            return
        self._unkey_at = now
        if self._set("T 0"):
            self._unkey_ok = getattr(self, "_unkey_ok", 0) + 1
            if self._unkey_ok >= self.UNKEY_CONFIRM:
                with self.state.lock:
                    self.state.unkey_pending = False
                self._unkey_ok = 0
                log.info("PTT release confirmed")

    def _loop(self):
        interval = float(self.cfg["poll_interval_sec"])
        while not self.stop.is_set():
            while self.cmds:
                kind, value = self.cmds.popleft()
                self._apply(kind, value)
            self._retry_unkey()
            freq = int(float(self._query("f")[0]))
            mode, _pb = self._query("m", 2)
            try:
                db = float(self._query("l STRENGTH")[0])
            except ValueError:
                db = -54.0
            ptt = self._query("t")[0] == "1"
            tuner = self._query("u TUNER")[0] == "1"
            with self.state.lock:
                self.state.freq, self.state.mode, self.state.smeter_db = freq, mode, db
                if not ptt and self.state.ptt:
                    self.state.ptt_by = ""
                self.state.ptt = ptt
                self.state.tuner_on = tuner
                if ptt and time.time() < self.state.tune_until:
                    self.state.tune_seen_tx = True
            time.sleep(interval)

    def _apply(self, kind: str, value):
        if kind == "freq":
            self._set(f"F {int(value)}")
        elif kind == "mode":
            self._set(f"M {value} 0")
        elif kind == "ptt":
            self._set(f"T {1 if value else 0}")
        elif kind == "tuner":
            self._set(f"U TUNER {1 if value else 0}")

    def release_ptt_now(self):
        """Best effort, used on shutdown."""
        try:
            if self.sock:
                self._set("T 0")
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- command logic
class Commander:
    def __init__(self, cfg: dict, state: RigState, rig: RigCtl):
        self.cfg, self.state, self.rig = cfg, state, rig
        self.allowed_ptt = {n.strip().lower() for n in cfg.get("ptt_allowed", []) if n.strip()}
        if cfg.get("operator_name"):
            self.allowed_ptt.add(cfg["operator_name"].strip().lower())

    def handle(self, cmd: dict):
        op = cmd.get("op")
        by = str(cmd.get("by", ""))
        st = self.state
        with st.lock:
            freq, mode, step_i = st.freq, st.mode, st.step_index
            connected, in_world = st.connected, st.in_world
        if not in_world:
            log.warning("ignored %s: not in the configured world", cmd)
            return
        if op == "pad":                                       # room tablet (no radio involved)
            if getattr(self, "pad", None) is not None:
                self.pad.command(cmd)
            return
        if op == "cap":                                       # video captions (no radio involved)
            if getattr(self, "captions", None) is not None:
                self.captions.command(cmd)
            return
        if not connected:
            log.warning("ignored %s: rig not connected", cmd)
            return
        with st.lock:
            link_ok, pending = st.link_ok, st.unkey_pending
        if not link_ok:
            # do not queue anything for a rig we cannot reach (it would all fire at once on reconnect)
            log.warning("ignored %s: link to the rig is down", cmd)
            return
        transmits = (op == "ptt" and cmd.get("on")) or op == "atu_tune"
        if transmits and pending:
            log.warning("%s refused: a previous PTT release is still being confirmed", op)
            return
        if transmits and by.strip().lower() not in self.allowed_ptt:
            log.warning("%s refused for %r (not in ptt_allowed)", op, by)
            return
        steps = self.cfg["tune_steps_hz"]
        if op == "tune":
            d = 1 if int(cmd.get("dir", 1)) > 0 else -1
            mult = max(1, min(100, int(cmd.get("n", 1))))
            self.rig.submit("freq", max(30000, freq + d * mult * steps[step_i % len(steps)]))
        elif op == "step":
            with st.lock:
                st.step_index = (st.step_index + 1) % len(steps)
        elif op == "band":
            bands = sorted(self.cfg["bands_hz"])
            d = 1 if int(cmd.get("dir", 1)) > 0 else -1
            if d > 0:
                nxt = next((b for b in bands if b > freq + 1000), bands[0])
            else:
                lower = [b for b in bands if b < freq - 1000]
                nxt = lower[-1] if lower else bands[-1]
            self.rig.submit("freq", nxt)
        elif op == "mode":
            modes = self.cfg["modes"]
            i = modes.index(mode) if mode in modes else -1
            self.rig.submit("mode", modes[(i + (1 if int(cmd.get("dir", 1)) > 0 else -1)) % len(modes)])
        elif op == "atu":
            with st.lock:
                on = not st.tuner_on
            self.rig.submit("tuner", on)
        elif op == "atu_tune":
            with st.lock:
                busy = st.ptt or time.time() < st.tune_until
            if busy:
                log.warning("ATU tune refused: already transmitting / tuning")
                return
            with st.lock:
                st.tune_until, st.tune_seen_tx = time.time() + 15.0, False
            # wfview's rigctld has no tune op ('G TUNE' would select VFO A); TCI 'tune:2' = CI-V 1C 01 02
            threading.Thread(target=self._start_tune, daemon=True, name="atu").start()
        elif op == "ptt":
            on = bool(cmd.get("on"))
            self.rig.submit("ptt", on)
            with st.lock:
                st.ptt_by = by if on else ""
                st.ptt_since = time.time() if on else 0.0
        else:
            log.warning("unknown command %s", cmd)
            return
        with st.lock:
            st.last_cmd = f"{op} ({by})" if by else op
        log.info("cmd %s", cmd)

    def _start_tune(self):
        try:
            tci_command(self.cfg, "tune:2;")
            log.info("ATU tune requested via TCI")
        except Exception as e:  # noqa: BLE001
            log.warning("ATU tune: TCI failed: %s", e)
            with self.state.lock:
                self.state.tune_until = 0.0
            return
        time.sleep(4.0)
        with self.state.lock:
            seen = self.state.tune_seen_tx
        if not seen:
            log.warning("ATU tune: the rig did not go to TX within 4 s - tuning probably did not start")

    def watchdog(self):
        """PTT timeout."""
        with self.state.lock:
            on, since = self.state.ptt, self.state.ptt_since
        if on and since and time.time() - since > float(self.cfg["ptt_timeout_sec"]):
            self.force_unkey("timeout")

    def force_unkey(self, reason: str):
        with self.state.lock:
            on = self.state.ptt or self.state.ptt_since
            self.state.ptt_since = 0.0
            if on:
                self.state.unkey_pending = True   # RigCtl keeps re-sending T 0 until confirmed
        if on:
            log.warning("PTT released: %s", reason)
            self.rig.submit("ptt", False)


def tci_command(cfg: dict, command: str, timeout: float = 3.0):
    """Send one command to wfview's TCI server (waits for 'ready;' first)."""
    from websockets.sync.client import connect
    with connect(cfg["tci_url"], open_timeout=timeout, max_size=None) as ws:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            msg = ws.recv(timeout=max(0.05, end - time.monotonic()))
            if isinstance(msg, str) and msg.strip().lower().startswith("ready"):
                break
        ws.send(command)
        time.sleep(0.3)   # let wfview queue it before the socket closes


# --------------------------------------------------------------------------- link to the remote rig (Pi)
def wfview_lan_host() -> str:
    """The wfview server address from wfview's own settings (LAN connection), or '' for a USB rig."""
    if os.name != "nt":
        return ""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\wfview\wfview\LAN") as k:
            enabled = str(winreg.QueryValueEx(k, "EnableLAN")[0]).lower() == "true"
            ip = str(winreg.QueryValueEx(k, "IPAddress")[0]).strip()
            return ip if enabled else ""
    except OSError:
        return ""


def icmp_ping(host: str, timeout_ms: int = 800) -> bool:
    """One ICMP echo via iphlpapi (Windows `ping` exits 0 even for 'unreachable')."""
    import ctypes
    from ctypes import wintypes
    try:
        addr = socket.inet_aton(socket.gethostbyname(host))
    except OSError:
        return False
    ip = ctypes.WinDLL("iphlpapi")
    ip.IcmpCreateFile.restype = wintypes.HANDLE
    ip.IcmpSendEcho.argtypes = [wintypes.HANDLE, ctypes.c_uint32, ctypes.c_void_p, wintypes.WORD,
                                ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD]
    ip.IcmpCloseHandle.argtypes = [wintypes.HANDLE]
    h = ip.IcmpCreateFile()
    try:
        data = b"rigbridge"
        reply = ctypes.create_string_buffer(64 + len(data) + 8)
        n = ip.IcmpSendEcho(h, struct.unpack("<I", addr)[0], data, len(data), None, reply, len(reply), timeout_ms)
        return n > 0 and struct.unpack_from("<I", reply.raw, 4)[0] == 0   # ICMP_ECHO_REPLY.Status == IP_SUCCESS
    finally:
        ip.IcmpCloseHandle(h)


class LinkMonitor(threading.Thread):
    """Pings the wfview server (the Pi). wfview's rigctld keeps answering from its cache when that link
    drops, so this is how we notice: commands are ignored and a running PTT is released (and re-sent)."""
    DOWN_AFTER, UP_AFTER = 3, 2     # consecutive failures / successes (1 s apart)

    def __init__(self, cfg: dict, state: RigState, commander: Commander):
        super().__init__(daemon=True, name="link")
        self.cfg, self.state, self.cmd = cfg, state, commander
        self.host = (cfg.get("rig_link_host") or "").strip() or wfview_lan_host()
        self.stop = threading.Event()

    def run(self):
        with self.state.lock:
            self.state.link_host = self.host
        if not self.host:
            log.info("link monitor: no remote rig host (USB rig) - disabled")
            return
        log.info("link monitor: pinging %s", self.host)
        fails = oks = 0
        while not self.stop.is_set():
            ok = icmp_ping(self.host)
            fails, oks = (0, oks + 1) if ok else (fails + 1, 0)
            with self.state.lock:
                was = self.state.link_ok
            if was and fails >= self.DOWN_AFTER:
                with self.state.lock:
                    self.state.link_ok = False
                log.warning("link to %s lost", self.host)
                self.cmd.force_unkey("link to the rig lost")
            elif not was and oks >= self.UP_AFTER:
                with self.state.lock:
                    self.state.link_ok = True
                log.info("link to %s restored", self.host)
            self.stop.wait(1.0)


# --------------------------------------------------------------------------- VRChat log watcher
class LogWatcher(threading.Thread):
    JOIN_RE = re.compile(r"Joining (wrld_[A-Za-z0-9\-]+)")
    LEFT_RE = re.compile(r"\[Behaviour\] OnLeftRoom|Successfully left room")

    def __init__(self, cfg: dict, state: RigState, commander: Commander):
        super().__init__(daemon=True, name="logwatch")
        self.cfg, self.state, self.cmd = cfg, state, commander
        self.world = cfg.get("world_id", "").strip()
        self.stop = threading.Event()

    def _newest(self) -> str | None:
        files = glob.glob(os.path.join(self.cfg["vrchat_log_dir"], "output_log_*.txt"))
        return max(files, key=os.path.getmtime) if files else None

    def _scan_current_world(self, path: str):
        """Find the world we are in right now by reading the whole current log once."""
        current = ""
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                m = self.JOIN_RE.search(line)
                if m:
                    current = m.group(1)
                elif self.LEFT_RE.search(line):
                    current = ""
        self._set_world(current)

    def _set_world(self, wid: str):
        inside = bool(wid) and (not self.world or wid == self.world)
        with self.state.lock:
            was = self.state.in_world
            self.state.world_id, self.state.in_world = wid, inside
        if was and not inside:
            self.cmd.force_unkey("left the world")
        if inside != was:
            log.info("world: %s (%s)", wid or "-", "OK" if inside else "not the radio world")

    def run(self):
        path, fh, next_scan = None, None, 0.0
        while not self.stop.is_set():
            newest = path
            if time.monotonic() >= next_scan:      # look for a newer VRChat log every 2 s
                newest, next_scan = self._newest(), time.monotonic() + 2.0
            if newest and newest != path:
                if fh:
                    fh.close()
                path = newest
                self._scan_current_world(path)
                fh = open(path, "r", encoding="utf-8", errors="replace")
                fh.seek(0, os.SEEK_END)
                log.info("watching %s", os.path.basename(path))
            if not fh:
                self.stop.wait(2.0)
                continue
            line = fh.readline()
            if not line:
                self.stop.wait(0.05)
                continue
            m = self.JOIN_RE.search(line)
            if m:
                self._set_world(m.group(1))
                continue
            if self.LEFT_RE.search(line):
                self._set_world("")
                continue
            i = line.find(MARKER)
            if i >= 0:
                payload = line[i + len(MARKER):].strip()
                try:
                    self.cmd.handle(json.loads(payload))
                except json.JSONDecodeError:
                    log.warning("bad command line: %s", payload[:200])


# --------------------------------------------------------------------------- audio sources
class AudioBuffer:
    """Thread-safe FIFO of interleaved float32 stereo samples at 48 kHz."""

    def __init__(self, max_sec: float = 0.25):
        self.q: "collections.deque[np.ndarray]" = collections.deque()
        self.n = 0
        self.max = int(SR * max_sec)
        self.lock = threading.Lock()

    def push(self, frames: np.ndarray):  # shape (N, 2) float32
        with self.lock:
            self.q.append(frames)
            self.n += len(frames)
            while self.n > self.max and self.q:       # keep latency bounded
                self.n -= len(self.q.popleft())

    def pull(self, count: int) -> np.ndarray:
        out = np.zeros((count, CH), np.float32)
        got = 0
        with self.lock:
            while got < count and self.q:
                head = self.q[0]
                take = min(count - got, len(head))
                out[got:got + take] = head[:take]
                got += take
                if take == len(head):
                    self.q.popleft()
                else:
                    self.q[0] = head[take:]
            self.n -= got
        return out


def _to_stereo48k(data: np.ndarray, rate: int, channels: int) -> np.ndarray:
    if channels <= 0:
        channels = 2
    frames = data.reshape(-1, channels)
    if channels == 1:
        frames = np.repeat(frames, 2, axis=1)
    elif channels > 2:
        frames = frames[:, :2]
    if rate and rate != SR:
        n_out = int(len(frames) * SR / rate)
        if n_out <= 0:
            return np.zeros((0, 2), np.float32)
        x = np.linspace(0, len(frames) - 1, n_out)
        frames = np.stack([np.interp(x, np.arange(len(frames)), frames[:, c]) for c in range(2)], axis=1)
    return frames.astype(np.float32)


class TciAudio(threading.Thread):
    """RX audio from wfview's TCI server (TCI binary stream, RX_AUDIO frames)."""
    FMT = {0: (np.int16, 32768.0), 1: (None, None), 2: (np.int32, 2147483648.0), 3: (np.float32, 1.0)}

    def __init__(self, cfg: dict, state: RigState, buf: AudioBuffer):
        super().__init__(daemon=True, name="tci")
        self.cfg, self.state, self.buf = cfg, state, buf

    def run(self):
        asyncio.run(self._main())

    async def _main(self):
        import websockets
        while True:
            try:
                async with websockets.connect(self.cfg["tci_url"], max_size=None, ping_interval=None) as ws:
                    log.info("TCI connected %s", self.cfg["tci_url"])
                    for c in ("audio_samplerate:48000;", "audio_stream_sample_type:float32;",
                              "audio_stream_channels:2;", "audio_start:0;"):
                        await ws.send(c)
                    async for msg in ws:
                        if isinstance(msg, bytes) and len(msg) > 64:
                            self._on_binary(msg)
            except Exception as e:  # noqa: BLE001
                with self.state.lock:
                    self.state.audio_ok = False
                log.warning("TCI audio: %s (retry in 5s)", e)
                await asyncio.sleep(5)

    def _on_binary(self, msg: bytes):
        (_rx, rate, fmt, _codec, _crc, length, typ, channels) = struct.unpack_from("<8I", msg, 0)
        if typ != 1:            # 1 = RX_AUDIO_STREAM
            return
        dtype, scale = self.FMT.get(fmt, (None, None))
        if dtype is None:
            return
        data = np.frombuffer(msg, dtype=dtype, offset=64)
        if length and length < len(data):
            data = data[:length]
        samples = data.astype(np.float32) / scale
        self.buf.push(_to_stereo48k(samples, rate, channels or 2))
        with self.state.lock:
            self.state.audio_ok = True


class DeviceAudio(threading.Thread):
    """Fallback: capture RX audio from an input device (e.g. a virtual cable wfview plays into)."""

    def __init__(self, cfg: dict, state: RigState, buf: AudioBuffer):
        super().__init__(daemon=True, name="audio-dev")
        self.cfg, self.state, self.buf = cfg, state, buf

    def run(self):
        import sounddevice as sd
        dev = self.cfg.get("audio_device") or None

        def cb(indata, _frames, _time, _status):
            self.buf.push(_to_stereo48k(indata.copy().ravel(), SR, indata.shape[1]))
            with self.state.lock:
                self.state.audio_ok = True

        while True:
            try:
                with sd.InputStream(device=dev, samplerate=SR, channels=2, dtype="float32", callback=cb):
                    log.info("capturing audio from device %r", dev or "default")
                    while True:
                        time.sleep(1)
            except Exception as e:  # noqa: BLE001
                log.warning("audio device: %s (retry in 5s)", e)
                time.sleep(5)


def list_audio_devices() -> list[tuple[str, str]]:
    """[(kind, name)]: kind 'loopback' = a speaker's output (what it plays), 'mic' = an input device."""
    import soundcard as sc
    out = [("loopback", s.name) for s in sc.all_speakers()]
    out += [("mic", m.name) for m in sc.all_microphones(include_loopback=False)]
    return out


def default_speaker_name() -> str:
    try:
        import soundcard as sc
        return sc.default_speaker().name
    except Exception:  # noqa: BLE001
        return ""


def measure_audio_levels(seconds: float = 0.6) -> dict[tuple[str, str], float]:
    """RMS level of every device for a short moment, to see which one carries the radio."""
    import soundcard as sc
    import warnings
    warnings.filterwarnings("ignore", category=sc.SoundcardRuntimeWarning)
    levels: dict[tuple[str, str], float] = {}
    for kind, name in list_audio_devices():
        try:
            dev = _find_capture_device(sc, kind, name)
            with dev.recorder(samplerate=SR, channels=min(2, dev.channels)) as rec:
                d = np.asarray(rec.record(numframes=int(SR * seconds)), dtype=np.float32)
            levels[(kind, name)] = float(np.sqrt((d ** 2).mean()))
        except Exception:  # noqa: BLE001
            levels[(kind, name)] = -1.0
    return levels


def _find_capture_device(sc, kind: str, want: str):
    want = want.lower()
    if kind == "mic":
        dev = next((m for m in sc.all_microphones(include_loopback=False) if want in m.name.lower()), None)
        if dev is None:
            raise RuntimeError(f"input device containing {want!r} not found")
        return dev
    spk = next((s for s in sc.all_speakers() if want in s.name.lower()), None)
    if spk is None:
        raise RuntimeError(f"speaker containing {want!r} not found")
    return sc.get_microphone(id=str(spk.id), include_loopback=True)


def save_config(updates: dict):
    """Write a few keys back to config.json (keeps the rest of the file)."""
    data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    data.update(updates)
    CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class LoopbackAudio(threading.Thread):
    """RX audio from a sound device chosen in the tray menu:
    - 'loopback': what a speaker plays (WASAPI loopback) — wfview plays the radio to that speaker
    - 'mic'     : an input device (e.g. a virtual audio cable wfview plays into, or a line input)
    The device can be switched at runtime with switch()."""

    def __init__(self, cfg: dict, state: RigState, buf: AudioBuffer):
        super().__init__(daemon=True, name="audio-loop")
        self.cfg, self.state, self.buf = cfg, state, buf
        src = cfg.get("audio_source", "loopback")
        self.kind = "mic" if src == "mic" else "loopback"
        self.name = cfg.get("audio_device") or "Realtek"
        self._gen = 0                      # bumped by switch() to reopen the device
        self.current = ""                  # full name of the device being captured
        self.level = 0.0                   # recent RMS level (for the tray menu)

    def switch(self, kind: str, name: str):
        self.kind, self.name = kind, name
        self._gen += 1
        log.info("audio device -> %s: %s", kind, name)

    def run(self):
        import soundcard as sc
        import warnings
        warnings.filterwarnings("ignore", category=sc.SoundcardRuntimeWarning)  # data discontinuity on silence
        while True:
            gen = self._gen
            try:
                dev = _find_capture_device(sc, self.kind, self.name)
                ch = min(2, dev.channels)
                with dev.recorder(samplerate=SR, channels=ch, blocksize=SR // 50) as rec:
                    self.current = f"{'스피커' if self.kind == 'loopback' else '입력'}: {dev.name}"
                    log.info("capturing audio from %s", self.current)
                    while gen == self._gen:
                        data = np.asarray(rec.record(numframes=SR // 50), dtype=np.float32)
                        stereo = data.reshape(-1, 2) if ch == 2 else np.repeat(data.reshape(-1, 1), 2, axis=1)
                        self.buf.push(stereo)
                        self.level = 0.9 * self.level + 0.1 * float(np.sqrt((stereo ** 2).mean()))
                        with self.state.lock:
                            self.state.audio_ok = True
            except Exception as e:  # noqa: BLE001
                self.current = ""
                with self.state.lock:
                    self.state.audio_ok = False
                log.warning("audio capture: %s (retry in 5s)", e)
                for _ in range(50):              # wake up early if the device is switched
                    if gen != self._gen:
                        break
                    time.sleep(0.1)


# --------------------------------------------------------------------------- LCD renderer
def _font(names: list[str], size: int):
    for n in names:
        p = Path("C:/Windows/Fonts") / n
        if p.exists():
            return ImageFont.truetype(str(p), size)
    return ImageFont.load_default()


class Lcd:
    def __init__(self, w: int, h: int, cfg: dict):
        self.w, self.h, self.cfg = w, h, cfg
        self.f_big = _font(["consolab.ttf", "consola.ttf"], int(h * 0.30))
        self.f_mid = _font(["consolab.ttf", "consola.ttf"], int(h * 0.09))
        self.f_small = _font(["malgun.ttf", "consola.ttf"], int(h * 0.065))

    def render(self, st: RigState) -> bytes:
        w, h = self.w, self.h
        img = Image.new("RGB", (w, h), (8, 18, 34))
        d = ImageDraw.Draw(img)
        with st.lock:
            freq, mode, db, ptt, ptt_by = st.freq, st.mode, st.smeter_db, st.ptt, st.ptt_by
            connected, step_i, audio_ok, last = st.connected, st.step_index, st.audio_ok, st.last_cmd
            link_ok, pending = st.link_ok, st.unkey_pending
            tuner_on, tuning = st.tuner_on, time.time() < st.tune_until
        fg = (120, 220, 255)
        if not connected:
            d.text((w // 2, h // 2), "RIG OFFLINE", fill=(200, 80, 80), font=self.f_mid, anchor="mm")
            return img.tobytes()
        if not link_ok:
            d.text((w // 2, h * 0.42), "RIG LINK LOST", fill=(230, 150, 60), font=self.f_mid, anchor="mm")
            d.text((w // 2, h * 0.60), "reconnecting...  controls paused", fill=(150, 170, 190), font=self.f_small, anchor="mm")
            if pending:
                d.text((w // 2, h * 0.75), "PTT release pending", fill=(230, 90, 80), font=self.f_small, anchor="mm")
            return img.tobytes()
        mhz, rest = divmod(freq, 1_000_000)
        khz, hz = divmod(rest, 1000)
        d.text((w * 0.97, h * 0.42), f"{mhz}.{khz:03d}.{hz // 10:02d}", fill=(255, 255, 255), font=self.f_big, anchor="rm")
        d.text((w * 0.04, h * 0.10), mode, fill=fg, font=self.f_mid, anchor="lm")
        step = self.cfg["tune_steps_hz"][step_i % len(self.cfg["tune_steps_hz"])]
        d.text((w * 0.40, h * 0.10), f"STEP {step}Hz", fill=(150, 170, 190), font=self.f_small, anchor="lm")
        if tuner_on:
            d.text((w * 0.23, h * 0.10), "ATU", fill=(240, 200, 90), font=self.f_small, anchor="lm")
        if ptt:
            d.rounded_rectangle((w * 0.78, h * 0.03, w * 0.97, h * 0.17), 8, fill=(220, 30, 30))
            d.text((w * 0.875, h * 0.10), "TUNE" if tuning else "TX", fill=(255, 255, 255), font=self.f_mid, anchor="mm")
        else:
            d.rounded_rectangle((w * 0.78, h * 0.03, w * 0.97, h * 0.17), 8, outline=(60, 120, 80), width=3)
            d.text((w * 0.875, h * 0.10), "RX", fill=(90, 200, 120), font=self.f_mid, anchor="mm")
        # S-meter: rigctld STRENGTH is dB relative to S9 (S0 = -54 dB, +60 max)
        x0, x1, y0, y1 = w * 0.04, w * 0.96, h * 0.70, h * 0.80
        frac = (min(max(db, -54.0), 60.0) + 54.0) / 114.0
        d.rectangle((x0, y0, x1, y1), outline=(60, 90, 120), width=2)
        s9x = x0 + (x1 - x0) * 54.0 / 114.0
        d.rectangle((x0 + 2, y0 + 2, x0 + 2 + (x1 - x0 - 4) * frac, y1 - 2), fill=(255, 80, 60) if db > 0 else fg)
        for i, lab in enumerate(["1", "3", "5", "7", "9"]):
            sx = x0 + (s9x - x0) * (1 + 2 * i) / 9.0
            d.text((sx, y1 + h * 0.06), lab, fill=(150, 170, 190), font=self.f_small, anchor="mm")
        for i, lab in enumerate(["+20", "+40", "+60"]):
            sx = s9x + (x1 - s9x) * (i + 1) / 3.0
            d.text((sx, y1 + h * 0.06), lab, fill=(220, 120, 110), font=self.f_small, anchor="mm")
        s_units = 9 + db / 6.0 if db < 0 else 9
        s_txt = f"S{max(0, int(round(s_units)))}" if db <= 0 else f"S9+{int(db)}"
        d.text((w * 0.04, h * 0.60), s_txt, fill=fg, font=self.f_small, anchor="lm")
        footer = f"TX: {ptt_by}" if ptt and ptt_by else (last or "")
        d.text((w * 0.96, h * 0.60), footer, fill=(150, 170, 190), font=self.f_small, anchor="rm")
        if not audio_ok:
            d.text((w * 0.96, h * 0.96), "no audio", fill=(200, 120, 80), font=self.f_small, anchor="rb")
        return img.tobytes()


# --------------------------------------------------------------------------- HLS streamer (ffmpeg)
def find_exe(name: str) -> str | None:
    p = shutil.which(name)
    if p:
        return p
    links = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links" / f"{name}.exe"
    if links.exists():
        return str(links)
    for c in glob.glob(str(Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages" / "**" / f"{name}.exe"), recursive=True):
        return c
    for c in (Path("C:/Program Files (x86)/cloudflared") / f"{name}.exe", Path("C:/Program Files/cloudflared") / f"{name}.exe"):
        if c.exists():
            return str(c)
    return None


class RtspKeepaliveProxy(threading.Thread):
    """VRChat's RTSP client (MediaFoundation) never sends RTSP keepalives, and MediaMTX ends such sessions after
    exactly 60 s → the world screen dropped and reconnected every minute. This relay sits between the world
    (listen_port, what the world URL points at) and MediaMTX (upstream_port): it forwards both directions as-is,
    sends GET_PARAMETER keepalives upstream every 20 s for each playing session, and swallows their replies."""
    KEEPALIVE_SEC = 20
    OWN_CSEQ = 900000

    def __init__(self, listen_port: int, upstream_port: int):
        super().__init__(daemon=True, name="rtsp-proxy")
        self.listen_port, self.upstream_port = listen_port, upstream_port
        self.servers = []
        s4 = socket.socket()
        s4.bind(("127.0.0.1", listen_port))                  # raises if the port is taken (caller logs it)
        s4.listen(8)
        self.servers.append(s4)
        try:                                                 # "localhost" may resolve to ::1 first
            s6 = socket.socket(socket.AF_INET6)
            s6.bind(("::1", listen_port))
            s6.listen(8)
            self.servers.append(s6)
        except OSError:
            pass

    def run(self):
        for srv in self.servers[1:]:
            threading.Thread(target=self._accept_loop, args=(srv,), daemon=True, name="rtsp-proxy6").start()
        self._accept_loop(self.servers[0])

    def _accept_loop(self, srv: socket.socket):
        while True:
            try:
                cli, _ = srv.accept()
                threading.Thread(target=self._session, args=(cli,), daemon=True, name="rtsp-relay").start()
            except OSError as e:
                log.warning("rtsp proxy accept: %s", e)
                time.sleep(1)

    @staticmethod
    def _read_exact(sock, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("closed")
            buf += chunk
        return buf

    @classmethod
    def _read_message(cls, sock, pending: bytearray) -> bytes:
        """One RTSP message (text + body) or one interleaved '$' frame, using `pending` as read buffer."""
        def need(n):
            while len(pending) < n:
                chunk = sock.recv(65536)
                if not chunk:
                    raise ConnectionError("closed")
                pending.extend(chunk)
        need(1)
        if pending[0] == 0x24:                              # '$' channel len16 payload
            need(4)
            total = 4 + int.from_bytes(pending[2:4], "big")
            need(total)
        else:
            while b"\r\n\r\n" not in pending:
                need(len(pending) + 1)
            head_end = pending.index(b"\r\n\r\n") + 4
            m = re.search(rb"(?i)content-length:\s*(\d+)", bytes(pending[:head_end]))
            total = head_end + (int(m.group(1)) if m else 0)
            need(total)
        msg = bytes(pending[:total])
        del pending[:total]
        return msg

    def _session(self, cli: socket.socket):
        try:
            up = socket.create_connection(("127.0.0.1", self.upstream_port), timeout=5)
        except OSError as e:
            log.warning("rtsp proxy: upstream not reachable: %s", e)
            cli.close()
            return
        up.settimeout(None)
        cli.settimeout(None)
        for s_ in (cli, up):                                 # many small RTP frames: no Nagle/delayed-ACK stalls
            s_.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        lock = threading.Lock()                              # whole messages only, from either writer
        st = {"session": "", "url": "", "playing": False, "open": True, "cseq": self.OWN_CSEQ}

        def client_to_server():
            pend = bytearray()
            try:
                while st["open"]:
                    msg = self._read_message(cli, pend)
                    if msg[:1] != b"$":
                        first = msg.split(b"\r\n", 1)[0].decode(errors="replace")
                        parts = first.split(" ")
                        if len(parts) >= 2 and parts[1].startswith("rtsp://"):
                            st["url"] = parts[1].split("/trackID")[0].rstrip("/")
                        if first.startswith("PLAY "):
                            st["playing"] = True
                        if first.startswith("TEARDOWN "):
                            st["playing"] = False
                    with lock:
                        up.sendall(msg)
            except (OSError, ConnectionError):
                pass
            finally:
                st["open"] = False

        def server_to_client():
            pend = bytearray()
            try:
                while st["open"]:
                    msg = self._read_message(up, pend)
                    if msg[:1] != b"$":
                        m = re.search(rb"(?i)cseq:\s*(\d+)", msg)
                        if m and int(m.group(1)) >= self.OWN_CSEQ:
                            continue                         # reply to our keepalive: not for the player
                        s = re.search(rb"(?i)session:\s*([^;\r\n]+)", msg)
                        if s:
                            st["session"] = s.group(1).decode().strip()
                    cli.sendall(msg)
            except (OSError, ConnectionError):
                pass
            finally:
                st["open"] = False

        def keepalive():
            while st["open"]:
                time.sleep(self.KEEPALIVE_SEC)
                if st["open"] and st["playing"] and st["session"] and st["url"]:
                    st["cseq"] += 1
                    req = (f"GET_PARAMETER {st['url']} RTSP/1.0\r\nCSeq: {st['cseq']}\r\n"
                           f"Session: {st['session']}\r\n\r\n").encode()
                    try:
                        with lock:
                            up.sendall(req)
                    except OSError:
                        st["open"] = False

        threads = [threading.Thread(target=f, daemon=True) for f in (client_to_server, server_to_client, keepalive)]
        for t in threads:
            t.start()
        threads[0].join()
        threads[1].join(timeout=1)
        for s_ in (cli, up):
            try:
                s_.close()
            except OSError:
                pass


class MediaMtx(threading.Thread):
    """Local RTSP server (MediaMTX, localhost only) so the operator's world player gets ~1 s delay instead of HLS's ~3 s.
    Restarted if it exits; the streamer re-opens its RTSP output afterwards."""
    DIR = BASE / "mediamtx"
    CONF = "rigbridge-mediamtx.yml"

    def __init__(self, cfg: dict, streamer: "Streamer"):
        super().__init__(daemon=True, name="mediamtx")
        self.cfg, self.streamer = cfg, streamer
        self.port = int(cfg.get("rtsp_internal_port", 8564))   # MediaMTX itself; the world uses the keepalive proxy
        self.proc: subprocess.Popen | None = None

    @classmethod
    def available(cls) -> bool:
        return (cls.DIR / "mediamtx.exe").exists() and (cls.DIR / cls.CONF).exists()

    def _port_open(self) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", self.port), timeout=0.3):
                return True
        except OSError:
            return False

    def start_and_wait(self, timeout: float = 5.0) -> bool:
        self.start()
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self._port_open():
                return True
            time.sleep(0.1)
        return False

    def run(self):
        first = True
        while True:
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            self.proc = subprocess.Popen([str(self.DIR / "mediamtx.exe"), self.CONF], cwd=str(self.DIR),
                                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=flags)
            _bind_child(self.proc)
            if not first:
                # wait for the port, then make ffmpeg reconnect its RTSP output
                for _ in range(50):
                    if self._port_open():
                        break
                    time.sleep(0.1)
                self.streamer.restart.set()
            first = False
            for raw in self.proc.stdout:
                line = raw.decode(errors="replace").strip()
                if " ERR " in line or " WAR " in line:
                    log.warning("mediamtx: %s", line)
                elif "is publishing" in line or "is reading" in line or "started with listeners" in line:
                    log.info("mediamtx: %s", line)
            log.warning("mediamtx exited (code %s) - restarting in 3s", self.proc.poll())
            time.sleep(3)


class Streamer(threading.Thread):
    def __init__(self, cfg: dict, state: RigState, buf: AudioBuffer):
        super().__init__(daemon=True, name="streamer")
        self.cfg, self.state, self.buf = cfg, state, buf
        self.lcd = Lcd(cfg["stream_width"], cfg["stream_height"], cfg)
        self.proc: subprocess.Popen | None = None
        self.stop = threading.Event()
        self.restart = threading.Event()      # set by MediaMtx when the RTSP server came back → re-open outputs
        self.rtsp_url = ""                     # set before start() when MediaMTX is available

    def _free_port(self) -> int:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def run(self):
        ffmpeg = find_exe("ffmpeg")
        if not ffmpeg:
            log.error("ffmpeg not found - stream disabled")
            return
        while not self.stop.is_set():
            try:
                self._run_once(ffmpeg)
            except Exception as e:  # noqa: BLE001
                log.warning("streamer restart: %s", e)
            finally:
                if self.proc and self.proc.poll() is None:
                    self.proc.kill()
            self.stop.wait(2.0)

    def _run_once(self, ffmpeg: str):
        STREAM_DIR.mkdir(exist_ok=True)
        for f in STREAM_DIR.glob("*"):
            try:
                f.unlink()
            except OSError:
                pass
        w, h, fps = self.cfg["stream_width"], self.cfg["stream_height"], int(self.cfg["stream_fps"])
        port = self._free_port()
        seg_f = float(self.cfg["hls_segment_sec"])
        seg = f"{seg_f:g}"
        gop = max(1, round(fps * seg_f))            # every segment starts with a keyframe
        list_size = str(int(self.cfg.get("hls_list_size", 4)))
        args = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-framerate", str(fps), "-i", "-",
                "-f", "f32le", "-ar", str(SR), "-ac", str(CH), "-i", f"tcp://127.0.0.1:{port}?listen=1",
                "-map", "0:v", "-map", "1:a",
                "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency", "-pix_fmt", "yuv420p",
                "-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0", "-b:v", "600k",
                "-c:a", "aac", "-b:a", "96k", "-ar", str(SR)]
        # short segments + short playlist: the player cannot start far behind the live edge
        hls_opts = (f"hls_time={seg}:hls_list_size={list_size}"
                    ":hls_flags=delete_segments+independent_segments+omit_endlist:hls_segment_filename=seg_%05d.ts")
        if self.rtsp_url:
            # one encode → HLS (friends via tunnel, fallback) + RTSP to the local MediaMTX (operator, ~1 s delay).
            # onfail=ignore: if the RTSP side fails, HLS keeps going. Paths are relative (cwd = stream dir)
            # so no "C:" colons end up inside the tee spec.
            args += ["-flags", "+global_header", "-f", "tee", "-use_fifo", "1",
                     f"[f=hls:{hls_opts}]radio.m3u8|[f=rtsp:rtsp_transport=tcp:onfail=ignore]{self.rtsp_url}"]
        else:
            args += ["-f", "hls", "-hls_time", seg, "-hls_list_size", list_size,
                     "-hls_flags", "delete_segments+independent_segments+omit_endlist",
                     "-hls_segment_filename", "seg_%05d.ts", "radio.m3u8"]
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.proc = subprocess.Popen(args, stdin=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=flags,
                                     cwd=str(STREAM_DIR))
        _bind_child(self.proc)
        threading.Thread(target=self._pump_stderr, daemon=True).start()
        # first video frame lets ffmpeg move on to opening the audio listener
        self.proc.stdin.write(self.lcd.render(self.state))
        audio = None
        for _ in range(50):
            try:
                audio = socket.create_connection(("127.0.0.1", port), timeout=1.0)
                break
            except OSError:
                time.sleep(0.1)
        if audio is None:
            raise RuntimeError("ffmpeg audio input did not open")
        log.info("HLS stream running -> %s", STREAM_DIR / "radio.m3u8")
        t0 = time.perf_counter()
        frames_sent, samples_sent = 1, 0
        chunk = SR // 50   # 20 ms
        self.restart.clear()
        while not self.stop.is_set() and not self.restart.is_set() and self.proc.poll() is None:
            now = time.perf_counter() - t0
            while samples_sent < now * SR:
                audio.sendall(self.buf.pull(chunk).tobytes())
                samples_sent += chunk
            if frames_sent < now * fps:
                self.proc.stdin.write(self.lcd.render(self.state))
                frames_sent += 1
            time.sleep(0.005)
        audio.close()

    def _pump_stderr(self):
        for line in self.proc.stderr:
            log.warning("ffmpeg: %s", line.decode(errors="replace").strip())


# --------------------------------------------------------------------------- HTTP + tunnel
class StreamHandler(http.server.SimpleHTTPRequestHandler):
    state: RigState = None
    pad = None          # rigbridge_pad.Pad
    captions = None     # rigbridge_captions.Captions

    def _json(self, code: int, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_POST(self):
        """Browser extension → recommendation cards. Only extension origins (web pages cannot fake Origin)."""
        if not self.path.startswith("/pad/feed") or self.pad is None:
            self.send_error(404)
            return
        origin = self.headers.get("Origin", "")
        from rigbridge_pad import EXT_ORIGINS
        if not origin.startswith(EXT_ORIGINS):
            self._json(403, {"ok": False, "error": "extension only"})
            return
        if (self.headers.get("Host") or "").split(":")[0] not in ("127.0.0.1", "localhost"):
            self._json(403, {"ok": False, "error": "local only"})     # never via the public tunnel
            return
        try:
            n = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(min(n, 2_000_000)).decode("utf-8"))
            added = self.pad.add_feed(str(data.get("service", "")), list(data.get("items", [])))
            self._json(200, {"ok": True, "added": added, "counts": self.pad.feed.counts()})
        except Exception as e:  # noqa: BLE001
            self._json(400, {"ok": False, "error": str(e)[:200]})

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(STREAM_DIR), **kw)

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache" if self.path.endswith((".m3u8", ".json")) else "max-age=30")
        super().end_headers()

    def guess_type(self, path):
        if str(path).endswith(".m3u8"):
            return "application/vnd.apple.mpegurl"
        if str(path).endswith(".ts"):
            return "video/mp2t"
        return super().guess_type(path)

    def do_GET(self):
        if self.path.startswith("/cap.json"):
            if self.captions is None or not self._is_local_viewer():
                self.send_error(404)                            # operator's own VRChat only, not via the tunnel
                return
            self._json(200, self.captions.status())
            return
        if self.path.startswith("/pad/status.json") and self.pad is not None:
            self._json(200, self.pad.status())
            return
        if self.path.startswith("/status.json"):
            st = self.state
            with st.lock:
                body = json.dumps({"connected": st.connected, "freq": st.freq, "mode": st.mode,
                                   "smeter_db": st.smeter_db, "ptt": st.ptt, "ptt_by": st.ptt_by,
                                   "in_world": st.in_world, "audio": st.audio_ok, "link_host": st.link_host,
                                   "link_ok": st.link_ok, "unkey_pending": st.unkey_pending,
                                   "tuner_on": st.tuner_on}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/radio.m3u8") and self._is_local_viewer():
            self._send_short_playlist(int(StreamHandler.local_segments))
            return
        super().do_GET()

    local_segments = 3

    def _is_local_viewer(self) -> bool:
        """The operator's own VRChat asks for localhost:8810; the tunnel forwards with the public host name."""
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        return host in ("localhost", "127.0.0.1", "[::1]")

    def _send_short_playlist(self, keep: int):
        """Same live playlist, but only the newest `keep` segments → the player starts closer to live."""
        try:
            lines = (STREAM_DIR / "radio.m3u8").read_text(encoding="utf-8").splitlines()
        except OSError:
            self.send_error(404)
            return
        head, entries, seq, i = [], [], 0, 0
        while i < len(lines):
            ln = lines[i]
            if ln.startswith("#EXTINF") and i + 1 < len(lines):
                entries.append((ln, lines[i + 1]))
                i += 2
                continue
            if ln.startswith("#EXT-X-MEDIA-SEQUENCE:"):
                seq = int(ln.split(":", 1)[1])
            elif ln.strip():
                head.append(ln)
            i += 1
        drop = max(0, len(entries) - keep)
        out = [h for h in head]
        out.insert(min(3, len(out)), f"#EXT-X-MEDIA-SEQUENCE:{seq + drop}")
        for inf, uri in entries[drop:]:
            out += [inf, uri]
        body = ("\n".join(out) + "\n").encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.apple.mpegurl")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def start_http(cfg: dict, state: RigState):
    StreamHandler.state = state
    srv = ThreadingHTTPServer(("127.0.0.1", int(cfg["http_port"])), StreamHandler)
    threading.Thread(target=srv.serve_forever, daemon=True, name="http").start()
    log.info("HTTP: http://localhost:%s/radio.m3u8", cfg["http_port"])
    return srv


class Tunnel(threading.Thread):
    URL_RE = re.compile(r"https://[a-z0-9\-]+\.trycloudflare\.com")

    def __init__(self, cfg: dict, state: RigState):
        super().__init__(daemon=True, name="tunnel")
        self.cfg, self.state = cfg, state
        self.proc: subprocess.Popen | None = None

    def run(self):
        exe = find_exe("cloudflared")
        if not exe:
            log.error("cloudflared not found - friends cannot receive the stream")
            return
        while True:
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            self.proc = subprocess.Popen([exe, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{self.cfg['http_port']}"],
                                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=flags)
            _bind_child(self.proc)
            for raw in self.proc.stdout:
                m = self.URL_RE.search(raw.decode(errors="replace"))
                if m:
                    url = m.group(0) + "/radio.m3u8"
                    with self.state.lock:
                        self.state.public_url = url
                    log.info("public stream URL: %s", url)
            with self.state.lock:
                self.state.public_url = ""
            log.warning("cloudflared exited, restarting in 10s")
            time.sleep(10)


def copy_to_clipboard(text: str):
    if not text:
        return
    subprocess.run(["clip.exe"], input=text.encode("utf-16-le"), check=False)


# --------------------------------------------------------------------------- tray / main
def make_icon(color):
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((4, 14, 60, 50), 8, fill=(30, 30, 34))
    d.rectangle((10, 20, 38, 36), fill=color)
    d.ellipse((42, 22, 56, 36), fill=(90, 90, 96))
    return img


_MUTEX = None


def acquire_single_instance(wait_sec: float = 0.0) -> bool:
    """Only one RigBridge may run: two would write the same HLS folder (broken stream) and send every
    world command twice (double tuning / PTT). Uses a Windows named mutex; wait_sec is for restarts."""
    global _MUTEX
    if os.name != "nt":
        return True
    import ctypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = ctypes.c_void_p
    end = time.monotonic() + wait_sec
    while True:
        h = k32.CreateMutexW(None, True, "Local\\RigBridge_single_instance")
        if h and ctypes.get_last_error() != 183:      # 183 = ERROR_ALREADY_EXISTS
            _MUTEX = h
            return True
        if h:
            k32.CloseHandle(ctypes.c_void_p(h))
        if time.monotonic() >= end:
            return False
        time.sleep(0.5)


def notify_already_running():
    if os.name == "nt":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, "RigBridge가 이미 실행 중이에요.\n작업 표시줄 트레이의 무전기 아이콘을 확인하세요.\n"
                                               "(다시 시작하려면 트레이 메뉴 → 다시 시작)", "RigBridge", 0x40)


def restart_self():
    """Start a fresh RigBridge that waits for this one to exit (used after wfview/config changes)."""
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--wait"], cwd=str(BASE), creationflags=flags)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(BASE / "rigbridge.log", encoding="utf-8"),
                                  logging.StreamHandler(sys.stdout)])
    if not acquire_single_instance(20.0 if "--wait" in sys.argv else 0.0):
        log.warning("another RigBridge is already running - exiting")
        notify_already_running()
        return
    cfg = load_config()
    if not cfg.get("operator_name"):
        log.warning("config.json: operator_name is empty - only names in ptt_allowed can transmit")
    state = RigState()
    rig = RigCtl(cfg, state)
    commander = Commander(cfg, state, rig)
    buf = AudioBuffer()
    rig.start()
    LinkMonitor(cfg, state, commander).start()
    LogWatcher(cfg, state, commander).start()
    audio = {"tci": TciAudio, "device": DeviceAudio}.get(cfg.get("audio_source", "loopback"), LoopbackAudio)(cfg, state, buf)
    audio.start()
    streamer = Streamer(cfg, state, buf)
    pad_rtsp = ""
    if cfg.get("rtsp_enabled", True) and MediaMtx.available():
        mtx = MediaMtx(cfg, streamer)
        if mtx.start_and_wait():
            streamer.rtsp_url = f"rtsp://127.0.0.1:{mtx.port}/radio"
            pad_rtsp = f"rtsp://127.0.0.1:{mtx.port}/pad"
            public_port = int(cfg.get("rtsp_port", 8554))
            try:
                RtspKeepaliveProxy(public_port, mtx.port).start()
                log.info("RTSP (operator, low latency): rtsp://localhost:%s/radio (keepalive relay -> %s)", public_port, mtx.port)
            except OSError as e:
                log.warning("RTSP keepalive relay could not listen on %s: %s", public_port, e)
        else:
            log.warning("MediaMTX did not open port %s - HLS only", mtx.port)
    streamer.start()
    pad = None
    if cfg.get("pad_enabled", True):
        sys.path.insert(0, str(BASE))
        from rigbridge_pad import Pad
        pad = Pad(cfg, find_exe, _bind_child, STREAM_DIR / "pad", pad_rtsp)
        pad.start()
        commander.pad = pad
        StreamHandler.pad = pad
        log.info("pad (tablet): rtsp://localhost:%s/pad , http://localhost:%s/pad/pad.m3u8",
                 cfg.get("rtsp_port", 8554), cfg["http_port"])
    if cfg.get("captions_enabled", True):
        sys.path.insert(0, str(BASE))
        from rigbridge_captions import Captions
        captions = Captions()
        commander.captions = captions
        StreamHandler.captions = captions
        log.info("captions: http://localhost:%s/cap.json", cfg["http_port"])
    start_http(cfg, state)
    if cfg.get("tunnel_enabled"):
        Tunnel(cfg, state).start()

    def tick():
        while True:
            commander.watchdog()
            time.sleep(1)
    threading.Thread(target=tick, daemon=True).start()

    def shutdown():
        log.info("shutting down - releasing PTT")
        rig.release_ptt_now()
        streamer.stop.set()

    try:
        import pystray
    except ImportError:
        log.info("pystray not installed - running in console (Ctrl+C to quit)")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            shutdown()
        return

    local_url = f"http://localhost:{cfg['http_port']}/radio.m3u8"

    def status_text(_item=None):
        with state.lock:
            if not state.connected:
                return "무전기: 연결 안 됨 (wfview rigctld 확인)"
            if not state.link_ok:
                return f"무전기: {state.link_host} 연결 끊김 (조작 멈춤)"
            return f"{state.freq / 1e6:.5f} MHz {state.mode}  {'TX' if state.ptt else 'RX'}"

    def world_text(_item=None):
        with state.lock:
            return "월드: 입장함" if state.in_world else "월드: 대기 중"

    def public_text(_item=None):
        with state.lock:
            return "친구용 주소 복사" + ("" if state.public_url else " (준비 중)")

    # ---- receive-audio device picker
    dev_cache: dict = {"list": [], "levels": {}, "default": "", "busy": False}

    def scan_devices():
        if dev_cache["busy"]:
            return
        dev_cache["busy"] = True
        try:
            dev_cache["list"] = list_audio_devices()
            dev_cache["default"] = default_speaker_name()
            dev_cache["levels"] = measure_audio_levels()
        except Exception as e:  # noqa: BLE001
            log.warning("audio device scan: %s", e)
        finally:
            dev_cache["busy"] = False

    threading.Thread(target=scan_devices, daemon=True).start()

    def audio_text(_item=None):
        if not isinstance(audio, LoopbackAudio):
            return "수신 음성: " + cfg.get("audio_source", "?")
        cur = audio.current or "장치 없음"
        return f"수신 음성 — {cur}  (소리 {audio.level:.3f})"

    def pick(kind, name):
        def _do(_icon=None, _item=None):
            audio.switch(kind, name)
            cfg["audio_source"], cfg["audio_device"] = kind, name
            save_config({"audio_source": kind, "audio_device": name})
        return _do

    def is_current(kind, name):
        return lambda _item: (isinstance(audio, LoopbackAudio) and audio.kind == kind
                              and audio.name.lower() in name.lower())

    def device_items():
        items = []
        if not isinstance(audio, LoopbackAudio):
            items.append(pystray.MenuItem("config.json의 audio_source가 loopback/mic일 때만 바꿀 수 있어요", None, enabled=False))
            return items
        if dev_cache["busy"] and not dev_cache["list"]:
            items.append(pystray.MenuItem("장치 찾는 중…", None, enabled=False))
        for kind, name in dev_cache["list"]:
            lv = dev_cache["levels"].get((kind, name))
            lvl = "" if lv is None else (" — 열 수 없음" if lv < 0 else f" — 소리 {lv:.3f}")
            warn = "  ※ 기본 출력: VRChat 소리도 섞여요" if kind == "loopback" and name == dev_cache["default"] else ""
            label = f"{'스피커 출력' if kind == 'loopback' else '입력 장치'}: {name}{lvl}{warn}"
            items.append(pystray.MenuItem(label, pick(kind, name), checked=is_current(kind, name), radio=True))
        items.append(pystray.Menu.SEPARATOR)
        items.append(pystray.MenuItem("소리 크기 다시 재기 / 장치 새로고침",
                                      lambda: threading.Thread(target=scan_devices, daemon=True).start()))
        return items

    icon = pystray.Icon("RigBridge", make_icon((120, 220, 255)), "RigBridge", menu=pystray.Menu(
        pystray.MenuItem(status_text, None, enabled=False),
        pystray.MenuItem(world_text, None, enabled=False),
        pystray.MenuItem(audio_text, None, enabled=False),
        pystray.MenuItem("수신 음성 장치 고르기", pystray.Menu(device_items)),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(public_text, lambda: copy_to_clipboard(state.public_url)),
        pystray.MenuItem("태블릿 추천 친구용 주소 복사",
                         lambda: copy_to_clipboard(state.public_url.replace("/radio.m3u8", "/pad/pad.m3u8")),
                         visible=lambda _i: pad is not None),
        pystray.MenuItem("내 PC용 주소 복사", lambda: copy_to_clipboard(local_url)),
        pystray.MenuItem("로그 폴더 열기", lambda: os.startfile(str(BASE))),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("다시 시작 (wfview 주소·설정 다시 읽기)", lambda i: (shutdown(), restart_self(), i.stop())),
        pystray.MenuItem("종료", lambda i: (shutdown(), i.stop())),
    ))

    def refresh():
        announced = ""
        while True:
            with state.lock:
                color = ((220, 40, 40) if state.ptt else (120, 120, 120) if not state.connected
                         else (240, 160, 40) if not state.link_ok else (120, 220, 255))
                url = state.public_url
            icon.icon = make_icon(color)
            icon.update_menu()
            # 임시 터널 주소는 RigBridge를 켤 때마다 바뀐다 → 운용자에게 알려서 월드에 다시 넣게 한다
            if url and url != announced:
                announced = url
                try:
                    icon.notify("트레이 메뉴 → 친구용 주소 복사 → 월드 무전기 위 주소칸에 붙여넣고 [연결]",
                                "친구용 주소가 새로 생겼어요")
                except Exception as e:  # noqa: BLE001
                    log.warning("notify: %s", e)
            time.sleep(1)
    threading.Thread(target=refresh, daemon=True).start()
    icon.run()


if __name__ == "__main__":
    main()
