#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["bleak", "zeroconf"]
# ///
"""Clawdmeter bridge: Claude Code usage, activity and alerts for the Clawdmeter phone app.

One process, two links (the phone picks one in its settings):
  Wi-Fi      HTTP on :8787 (GET /state, POST /dismiss), announced over mDNS as _clawdmeter._tcp
  Bluetooth  connects to the phone's GATT service and writes the same JSON
Payload fields follow upstream Clawdmeter docs/protocol.md, plus `dev` (this computer),
`id` (this install), `pj` (projects being worked on), `wk` (sessions working), `dt` (when a turn
last finished), `ai` (alert id) and `stale`.

Usage: GET https://api.anthropic.com/api/oauth/usage with Claude Code's own token, read and never
refreshed (an expired token is renewed by `claude -p /usage`, like upstream).
Activity and alerts: files the `hook` command writes in the state dir (upstream's format).

    clawdbridge.py serve [--port 8787] [--no-ble]
    clawdbridge.py hook alert|clear|event [event]    Claude Code hook (JSON on stdin)
    clawdbridge.py install | uninstall               hooks + start at login, for this user
    clawdbridge.py selftest
Optional: bleak (Bluetooth), zeroconf (auto-discovery). Without them, those links are off.
"""
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

WINDOWS, MAC = os.name == "nt", sys.platform == "darwin"
HOME = Path.home()
ENV_FILE = Path(__file__).with_name("clawdbridge-env.json")  # written by `install`: the service gets the same dirs
try:
    for _k, _v in json.loads(ENV_FILE.read_text(encoding="utf-8")).items():
        os.environ.setdefault(_k, _v)
except (OSError, ValueError, AttributeError):
    pass
STATE_DIR = Path(os.environ.get("CLAWDMETER_STATE") or (
    Path(os.environ.get("LOCALAPPDATA", HOME)) / "Clawdmeter" if WINDOWS else HOME / ".config/clawdmeter")).expanduser()
CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR") or HOME / ".claude").expanduser()
ASSETS = Path(__file__).resolve().parent.parent / "android/app/src/main/assets"  # UI preview (repo only)
HOST = os.environ.get("CLAWDBRIDGE_NAME") or socket.gethostname().split(".")[0]
PORT = 8787
VERSION = "dev"  # release CI stamps the git tag here, so it matches the APK's versionName
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
ALERT_TEXT = {"question": "Question for you", "idle": "Waiting for you!", "elicitation": "MCP request!"}
ALERT_TTL, ATTENTION_MAX_AGE = 120, 60
MIN_POLL_GAP = 120  # never ask the rate-limited endpoint twice within 2 min
CWD_RE = re.compile(rb'"cwd":"((?:[^"\\]|\\.)*)"')
SVC = "4c41555a-4465-7669-6365-000000000001"  # the phone app's GATT service (upstream's UUIDs)
MARKER = (0xFFFF, b"clwd")  # in the phone's advertisement; BlueZ may report a paired phone's stale service list
RX, TX, REQ = (SVC[:-1] + c for c in "234")

lock = threading.Lock()


def log(*a):
    line = time.strftime("%H:%M:%S ") + " ".join(map(str, a))
    if sys.stderr:
        print(line, file=sys.stderr, flush=True)
    else:  # pythonw (Windows autostart) has no stderr
        with open(STATE_DIR / "bridge.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")


# ---- usage ----------------------------------------------------------------------------

def keychain_secret():
    """macOS: Claude Code keeps its sign-in in the login Keychain (upstream oauth.rs)."""
    import hashlib
    import unicodedata
    svc = "Claude Code-credentials"
    if CLAUDE_DIR != HOME / ".claude":
        svc += "-" + hashlib.sha256(unicodedata.normalize("NFC", str(CLAUDE_DIR)).encode()).hexdigest()[:8]
    try:
        out = subprocess.run(["security", "find-generic-password", "-a", os.environ.get("USER", ""), "-w", "-s", svc],
                             capture_output=True, text=True, timeout=15).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    if out and len(out) % 2 == 0 and re.fullmatch(r"[0-9a-fA-F]+", out):  # `security -w` hex-dumps some secrets
        try:
            return bytes.fromhex(out).decode()
        except ValueError:
            pass
    return out or None


def read_token():
    raw = (keychain_secret() if MAC else None) or (CLAUDE_DIR / ".credentials.json").read_text(encoding="utf-8")
    o = json.loads(raw)
    o = o.get("claudeAiOauth", o)
    return o.get("accessToken"), o.get("expiresAt")


def find_claude():
    """Services start with a bare PATH; look where Claude Code's installers put it too."""
    extra = [HOME / ".local/bin", HOME / ".claude/local", Path("/opt/homebrew/bin"), Path("/usr/local/bin"),
             Path(os.environ.get("APPDATA", HOME)) / "npm"]
    return shutil.which("claude", path=os.pathsep.join([os.environ.get("PATH", "")] + [str(p) for p in extra]))


def renew_token():
    """Claude Code owns refresh; any CLI call that needs the token renews it."""
    claude = find_claude()
    if not claude:
        return log("token expired and the claude CLI wasn't found to renew it")
    log("token expired or refused; running `claude -p /usage` to renew it")
    try:
        subprocess.run([claude, "-p", "/usage", "--output-format", "json"], check=False, cwd=HOME, capture_output=True,
                       timeout=120, stdin=subprocess.DEVNULL, env={**os.environ, "CLAWDBRIDGE_NOHOOK": "1"})
    except (OSError, subprocess.SubprocessError) as e:
        log("renew failed:", e)


def http_usage(token):
    import urllib.request
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": "clawdmeter-bridge/1.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def fetch_usage():
    import urllib.error
    token, exp = read_token()
    if exp and exp / 1000 <= time.time() + 60:
        renew_token()
        token, _ = read_token()
    try:
        return http_usage(token)
    except urllib.error.HTTPError as e:
        if e.code != 401:
            raise
    renew_token()
    return http_usage(read_token()[0])


def ts(s):
    from datetime import datetime
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() if s else None


def parse_usage(v, now):
    """Endpoint JSON -> {pct, reset epoch} per window, like upstream oauth.rs."""
    def window(k):
        w = v.get(k)
        if not isinstance(w, dict) or w.get("utilization") is None:
            return None
        return max(0, round(w["utilization"])), ts(w.get("resets_at"))

    session, week = window("five_hour"), window("seven_day")
    if session and not (session[1] and session[1] > now):
        session = (0, None)  # window not started
    if session is None and week is None:
        raise ValueError("no usage windows in response")
    # ponytail: spend-capped (`ent`) accounts not mapped; add tp/pd/rd from `limits` if needed.
    return {"acct": "pro" if v.get("five_hour") is not None else "week",
            "session": session or (0, None), "week": week or (0, None)}


def minutes_until(epoch, now):
    return -1 if epoch is None else max(0, round((epoch - now) / 60))


# ---- hooks: events.log + attention files (upstream's format) ----------------------------

def clean(s):
    return re.sub(r"[^A-Za-z0-9_.-]", "", "" if s is None else str(s))


def run_hook(args, text, now=None):
    """`hook alert|clear|event [event]`: record what Claude Code is doing. Never the prompt or tool call."""
    v = json.loads(text or "null")
    if not isinstance(v, dict):
        return
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    mode, sid = (args or [""])[0], clean(v.get("session_id"))
    if mode == "event" or "event" in args[1:]:
        tp = Path(str(v.get("transcript_path") or ""))
        acct = next((str(a.parent) for a in tp.parents if a.name == "projects"), "")
        line = f"{int(now or time.time())} {clean(v.get('hook_event_name')) or 'unknown'} {sid or '-'}"
        f = STATE_DIR / "events.log"
        if f.exists() and f.stat().st_size > 262144:
            os.replace(f, STATE_DIR / "events.log.1")
        with open(f, "a", encoding="utf-8") as fh:  # one write: concurrent hooks append whole lines
            fh.write(line + (f" {acct}" if acct else "") + "\n")
    if mode == "alert":
        nt = v.get("notification_type")
        kind = "question" if v.get("hook_event_name") == "PreToolUse" else {
            None: "permission", "permission_prompt": "permission", "idle_prompt": "idle",
            "elicitation_dialog": "elicitation"}.get(nt, "other")
        questions = (v.get("tool_input") or {}).get("questions") if isinstance(v.get("tool_input"), dict) else None
        msg = v.get("message") or (questions[0].get("question") if questions and isinstance(questions[0], dict) else "")
        tmp = STATE_DIR / f"attention.{os.getpid()}"
        tmp.write_text(json.dumps({"k": kind, "m": msg or "", "s": v.get("session_id")}, separators=(",", ":")) + "\n")
        os.replace(tmp, STATE_DIR / "attention")
    elif mode == "clear" and sid and ".." not in sid:
        (STATE_DIR / f"clear-{sid}").touch()


def hook(args):
    if os.environ.get("CLAWDBRIDGE_NOHOOK"):  # the bridge's own `claude -p /usage`
        return
    try:  # hook output can reach the model: print nothing, always exit 0
        run_hook(args, sys.stdin.buffer.read().decode("utf-8", "replace"))
    except Exception:
        pass


class Activity:
    """events.log lines `<ts> <HookEvent> <session> [<config dir>]` -> a, n, e, es."""

    def __init__(self):
        self.tools, self.sessions, self.accounts = deque(), {}, {}
        self.last_kind = {}  # sid -> (latest hook event, ts): is Claude working there?
        self.last_event = self.last_prompt = self.last_stop = None
        self.latest, self.seq = None, 0

    def ingest(self, line, live=True):
        parts = line.split(None, 3)  # ts, event, session, config dir (may hold spaces)
        if len(parts) < 2:
            return None
        try:
            t = int(float(parts[0]))
        except ValueError:
            return None
        kind, sid = parts[1], parts[2] if len(parts) > 2 and parts[2] != "-" else ""
        gap = None if self.last_event is None else t - self.last_event
        if kind != "SessionStart":  # opening Claude isn't activity: the first prompt after a break is still "back"
            self.last_event = t if self.last_event is None else max(self.last_event, t)
        if sid:
            self.sessions[sid] = max(self.sessions.get(sid, t), t)
            if len(parts) > 3:
                self.accounts[sid] = parts[3].strip()
            self.last_kind[sid] = (kind, t)
        code = None
        if kind == "PostToolUse":
            self.tools.append(t)
        elif kind == "UserPromptSubmit":
            self.last_prompt = t
            code = "b" if gap is not None and gap >= 600 else "p"
        elif kind == "Stop":
            self.last_stop = t
            code = "s"
        if live and code:
            self.latest, self.seq = code, self.seq + 1
        return code

    def prune(self, now, keep=86400):
        """Forget sessions quiet for a day (a long-running bridge would otherwise grow forever)."""
        for sid in [s for s, t in self.sessions.items() if now - t > keep]:
            for d in (self.sessions, self.accounts, self.last_kind):
                d.pop(sid, None)

    def level(self, now):
        while self.tools and now - self.tools[0] >= 60:
            self.tools.popleft()
        n = len(self.tools)
        return 0 if n == 0 else 1 if n <= 3 else 2 if n <= 9 else 3

    def live(self, sid, now):
        """An event in the last 5 min, and Claude Code hasn't exited (SessionEnd)."""
        return now - self.sessions[sid] < 300 and self.last_kind[sid][0] != "SessionEnd"

    def active_sessions(self, now):
        return sum(1 for sid in self.sessions if self.live(sid, now))

    def working_sessions(self, now):
        """Sessions whose latest event is a prompt or a tool run (not Stop, not a wait for the user).
        No Stop arrives when a turn is interrupted, so 15 quiet minutes also count as done."""
        return sum(1 for k, t in self.last_kind.values()
                   if k in ("UserPromptSubmit", "PostToolUse", "PostToolUseFailure") and now - t < 900)

    def working(self, now):
        open_prompt = self.last_prompt and (not self.last_stop or self.last_prompt > self.last_stop)
        return bool(open_prompt and self.last_event and now - self.last_event < 600)


class Tail:
    def __init__(self, path):
        self.path, self.off, self.ino = path, 0, None

    def read(self):
        try:
            st = self.path.stat()
        except OSError:
            return []
        if st.st_ino != self.ino or st.st_size < self.off:  # rolled over
            self.off, self.ino = 0, st.st_ino
        if st.st_size == self.off:
            return []
        with self.path.open("rb") as f:
            f.seek(self.off)
            data = f.read()
        end = data.rfind(b"\n") + 1  # keep a half-written line for next time
        self.off += end
        return data[:end].decode("utf-8", "replace").splitlines()


def project_of(sid, config_dir):
    """Basename of the dir a session was started in: the first `cwd` in its transcript."""
    try:
        for t in Path(config_dir or CLAUDE_DIR).glob(f"projects/*/{sid}.jsonl"):
            with t.open("rb") as f:
                found = CWD_RE.search(f.read(65536))
            if found:
                return Path(json.loads(b'"' + found.group(1) + b'"').replace("\\", "/")).name[:40]
    except (OSError, ValueError):  # unreadable, deleted mid-read, odd bytes
        pass
    return None


def take_alert(now):
    flag, taking = STATE_DIR / "attention", STATE_DIR / "attention.taking"
    try:
        os.replace(flag, taking)
    except OSError:
        return None
    try:
        mtime, raw = taking.stat().st_mtime, taking.read_text(encoding="utf-8")
    finally:
        taking.unlink(missing_ok=True)
    if now - mtime >= ATTENTION_MAX_AGE:
        return None
    try:
        v = json.loads(raw)
    except ValueError:
        v = {}
    k = v.get("k") or "permission"
    if k == "idle":  # like upstream's default alert_on: everything but idle
        return None
    return {"m": ALERT_TEXT.get(k, "Permission needed!"), "s": str(v.get("s") or ""), "at": mtime,
            "id": int(mtime * 1000)}


def take_clears():
    out = {}
    for p in STATE_DIR.glob("clear-*"):
        try:
            m = p.stat().st_mtime
            p.unlink()
            out[p.name[6:]] = m
        except OSError:
            pass
    return out


# ---- engine ---------------------------------------------------------------------------

class Engine:
    def __init__(self):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self.act = Activity()
        self.tail = Tail(STATE_DIR / "events.log")
        for line in self.tail.read():  # history without replaying old events
            self.act.ingest(line, live=False)
        self.usage, self.good_at, self.err = None, 0.0, ""  # good_at, *_poll: time.monotonic() (clock jumps)
        self.alert, self.last_poll, self.next_poll, self.misses = None, -1e9, 0.0, 0
        self.proj = {}  # sid -> (project name or None, checked at)
        self.http_seen = 0  # last Wi-Fi poll from a phone
        self.polling, self.pruned = False, 0
        idf = STATE_DIR / "bridge-id"  # tells apart computers with the same hostname
        try:
            self.id = idf.read_text().strip()
        except OSError:
            self.id = ""
        if not re.fullmatch(r"[0-9a-f]{12}", self.id):
            self.id = os.urandom(6).hex()
            idf.write_text(self.id)
        (STATE_DIR / "attention").unlink(missing_ok=True)
        take_clears()

    def tick(self):
        now, mono = time.time(), time.monotonic()
        with lock:
            for line in self.tail.read():
                code = self.act.ingest(line)
                if code in ("p", "b"):
                    self.next_poll = min(self.next_poll, mono)
                elif code == "s":
                    self.next_poll = min(self.next_poll, mono + 10)
            clears = take_clears()  # before the alert: a clear and a new prompt can land in one tick

            def cleared(al):
                return al is not None and clears.get(al["s"], 0) > al["at"]

            if cleared(self.alert):
                self.alert = None
            a = take_alert(now)
            if a and not cleared(a) and not (self.alert and self.alert["s"] == a["s"]):  # AskUserQuestion notifies twice
                self.alert = a
            if self.alert and now - self.alert["at"] >= ALERT_TTL:
                self.alert = None
            if now - self.pruned > 3600:
                self.pruned = now
                self.act.prune(now)
                self.proj = {sid: v for sid, v in self.proj.items() if sid in self.act.sessions}
            due = not self.polling and mono >= self.next_poll and mono - self.last_poll >= MIN_POLL_GAP
            if due:
                self.polling, self.last_poll = True, mono
        if due:  # its own thread: a token renewal (`claude -p /usage`) can take minutes; alerts can't wait
            threading.Thread(target=self.poll, args=(now, mono), daemon=True).start()

    def poll(self, now, mono):
        import urllib.error
        try:
            u = parse_usage(fetch_usage(), now)
            with lock:
                self.usage, self.good_at, self.err, self.misses = u, mono, "", 0
                quiet = now - (self.act.last_event or 0)
                self.next_poll = mono + (180 if self.act.working(now) else 300 if quiet < 600 else 900)
            log(f"usage: session {u['session'][0]}% week {u['week'][0]}% ({u['acct']})")
        except Exception as e:  # network, 429, 5xx, bad token: keep last values, back off
            wait = 600 * min(3, self.misses + 1)
            if isinstance(e, urllib.error.HTTPError) and e.code == 429:
                wait = max(wait, retry_after(e.headers.get("retry-after")))
            with lock:
                self.misses += 1
                self.err, self.next_poll = f"{type(e).__name__}: {e}", mono + wait
            log(f"usage poll failed ({self.err}); retrying in {wait}s")
        finally:
            self.polling = False

    def projects(self, now):
        """Projects of the live sessions, newest first; else the latest one."""
        recent = sorted(self.act.sessions, key=self.act.sessions.get, reverse=True)
        live = [sid for sid in recent if self.act.live(sid, now)]
        names = []
        for sid in (live or recent)[:20]:
            if names and not live:
                break
            name, at = self.proj.get(sid, (None, 0))
            if name is None and now - at > 30:  # transcript may not exist yet: retry every 30 s
                name = project_of(sid, self.act.accounts.get(sid))
                self.proj[sid] = (name, now)
            if name and name not in names:
                names.append(name)
        return names[:3]

    def payload(self, now=None):
        now = now or time.time()
        with lock:
            p = {"a": self.act.level(now), "n": self.act.active_sessions(now), "c": 1, "pm": 1,
                 "dev": HOST, "id": self.id, "pj": self.projects(now), "wk": self.act.working_sessions(now)}
            if self.act.last_stop:
                p["dt"] = self.act.last_stop
            if self.act.latest:
                p.update(e=self.act.latest, es=self.act.seq)
            if self.alert:
                p.update(m=self.alert["m"], b=1, ai=self.alert["id"])
            else:
                p["x"] = 1
            if not self.usage:
                p["ok"] = False
                if self.err:
                    p["err"] = self.err[:120]
                return p
            (s, sr), (w, wr) = self.usage["session"], self.usage["week"]
            if sr and sr <= now:  # the 5-hour window ended: 0 % until a poll says otherwise
                s, sr = 0, None
            if wr and wr <= now:
                w, wr = 0, None
            p.update(s=s, sr=minutes_until(sr, now), w=w, wr=minutes_until(wr, now), acct=self.usage["acct"],
                     st="limited" if max(s, w) >= 100 else "allowed", ok=True)
            if time.monotonic() - self.good_at > 20 * 60 or self.misses >= 3:
                p["stale"] = 1
            return p

    def dismiss(self):
        with lock:
            self.alert = None


# ---- links: Bluetooth (bleak) and network announcement (zeroconf) ------------------------

def ble_link(engine):
    """Find the phone app's GATT service, then write the payload on change (and every 30 s)."""
    try:
        import asyncio
        from bleak import BleakClient, BleakScanner
    except ImportError:
        return log("bleak not installed: Bluetooth link off")

    async def session(dev):
        gone, want = asyncio.Event(), asyncio.Event()

        def on_tx(_ch, data):  # the phone ended an alert
            if b'"dismiss":true' in bytes(data).replace(b" ", b""):
                engine.dismiss()

        async with BleakClient(dev, timeout=20, disconnected_callback=lambda _c: gone.set()) as c:
            log(f"bluetooth: connected to {dev.name or dev.address}")
            await c.start_notify(TX, on_tx)
            await c.start_notify(REQ, lambda _ch, _d: want.set())
            last, at = b"", 0.0
            while not gone.is_set():
                body = ble_body(engine.payload())
                if body != last or want.is_set() or time.time() - at > 30:
                    want.clear()
                    await c.write_gatt_char(RX, body, response=True)
                    last, at = body, time.time()
                await asyncio.sleep(0.5)
        log("bluetooth: disconnected")

    async def main():
        waiting_since = 0.0  # a paired phone we're letting BlueZ link up
        while True:
            if time.time() - engine.http_seen < 60:  # the phone is on Wi-Fi: don't scan
                await asyncio.sleep(10)
                continue
            dev = None
            try:
                dev = await BleakScanner.find_device_by_filter(lambda d, ad: is_phone(ad), timeout=6)
                props = dev.details.get("props", {}) if dev and isinstance(dev.details, dict) else {}
                if dev and props.get("Paired") and not props.get("Connected"):
                    # A phone paired with this computer (e.g. for upstream's app) advertises from a private
                    # address: BlueZ links it up by itself, and a Connect of ours would hold the controller
                    # and starve that. Give BlueZ a minute, then try ourselves.
                    if not waiting_since:
                        waiting_since = time.time()
                        log("bluetooth: this phone is paired with this computer. The bridge doesn't need that, and"
                            " paired Android phones often can't be reached from Linux: if it doesn't connect, forget"
                            " the pairing on both the phone and this computer.")
                    if time.time() - waiting_since < 60:
                        await asyncio.sleep(5)
                        continue
                if dev:
                    waiting_since = 0.0
                    await session(dev)  # bleak reuses a link BlueZ already has
            except Exception as e:  # adapter off, link lost, permission denied...
                log("bluetooth:", type(e).__name__, e)
            await asyncio.sleep(3 if dev else 20)

    asyncio.run(main())


def ble_body(p):
    """The payload as one GATT write: UTF-8 (not 6-byte \\u escapes) and within the 512-byte attribute limit."""
    p = {k: v for k, v in p.items() if k != "err"}
    while True:
        body = json.dumps(p, separators=(",", ":"), ensure_ascii=False).encode()
        if len(body) <= 500 or not p.get("pj"):
            return body
        p["pj"] = p["pj"][:-1]


def retry_after(v):
    try:
        return max(0, int(float(v or 0)))
    except ValueError:  # an HTTP date: fall back to our own backoff
        return 0


def is_phone(ad):
    """The Clawdmeter app's advertisement: its service, or its marker (which survives BlueZ's cache)."""
    return SVC in (u.lower() for u in ad.service_uuids) or \
        bytes(ad.manufacturer_data.get(MARKER[0], b""))[:4] == MARKER[1]


def default_ip():
    """This computer's address on its default route (no packet is sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            return s.getsockname()[0]
    except OSError:
        return None


def lan_ips():
    """IPv4 addresses, the default route's first: Android 13 uses only the first (not a VPN's or Docker's)."""
    import ifaddr
    ips = sorted({a.ip for ad in ifaddr.get_adapters() for a in ad.ips
                  if isinstance(a.ip, str) and not a.ip.startswith(("127.", "169.254."))})
    d = default_ip()
    return ([d] if d in ips else []) + [ip for ip in ips if ip != d]


def advertise(port, bridge_id):
    """Announce `_clawdmeter._tcp` so the phone finds this computer by itself."""
    try:
        from zeroconf import IPVersion, ServiceInfo, Zeroconf
    except ImportError:
        return log("zeroconf not installed: the phone can't find this computer by itself (enter its address)")
    zc, ips = None, None
    while True:  # at login the network may not be up yet; later DHCP, VPNs and docking change it
        now_ips = lan_ips()
        if zc is None or set(now_ips) != set(ips):  # the order (default route first) may flap between NICs
            if zc:  # start over: zeroconf only listens on the interfaces it was created with
                try:
                    zc.close()
                except Exception:
                    pass
            try:
                info = ServiceInfo("_clawdmeter._tcp.local.", f"Clawdmeter on {HOST}._clawdmeter._tcp.local.",
                                   port=port, parsed_addresses=now_ips, properties={"dev": HOST},
                                   server=f"clawdbridge-{bridge_id}.local.")  # unique, and not the host's own name
                zc = Zeroconf(ip_version=IPVersion.V4Only)
                zc.register_service(info, allow_name_change=True)  # two computers with one hostname: "(2)"
                ips = now_ips
                log(f"announced as '{info.name.split('._')[0]}' on :{port} ({', '.join(ips)})")
            except Exception as e:
                zc = None
                log("mDNS announce failed, retrying in 30 s:", type(e).__name__, e)
                time.sleep(30)
                continue
        time.sleep(60)


def quit_running(port=PORT):
    """Ask a bridge already on this port to exit (reinstall / second start); leave other servers alone."""
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/state", timeout=2) as r:
            if "dev" not in json.load(r):
                return
        urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/quit", data=b"",
                                                      headers={"X-Clawdbridge": "quit"}), timeout=2)
    except (OSError, ValueError, AttributeError, TypeError):
        return
    for _ in range(20):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)):
                return
        time.sleep(0.25)


def serve(port, ble=True):
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    for p in range(port, port + 10):  # a bridge left over on a fallback port too
        quit_running(p)
    engine = Engine()

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(ASSETS), **kw)

        def end_headers(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            super().end_headers()

        def json(self, obj):
            body = json.dumps(obj, separators=(",", ":")).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.split("?")[0] == "/state":
                engine.http_seen = time.time()
                return self.json(engine.payload())
            super().do_GET()  # the UI itself, for previewing in a browser

        def do_POST(self):
            if self.path == "/dismiss":
                engine.dismiss()
                return self.json({"ok": True})
            # a custom header: a web page can't send it to 127.0.0.1 without a CORS preflight, which we never grant
            if self.path == "/quit" and self.client_address[0] == "127.0.0.1" and \
                    self.headers.get("X-Clawdbridge") == "quit":
                self.json({"ok": True})
                threading.Timer(0.2, os._exit, [0]).start()
                return
            self.send_error(404)

        def log_message(self, *a):
            pass

    def loop():
        while True:
            try:
                engine.tick()
            except Exception as e:  # never let the loop die
                log("tick error:", e)
            time.sleep(0.5)

    server = None
    for p in range(port, port + 10):  # 8787 is also wrangler's and RStudio's default: take the next free one
        try:
            server = ThreadingHTTPServer(("0.0.0.0", p), Handler)
            break
        except OSError:
            log(f"port {p} is in use")
    if not server:
        raise SystemExit(f"no free port in {port}-{port + 9}")
    port = server.server_address[1]
    threading.Thread(target=loop, daemon=True).start()
    threading.Thread(target=advertise, args=(port, engine.id), daemon=True).start()
    if ble:
        threading.Thread(target=ble_link, args=(engine,), daemon=True).start()
    log(f"Clawdmeter bridge '{HOST}' on :{port} (state {STATE_DIR}, claude {CLAUDE_DIR})")
    server.serve_forever()


# ---- install: Claude Code hooks + start at login ------------------------------------------

HOOKS = [("UserPromptSubmit", None, "clear event"), ("PostToolUse", None, "clear event"),
         ("PostToolUseFailure", None, "clear event"), ("Stop", None, "clear event"),  # denied / Esc / failed: alert over
         ("SessionStart", "startup|resume|clear", "event"),  # opened: active before the first prompt (not compact)
         ("SessionEnd", None, "clear event"),  # exited: not active any more
         ("Notification", None, "alert event"), ("PreToolUse", "AskUserQuestion", "alert event")]
OURS = re.compile(r'(clawdbridge\.py|clawdmeter(\.exe)?)"? hook (alert|clear|event)')  # also upstream's entries


def our_hooks(prefix):
    """settings.json "hooks" entries for the bridge run as `prefix` (quoted python + script)."""
    out = {}
    for ev, matcher, args in HOOKS:
        g = {"hooks": [{"type": "command", "command": f"{prefix} hook {args}"}]}
        out.setdefault(ev, []).append({"matcher": matcher, **g} if matcher else g)
    return out


def set_hooks(path, prefix):
    """Replace Clawdmeter hook entries (ours or upstream's) in a settings.json; prefix None = remove only."""
    path = Path(os.path.realpath(path))  # a symlinked settings.json stays a symlink
    try:
        text = path.read_text(encoding="utf-8-sig") if path.exists() else ""
        d = json.loads(text or "{}")
    except ValueError as e:
        raise SystemExit(f"{path} isn't valid JSON ({e}). Fix it and run this again; nothing was changed.")
    if not isinstance(d, dict) or not isinstance(d.get("hooks") or {}, dict):
        raise SystemExit(f"{path} has an unexpected shape (\"hooks\" should be an object); nothing was changed.")
    hooks = d.get("hooks") or {}
    for ev in list(hooks):
        if not isinstance(hooks[ev], list):
            continue
        groups = []
        for g in hooks[ev]:
            if not isinstance(g, dict) or not isinstance(g.get("hooks", []), list):
                groups.append(g)
                continue
            kept = [h for h in g["hooks"] if not (isinstance(h, dict) and OURS.search(str(h.get("command", ""))))]
            if kept or not g.get("hooks"):
                groups.append({**g, "hooks": kept} if g.get("hooks") else g)
        if groups:
            hooks[ev] = groups
        else:
            del hooks[ev]
    for ev, groups in (our_hooks(prefix) if prefix else {}).items():
        hooks.setdefault(ev, []).extend(groups)
    if hooks:
        d["hooks"] = hooks
    else:
        d.pop("hooks", None)
    if d == json.loads(text or "{}"):  # already right: a read-only (e.g. home-manager) file the user filled in
        return
    bak = path.with_name(path.name + ".bak-clawdbridge")
    if path.exists() and not bak.exists():
        shutil.copy2(path, bak)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(d, indent=2) + "\n", encoding="utf-8")
    if path.exists():
        shutil.copymode(path, tmp)
    os.replace(tmp, path)


def spawn(cmd):
    with open(STATE_DIR / "bridge.log", "ab") as logf:
        kw = {"creationflags": 0x00000008 | 0x00000200} if WINDOWS else {"start_new_session": True}
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=logf, stderr=logf, close_fds=True, **kw)


CRON_TAG = "# clawdbridge"


def set_cron(line):
    """Put `@reboot <line>` in this user's crontab, or with None take it out. True if that worked."""
    if not shutil.which("crontab"):
        return False
    r = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    if r.returncode and not re.search(r"no f?crontab|no such file", r.stderr, re.I):
        return False  # couldn't read it: writing would wipe whatever is there
    old = r.stdout.splitlines()
    new = [x for x in old if not x.endswith(CRON_TAG)] + ([f"@reboot {line} {CRON_TAG}"] if line else [])
    return new == old or subprocess.run(["crontab", "-"], input="\n".join(new) + "\n", text=True,
                                        capture_output=True).returncode == 0


def cron_running():
    for p in Path("/proc").glob("[0-9]*/comm"):  # crond, cron, cronie, fcron...
        try:
            if "cron" in p.read_text():
                return True
        except OSError:  # exited meanwhile
            pass
    return False


def autostart_linux(on, cmd):
    """A systemd user service; without one, an @reboot crontab line; else a desktop autostart entry."""
    import pwd
    unit = HOME / ".config/systemd/user/clawdbridge.service"
    desktop = HOME / ".config/autostart/clawdbridge.desktop"
    sysd = shutil.which("systemctl") and subprocess.run(["systemctl", "--user", "show-environment"],
                                                        capture_output=True).returncode == 0
    gui = os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    quoted = " ".join(f'"{c}"' for c in cmd)
    if sysd and on:
        set_cron(None)  # left from an install without systemd
        desktop.unlink(missing_ok=True)
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text(f"""[Unit]
Description=Clawdmeter bridge (Claude Code usage for the Clawdmeter phone app)
After=network-online.target bluetooth.target
# upstream's daemon would also drive the phone and consume the same hook files
Conflicts=clawdmeter.service

[Service]
ExecStart={quoted}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
""")
        for a in (["daemon-reload"], ["enable", unit.name], ["restart", unit.name]):
            subprocess.run(["systemctl", "--user", *a], check=False)
        how = "starts at login as a systemd user service (clawdbridge), and is running now"
        if not gui and not Path("/var/lib/systemd/linger", pwd.getpwuid(os.getuid()).pw_name).exists():
            how += ("\n  - it stops when you log out, until your next login. To keep it running (headless), run once:"
                    "\n      loginctl enable-linger")
        return how
    if sysd:
        subprocess.run(["systemctl", "--user", "disable", "--now", unit.name], capture_output=True)
        unit.unlink(missing_ok=True)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    # no systemd user session (OpenRC, runit, sysvinit, containers...): cron works headless too
    cron = on and cron_running() and set_cron(f'{quoted} >>"{STATE_DIR / "bridge.log"}" 2>&1')
    if not cron:
        set_cron(None)
    if on and not cron:
        desktop.parent.mkdir(parents=True, exist_ok=True)
        desktop.write_text(f"[Desktop Entry]\nType=Application\nName=Clawdmeter bridge\nExec={quoted}\nTerminal=false\n")
    else:
        desktop.unlink(missing_ok=True)
    if not on:
        return
    spawn(cmd)
    if cron:
        return "starts at boot from your crontab (@reboot), and is running now"
    if gui:
        return ("starts at login from a desktop autostart entry, and is running now"
                "\n    (GNOME, KDE, Xfce... run it; a bare window manager needs the command below in its own startup)"
                f"\n    {quoted}")
    return ("is running now, but nothing will start it after a reboot (no systemd user session, no cron daemon)."
            f"\n    To fix that, have your init system or login script run: {quoted}")


def autostart_mac(on, cmd):
    from xml.sax.saxutils import escape
    plist = HOME / "Library/LaunchAgents/com.clawdmeter.bridge.plist"
    dom = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", dom, str(plist)], capture_output=True)
    if not on:
        plist.unlink(missing_ok=True)
        return
    plist.parent.mkdir(parents=True, exist_ok=True)
    args = "".join(f"<string>{escape(c)}</string>" for c in cmd)
    plist.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.clawdmeter.bridge</string>
  <key>ProgramArguments</key><array>{args}</array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardErrorPath</key><string>{escape(str(STATE_DIR / "bridge.log"))}</string>
</dict></plist>
""")
    subprocess.run(["launchctl", "bootstrap", dom, str(plist)], check=False)
    return "starts at login as a LaunchAgent (com.clawdmeter.bridge), and is running now"


def autostart_windows(on, cmd):
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0,
                        winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, "Clawdbridge", 0, winreg.REG_SZ, subprocess.list2cmdline(cmd))
        else:
            try:
                winreg.DeleteValue(k, "Clawdbridge")
            except FileNotFoundError:
                pass
    if on:
        spawn(cmd)
        return "starts at sign-in from HKCU Run (Clawdbridge), and is running now"


def nat_note():
    """Linux that sits behind NAT with no Bluetooth: the phone can't reach the bridge there as is."""
    try:
        wsl = "microsoft" in Path("/proc/sys/kernel/osrelease").read_text().lower()
    except OSError:
        wsl = False
    if wsl:
        return """
Note: this is WSL. The phone usually can't reach it: WSL 2 is behind NAT (no mDNS from outside) and has no
Bluetooth. Mirrored networking may let the phone reach it by address: https://learn.microsoft.com/windows/wsl/networking
The Windows installer doesn't help here: it reads Windows' .claude folder, not this one."""
    if Path("/opt/google/cros-containers").exists():
        return """
Note: this is ChromeOS's Linux. The phone can't reach it as is: it's behind NAT and has no Bluetooth.
Forward TCP 8787 (ChromeOS settings, Linux development environment, Port forwarding), then add the
Chromebook's own address on the phone."""
    return ""


def install(on=True):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    py, script = Path(sys.executable), Path(__file__).resolve()
    bg = py.with_name("pythonw.exe") if WINDOWS and py.with_name("pythonw.exe").exists() else py
    settings = CLAUDE_DIR / "settings.json"
    prefix = f'"{py.as_posix()}" "{script.as_posix()}"'
    try:
        set_hooks(settings, prefix if on else None)
        hooks = (f"Claude Code hooks added to {settings} (backup: settings.json.bak-clawdbridge)" if on
                 else f"hooks taken out of {settings}")
    except OSError as e:  # e.g. home-manager's settings.json: a link into the read-only /nix/store
        hooks = f"{settings} can't be written ({e.strerror or e}): "
        hooks += ("add these to whatever manages it (home-manager...), then run this again:\n"
                  + json.dumps({"hooks": our_hooks(prefix)}, indent=2) if on
                  else "remove the clawdbridge.py hook entries from it yourself")
    keep ={k: os.environ[k] for k in ("CLAUDE_CONFIG_DIR", "CLAWDMETER_STATE", "CLAWDBRIDGE_NAME") if os.environ.get(k)}
    if on and keep:  # a custom Claude dir etc. must reach the service and the hooks, not just this shell
        ENV_FILE.write_text(json.dumps(keep, indent=1), encoding="utf-8")
    else:
        ENV_FILE.unlink(missing_ok=True)
    quit_running()
    how = (autostart_windows if WINDOWS else autostart_mac if MAC else autostart_linux)(on, [str(bg), str(script), "serve"])
    if not on:
        return print(f"Clawdmeter bridge removed: no longer starts at login; {hooks}.")
    try:
        ips = ", ".join(lan_ips())
    except ImportError:
        ips = "(see your network settings)"
    print(f"""
Clawdmeter bridge installed for '{HOST}'.
  - {hooks}
  - {how}
On the phone, open Clawdmeter and long-press the clock:
  Wi-Fi      this computer shows up by itself; or add one of: {ips} (port {PORT})
  Bluetooth  choose Bluetooth; this computer connects when it sees the phone
Restart any open Claude Code sessions so they pick up the hooks.{nat_note()}
Uninstall: "{py}" "{script}" uninstall""")


# ---- checks ---------------------------------------------------------------------------

def selftest():
    import tempfile
    global STATE_DIR
    now = 1_800_000_000
    u = parse_usage({"five_hour": {"utilization": 35.4, "resets_at": "2027-01-15T08:30:00Z"},
                     "seven_day": {"utilization": 20.6, "resets_at": "2027-01-19T00:00:00Z"}}, now)
    assert u["acct"] == "pro" and u["session"][0] == 35 and u["week"][0] == 21, u
    assert minutes_until(u["session"][1], now) == round((ts("2027-01-15T08:30:00Z") - now) / 60)
    idle = parse_usage({"five_hour": {"utilization": 0.0, "resets_at": None},
                        "seven_day": {"utilization": 40.0, "resets_at": "2027-01-14T15:00:00.006703+00:00"}}, now)
    assert idle["session"] == (0, None) and minutes_until(None, now) == -1
    assert parse_usage({"five_hour": None, "seven_day": {"utilization": 5, "resets_at": None}}, now)["acct"] == "week"

    a = Activity()
    a.ingest(f"{now - 1000} Stop s1", live=False)  # history: no event replayed
    assert a.latest is None
    assert a.ingest(f"{now} UserPromptSubmit s1 /home/u/.claude") == "b"  # back after >10 min
    assert a.ingest(f"{now + 5} UserPromptSubmit s2") == "p" and a.seq == 2
    for i in range(5):
        a.ingest(f"{now + 10 + i} PostToolUse s2")
    assert a.level(now + 20) == 2 and a.active_sessions(now + 20) == 2 and a.working(now + 20)
    assert a.ingest(f"{now + 30} Stop s2") == "s" and not a.working(now + 31)
    assert a.level(now + 100) == 0 and a.ingest("garbage") is None
    assert a.accounts["s1"] == "/home/u/.claude" and "s2" not in a.accounts
    assert a.working_sessions(now + 31) == 1  # s2 stopped; s1 still on its prompt
    a.ingest(f"{now + 40} Notification s1")      # waiting for the user is not working
    assert a.working_sessions(now + 41) == 0 and a.last_stop == now + 30
    a.ingest(f"{now + 50} PostToolUse s1")       # permission granted, tools run again
    assert a.working_sessions(now + 51) == 1 and a.working_sessions(now + 50 + 900) == 0  # interrupted: no Stop
    a.ingest(f"{now + 60} SessionEnd s1")        # exited mid-turn: neither working nor active (s2 still is)
    assert a.working_sessions(now + 61) == 0 and a.active_sessions(now + 61) == 1
    a.prune(now + 86400 + 45)
    assert set(a.sessions) == {"s1"} and set(a.last_kind) == {"s1"} and "s2" not in a.accounts
    a.ingest(f"{now + 90000} SessionStart s3")  # just opened: active, not working
    assert a.active_sessions(now + 90001) == 1 and a.working_sessions(now + 90001) == 0
    assert a.ingest(f"{now + 90002} UserPromptSubmit s3") == "b"  # still "back" after the break

    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        t = d / "projects" / "-x-my-proj"
        t.mkdir(parents=True)
        (t / "s9.jsonl").write_text('{"type":"user","cwd":"/x/my_proj \\"q\\""}\n{"cwd":"/x/my_proj \\"q\\"/sub"}\n')
        assert project_of("s9", d) == 'my_proj "q"' and project_of("nope", d) is None

        # hook -> the files the engine reads (upstream's format)
        STATE_DIR = d / "state"
        tp = str(d / "projects" / "-x" / "s9.jsonl")
        run_hook(["clear", "event"], json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": "s9",
                                                 "transcript_path": tp, "prompt": "secret"}), now)
        run_hook(["alert", "event"], json.dumps({"hook_event_name": "Notification", "session_id": "s9",
                                                 "notification_type": "idle_prompt", "message": "hi"}), now)
        run_hook(["clear"], json.dumps({"session_id": "../x"}))  # path tricks write nothing
        log_lines = (STATE_DIR / "events.log").read_text().splitlines()
        assert log_lines == [f"{now} UserPromptSubmit s9 {d}", f"{now} Notification s9"], log_lines
        assert "secret" not in (STATE_DIR / "events.log").read_text()
        assert json.loads((STATE_DIR / "attention").read_text()) == {"k": "idle", "m": "hi", "s": "s9"}
        assert sorted(p.name for p in STATE_DIR.glob("clear-*")) == ["clear-s9"]
        run_hook(["alert"], json.dumps({"hook_event_name": "PreToolUse", "session_id": "s1",
                                        "tool_input": {"questions": [{"question": "Which?"}]}}))
        assert json.loads((STATE_DIR / "attention").read_text())["k"] == "question"

        # install/uninstall rewrite only Clawdmeter's hook entries
        s = d / "settings.json"
        other = {"type": "command", "command": "prettier --write"}
        s.write_text(json.dumps({"model": "x", "hooks": {
            "PostToolUse": [{"matcher": "Edit", "hooks": [other]},
                            {"hooks": [{"type": "command", "command": "/opt/c/clawdmeter hook clear event"}]}],
            "Stop": [{"hooks": [other, {"type": "command", "command": '"C:/x/clawdmeter.exe" hook event'}]}]}}))
        set_hooks(s, '"/v/python" "/b/clawdbridge.py"')
        ino = s.stat().st_ino
        set_hooks(s, '"/v/python" "/b/clawdbridge.py"')  # idempotent, and not rewritten (read-only files filled in)
        assert s.stat().st_ino == ino
        h = json.loads(s.read_text())["hooks"]
        cmds = [x["command"] for g in sum(h.values(), []) for x in g["hooks"]]
        assert cmds.count("prettier --write") == 2 and not any("clawdmeter" in c for c in cmds), cmds
        assert sum("clawdbridge.py\" hook" in c for c in cmds) == len(HOOKS), cmds
        assert h["PreToolUse"] == [{"matcher": "AskUserQuestion", "hooks": [
            {"type": "command", "command": '"/v/python" "/b/clawdbridge.py" hook alert event'}]}]
        assert (d / "settings.json.bak-clawdbridge").exists()
        weird = d / "weird.json"
        for bad in ("{not json", '{"hooks": [1]}', '[1, 2]'):
            weird.write_text(bad)
            try:
                set_hooks(weird, '"/v/python" "/b/clawdbridge.py"')
                raise AssertionError("accepted " + bad)
            except SystemExit:
                assert weird.read_text() == bad  # untouched
        weird.write_text('{"hooks": {"Stop": [1, {"hooks": "x"}], "Odd": {"a": 1}}}')
        set_hooks(weird, '"/v/python" "/b/clawdbridge.py"')  # odd entries are kept as they were
        wh = json.loads(weird.read_text())["hooks"]
        assert wh["Odd"] == {"a": 1} and wh["Stop"][:2] == [1, {"hooks": "x"}] and len(wh["Stop"]) == 3

        try:  # a symlinked settings.json (stow, home-manager) stays a link; a UTF-8 BOM is fine
            real, link = d / "real.json", d / "link.json"
            real.write_text('\ufeff{"model": "y"}', encoding="utf-8")
            link.symlink_to(real)
            set_hooks(link, '"/v/python" "/b/clawdbridge.py"')
            assert link.is_symlink() and len(json.loads(real.read_text(encoding="utf-8-sig"))["hooks"]) == len(HOOKS)
        except (OSError, NotImplementedError):  # no symlinks (Windows without the privilege)
            pass

        # alerts: a clear and the next prompt in one tick; a prompt already answered in the same tick
        eng = Engine()
        eng.next_poll = float("inf")  # no network in a selftest
        att = STATE_DIR / "attention"

        def alert_at(t):
            run_hook(["alert"], json.dumps({"hook_event_name": "Notification", "session_id": "s7"}))
            os.utime(att, (t, t))

        def clear_at(t):
            run_hook(["clear"], json.dumps({"session_id": "s7"}))
            os.utime(STATE_DIR / "clear-s7", (t, t))

        t0 = time.time()
        alert_at(t0)
        eng.tick()
        assert eng.alert and eng.alert["at"] == t0
        clear_at(t0 + 1)   # tool 1 approved and ran...
        alert_at(t0 + 2)   # ...and tool 2 asks, all within one tick
        eng.tick()
        assert eng.alert and eng.alert["at"] == t0 + 2, eng.alert
        alert_at(t0 + 3)
        clear_at(t0 + 4)   # asked and answered within one tick: nothing to show
        eng.tick()
        assert eng.alert is None, eng.alert

        # a window that has reset reads 0 % until the next poll
        eng.usage = {"acct": "pro", "session": (80, time.time() - 5), "week": (50, time.time() + 3600)}
        eng.good_at = time.monotonic()
        p = eng.payload()
        assert (p["s"], p["sr"], p["w"]) == (0, -1, 50) and p["wr"] > 0 and "stale" not in p, p

    # Bluetooth writes: UTF-8 and <= 500 bytes, dropping projects (and err) before anything else
    body = ble_body({"s": 5, "err": "x" * 120, "pj": ["\u6f22" * 60] * 3})
    assert len(body) <= 500 and b'"err"' not in body and "\u6f22".encode() in body
    assert len(json.loads(body)["pj"]) == 2
    assert [retry_after(v) for v in ("120", "1.5", None, "Wed, 21 Oct 2015 07:28:00 GMT")] == [120, 1, 0, 0]

    with tempfile.TemporaryDirectory() as d:  # (the old checks continue below)
        d = Path(d)
        s = d / "settings.json"
        s.write_text(json.dumps({"model": "x", "hooks": {"PostToolUse": [{"matcher": "Edit", "hooks": [
            {"type": "command", "command": "prettier --write"}]}], "Stop": [{"hooks": [
            {"type": "command", "command": "prettier --write"}]}]}}))
        other = {"type": "command", "command": "prettier --write"}
        set_hooks(s, '"/v/python" "/b/clawdbridge.py"')
        set_hooks(s, None)
        after = json.loads(s.read_text())
        assert after["model"] == "x" and after["hooks"] == {"PostToolUse": [{"matcher": "Edit", "hooks": [other]}],
                                                            "Stop": [{"hooks": [other]}]}, after
    if not WINDOWS:  # crontab: ours added once, replaced, removed; the user's lines kept; unreadable = untouched
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "crontab").write_text(  # a fake crontab, so the real one is never touched
                '#!/bin/sh\nf="$(dirname "$0")/tab"\n[ -n "$CRON_DENY" ] && { echo denied >&2; exit 1; }\n'
                'case "$1" in -l) [ -f "$f" ] && exec cat "$f"; echo "no crontab for x" >&2; exit 1 ;;'
                ' -) cat >"$f" ;; esac\n')
            (Path(d) / "crontab").chmod(0o755)
            tab, env = Path(d) / "tab", dict(os.environ)
            os.environ["PATH"] = d + os.pathsep + env.get("PATH", "")
            try:
                assert set_cron("/b/run") and set_cron("/b/run")
                tab.write_text("0 * * * * mine\n" + tab.read_text())
                assert set_cron("/b/run2") and tab.read_text() == f"0 * * * * mine\n@reboot /b/run2 {CRON_TAG}\n"
                os.environ["CRON_DENY"] = "1"
                assert not set_cron(None) and CRON_TAG in tab.read_text()
                del os.environ["CRON_DENY"]
                assert set_cron(None) and tab.read_text() == "0 * * * * mine\n"
            finally:
                os.environ.clear()
                os.environ.update(env)
    from types import SimpleNamespace as NS
    assert is_phone(NS(service_uuids=[SVC.upper()], manufacturer_data={}))
    assert is_phone(NS(service_uuids=["00001800-0000-1000-8000-00805f9b34fb"], manufacturer_data={0xFFFF: b"clwd"}))
    assert not is_phone(NS(service_uuids=[], manufacturer_data={0xFFFF: b"nope", 0x75: b"clwd"}))
    print("selftest ok")


if __name__ == "__main__":
    cmd, rest = (sys.argv[1:2] or ["serve"])[0], sys.argv[2:]
    if cmd == "hook":
        hook(rest)
    elif cmd == "selftest":
        selftest()
    elif cmd in ("install", "uninstall"):
        install(cmd == "install")
    elif cmd == "serve" or cmd.startswith("--"):
        args = sys.argv[1:]
        serve(int(args[args.index("--port") + 1]) if "--port" in args else PORT, ble="--no-ble" not in args)
    else:
        sys.exit(__doc__)
