#!/usr/bin/env python3
"""Local desktop-account bridge for 红果免费短剧 (Mac beta).

The beta UI implements an OAuth device-code flow but ships with no auth
service configured. This bridge implements that contract on loopback and
performs the real 红果 web QR login, so the app can show a scannable QR code.

Run:
    python bridge.py [--port 8787]

Then launch the app with:
    HONGGUO_DESKTOP_AUTH_BASE_URL=http://127.0.0.1:8787
    HONGGUO_DESKTOP_AUTH_CLIENT_ID=hongguo-local
    HONGGUO_DESKTOP_PLAYBACK_PATH=/playback

Only loopback binding is supported. A verified account is stored with 0600
permissions under ~/Library/Application Support/HongguoBridge/account.json so
later app launches can sign in without scanning again; `logout` removes it.
"""
from __future__ import annotations

import argparse
import html
import http.client
import io
import json
import os
import re
import secrets
import shlex
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import segno

HERE = Path(__file__).resolve().parent
VENDOR = HERE / "vendor"
sys.path.insert(0, str(VENDOR))
from phone_account_qr import QRLoginSession, QRUnavailable  # noqa: E402

CLIENT_ID = "hongguo-local"
SCOPE = "openid profile offline_access"
TOKEN_TTL = 24 * 3600

SESSIONS: dict[str, dict] = {}
TOKENS: dict[str, dict] = {}
LOCK = threading.RLock()

BACKEND: dict = {"checked": 0.0, "port": 0, "key": ""}

STATE_DIR = Path.home() / "Library" / "Application Support" / "HongguoBridge"
ACCOUNT_FILE = STATE_DIR / "account.json"
LAB_PAGE = STATE_DIR / "app" / "lab.html"
LAUNCHER = STATE_DIR / "bin" / "launch.sh"
LIBRARY_DB = Path.home() / "Library" / "Application Support" / "cn.guoban.desktop-companion" / "guoban.sqlite3"
ACCOUNT: dict | None = None
APP_BIN = "/Applications/红果免费短剧.app/Contents/MacOS/hongguo-desktop-companion"
AUTOLOGIN_JS = STATE_DIR / "bin" / "autologin.js"


def app_pid() -> int:
    """PID of the running 红果 app, or 0. The [n] keeps the pattern from
    matching the pgrep command line itself."""
    try:
        out = subprocess.run(["pgrep", "-f", "hongguo-desktop-companio[n]$"],
                             capture_output=True, text=True, timeout=5).stdout.split()
    except Exception:
        return 0
    return int(out[0]) if out else 0


_AUTOLOGINED: set[int] = set()


def autologin_current(reason: str) -> bool:
    """Restore the saved account in the running app (once per app process).
    The app itself does not persist login, so every launch — Dock, Finder,
    launch.sh — needs this; the bridge is always up under launchd and owns it."""
    pid = app_pid()
    if not pid or pid in _AUTOLOGINED:
        return False
    _AUTOLOGINED.add(pid)

    def run():
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            with open(STATE_DIR / "autologin.log", "ab") as log:
                subprocess.run(["/usr/bin/osascript", "-l", "JavaScript", str(AUTOLOGIN_JS)],
                               stdout=log, stderr=log, timeout=120)
            print(f"[bridge {stamp}] autologin finished ({reason}, pid {pid})", flush=True)
        except Exception as exc:
            print(f"[bridge {stamp}] autologin failed ({reason}): {type(exc).__name__}", flush=True)

    threading.Thread(target=run, daemon=True).start()
    return True


def watch_app():
    """Auto-login whenever a new app process appears (e.g. launched from the
    Dock, which does not run launch.sh)."""
    seen = app_pid()  # already running at bridge start: leave it alone
    while True:
        time.sleep(3)
        pid = app_pid()
        if pid != seen:
            seen = pid
            if not pid:
                _AUTOLOGINED.clear()
            else:
                autologin_current("new app process")


def load_account() -> dict | None:
    try:
        value = json.loads(ACCOUNT_FILE.read_text(encoding="utf-8"))
        uid = str(value.get("uid") or "")
        name = str(value.get("name") or "")
        cookies = value.get("cookies")
        if uid and name and isinstance(cookies, list):
            return {"uid": uid, "name": name, "cookies": cookies}
    except Exception:
        pass
    return None


def save_account(account: dict) -> None:
    global ACCOUNT
    ACCOUNT = account
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        temporary = ACCOUNT_FILE.with_suffix(".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(account, stream, ensure_ascii=False)
        os.replace(temporary, ACCOUNT_FILE)
    except Exception:
        pass


def json_bytes(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


def form_body(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("content-length") or 0)
    raw = handler.rfile.read(length) if length else b""
    out: dict[str, str] = {}
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(handler.path).query)
    for key, values in query.items():
        out[key] = values[-1]
    kind = (handler.headers.get("content-type") or "").split(";")[0].strip().lower()
    if not raw:
        return out
    if kind == "application/json":
        try:
            value = json.loads(raw)
        except ValueError:
            return out
        if isinstance(value, dict):
            out.update({str(k): str(v) for k, v in value.items()})
        return out
    try:
        parsed = urllib.parse.parse_qs(raw.decode("utf-8", "replace"))
    except Exception:
        return out
    for key, values in parsed.items():
        out[key] = values[-1]
    return out


def sanitize(params: dict) -> dict:
    hidden = {"token", "access_token", "authorization", "cookie", "sessionid", "device_code"}
    return {k: ("***" if k.lower() in hidden else v) for k, v in params.items()}


def _qr_png(challenge) -> bytes:
    buffer = io.BytesIO()
    segno.make(challenge.text, error="m").save(buffer, kind="png", scale=6, border=2)
    return buffer.getvalue()


def create_session() -> tuple[dict, str]:
    if ACCOUNT:
        session = {
            "qr": None,
            "png": b"",
            "created": time.time(),
            "expires": time.time() + 3600,
            "token": "",
            "account": dict(ACCOUNT),
            "lock": threading.Lock(),
            "restored": True,
        }
        device_code = secrets.token_hex(16)
        with LOCK:
            SESSIONS[device_code] = session
        return session, device_code
    qr = QRLoginSession()
    challenge = qr.start()
    png = _qr_png(challenge)
    session = {
        "qr": qr,
        "qr_id": challenge.id,
        "png": png,
        "created": time.time(),
        "expires": challenge.expires_at,
        "generation": 0,
        "token": "",
        "account": None,
        "lock": threading.Lock(),
        "restored": False,
    }
    with LOCK:
        SESSIONS[challenge.id] = session
    return session, challenge.id


def _refresh_qr(session: dict) -> None:
    qr = QRLoginSession()
    challenge = qr.start()
    session["qr"] = qr
    session["qr_id"] = challenge.id
    session["png"] = _qr_png(challenge)
    session["expires"] = challenge.expires_at
    session["generation"] = int(session.get("generation", 0)) + 1


def settle(session: dict, device_code: str) -> dict:
    """Advance one QR session and return the token-endpoint payload."""
    with session["lock"]:
        return _settle_locked(session, device_code)


def _settle_locked(session: dict, device_code: str) -> dict:
    qr: QRLoginSession = session["qr"]
    if session["account"]:
        account = session["account"]
        if not session["token"]:
            token = secrets.token_urlsafe(32)
            session["token"] = token
            with LOCK:
                TOKENS[token] = {
                    "created": time.time(),
                    "device_code": device_code,
                    "account": dict(account),
                }
        return {
            "access_token": session["token"],
            "accessToken": session["token"],
            "token_type": "Bearer",
            "scope": SCOPE,
            "expires_in": TOKEN_TTL,
            "expires_at": int(time.time()) + TOKEN_TTL,
            "status": "authenticated",
            "display_name": account["name"],
            "user": {"name": account["name"], "avatar_url": ""},
        }
    try:
        qr.poll(session.get("qr_id", device_code))
        if qr.phase == "confirmation-received":
            qr.exchange(session.get("qr_id", device_code))
        if qr.phase == "credentials-received":
            qr.verify(session.get("qr_id", device_code))
    except QRUnavailable:
        pass

    if qr.phase == "expired":
        try:
            _refresh_qr(session)
            qr = session["qr"]
        except Exception:
            pass

    phase = qr.phase
    account = qr.account
    if account is not None and not session["account"]:
        credentials = getattr(account, "credentials", None)
        cookies = list(getattr(credentials, "cookies", ()) or ())
        session["account"] = {"uid": account.uid, "name": account.name, "cookies": cookies}
        save_account(dict(session["account"]))
        token = secrets.token_urlsafe(32)
        session["token"] = token
        with LOCK:
            TOKENS[token] = {
                "created": time.time(),
                "device_code": device_code,
                "account": session["account"],
            }
        return {
            "access_token": token,
            "accessToken": token,
            "token_type": "Bearer",
            "scope": SCOPE,
            "expires_in": TOKEN_TTL,
            "expires_at": int(time.time()) + TOKEN_TTL,
            "status": "authenticated",
            "display_name": account.name,
            "user": {"name": account.name, "avatar_url": ""},
        }
    if phase in ("expired", "cancelled"):
        return {"status": "expired", "error": "expired_token"}
    if phase in ("error", "denied"):
        return {"status": "denied", "error": "access_denied"}
    return {"status": "pending", "error": "authorization_pending", "interval": 2}


def backend_endpoint() -> tuple[int, str]:
    """Discover the app's bundled guest backend (loopback port + session key)."""
    now = time.time()
    if now - BACKEND["checked"] < 3 and BACKEND["port"]:
        return BACKEND["port"], BACKEND["key"]
    port, key = 0, ""
    try:
        out = subprocess.run(
            ["ps", "eww", "-ax"], capture_output=True, text=True, timeout=5
        ).stdout
        for line in out.splitlines():
            if "desktop_bootstrap.py" not in line or "Application Support" not in line:
                continue
            pid = line.split(None, 1)[0]
            for token in line.split():
                if token.startswith("HONGGUO_SESSION_API_KEY="):
                    key = token.split("=", 1)[1]
            if not pid.isdigit():
                continue
            if int(pid) != BACKEND.get("pid"):
                BACKEND["pid"] = int(pid)
                BACKEND["boot"] = now  # app restart: backend is warming up
            listen = subprocess.run(
                ["lsof", "-nP", "-a", "-p", pid, "-iTCP", "-sTCP:LISTEN"],
                capture_output=True, text=True, timeout=5,
            ).stdout
            for row in listen.splitlines()[1:]:
                parts = row.split()
                if len(parts) >= 9 and ":" in parts[8]:
                    candidate = parts[8].rsplit(":", 1)[1]
                    if candidate.isdigit():
                        port = int(candidate)
                        break
            if port:
                break
    except Exception:
        pass
    BACKEND.update(checked=now, port=port, key=key)
    return port, key


# Next-episode prefetch. The backend /stream endpoint downloads + decrypts +
# (with HONGGUO_SESSION_API_KEY set) fully transcodes an episode inline before
# answering, so a cold next episode often outlasts the player's auto-advance
# wait. Warming the next episode in the background with a 1-byte Range request
# makes the player's switch instant; the backend's _dec_lock dedups overlaps.
_WARMED: set[str] = set()
_WARMED_LOCK = threading.Lock()


def _warm_stream(base: str, key: str, vid: str) -> None:
    with _WARMED_LOCK:
        if vid in _WARMED:
            return
        _WARMED.add(vid)

    def warm_once() -> None:
        url = base + "/stream?vid=" + urllib.parse.quote(str(vid)) + "&api_key=" + urllib.parse.quote(key)
        request = urllib.request.Request(url, headers={"Range": "bytes=0-0"})
        with urllib.request.urlopen(request, timeout=420) as response:
            response.read(16)

    def warm_worker() -> None:
        for attempt in (0, 1):
            try:
                warm_once()
                return
            except Exception:
                if attempt == 0:
                    time.sleep(30)
        # Both attempts failed; allow a later playback request to retry.
        with _WARMED_LOCK:
            _WARMED.discard(vid)

    threading.Thread(target=warm_worker, daemon=True).start()


# Bound the backend's stream cache. Each episode is stored as {vid}_best.mp4
# (decrypted) plus optional transcodes; the backend never evicts, so the
# directory grows without limit (8 GB after two weeks of use). atime is not
# maintained on this filesystem, so resolve_playback touches the files of the
# episode being watched and eviction goes by mtime, whole episodes at a time.
STREAM_CACHE_DIR = (Path.home() / "Library" / "Application Support"
                    / "cn.guoban.desktop-companion" / "stream-cache")
STREAM_CACHE_MAX_GB = float(os.environ.get("HONGGUO_STREAM_CACHE_MAX_GB", "8"))
STREAM_CACHE_MAX_AGE = 30 * 86400
STREAM_CACHE_INTERVAL = 1800
STREAM_CACHE_PROTECT = 1800  # never evict episodes used in the last 30 min

# Playback-v5 state lives next to the stream cache in the backend data dir.
# Both registrations are one-shot by design: a network failure during the
# anonymous device registration or an interrupted SDK save leaves a durable
# marker that blocks playback until it is cleared by hand. /v5-reset clears
# exactly those markers so the next playback can re-register; the AES key
# (playback-v5.key) and the verified SDK materials (playback-sdk/) are kept.
V5_DATA_DIR = STREAM_CACHE_DIR.parent
V5_STATE_FILES = (
    "playback-spare.dpapi", "playback-spare.attempt.json",
    "playback-spare.registration.pending", "playback-spare.registration.lock",
    "playback-spare.previous-attempt.json", "playback-spare.lock",
    "playback-sdk-v5.dpapi", "playback-sdk-v5-attempt.json",
    "playback-sdk-v5-pending.dpapi", "playback-sdk-v5-pending.tmp",
    "playback-sdk-v5-continue.json", "playback-sdk-v5.lock",
    "playback-sdk-v5.tmp", "playback-sdk-v5-recovery.dpapi",
    "playback-sdk-v5-recovery.tmp", "playback-v5-diagnostic.json",
)


def reset_v5_state() -> list:
    removed = []
    for name in V5_STATE_FILES:
        path = V5_DATA_DIR / name
        try:
            if path.is_symlink() or not path.is_file():
                continue
            path.unlink()
            removed.append(name)
        except OSError:
            pass
    return removed


def spawn_app_restart() -> bool:
    script = ("sleep 0.4; pkill -f hongguo-desktop-companion >/dev/null 2>&1; "
              "sleep 2; exec " + shlex.quote(str(LAUNCHER)) + " >/dev/null 2>&1")
    try:
        subprocess.Popen(["/bin/sh", "-c", script], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
        return True
    except Exception:
        return False


def stream_cache_files(vid: str):
    try:
        return [p for p in STREAM_CACHE_DIR.glob(str(vid) + "_*") if p.is_file()]
    except Exception:
        return []


def touch_stream_cache(vid: str) -> None:
    for path in stream_cache_files(vid):
        try:
            os.utime(path, None)
        except OSError:
            pass


def trim_stream_cache() -> dict:
    """Evict least-recently-used whole episodes until the cache is under
    STREAM_CACHE_MAX_GB; also drop entries older than STREAM_CACHE_MAX_AGE
    and stale .partial downloads."""
    now = time.time()
    removed, freed = 0, 0
    try:
        entries = [p for p in STREAM_CACHE_DIR.iterdir() if p.is_file()]
    except Exception:
        return {"removed": 0, "freed": 0, "total": 0}

    for path in entries:
        try:
            if path.name.endswith(".partial") and now - path.stat().st_mtime > 86400:
                freed += path.stat().st_size
                path.unlink()
                removed += 1
        except OSError:
            pass

    episodes: dict[str, dict] = {}
    for path in entries:
        match = re.match(r"^(\d+)_", path.name)
        if not match:
            continue
        try:
            st = path.stat()
        except OSError:
            continue
        entry = episodes.setdefault(match.group(1), {"size": 0, "used": 0.0, "files": []})
        entry["size"] += st.st_size
        entry["used"] = max(entry["used"], st.st_atime, st.st_mtime)
        entry["files"].append(path)

    total = sum(e["size"] for e in episodes.values())
    cap = STREAM_CACHE_MAX_GB * 1e9
    for vid, entry in sorted(episodes.items(), key=lambda kv: kv[1]["used"]):
        too_old = now - entry["used"] > STREAM_CACHE_MAX_AGE
        if total <= cap and not too_old:
            break
        if now - entry["used"] < STREAM_CACHE_PROTECT:
            continue
        for path in entry["files"]:
            try:
                path.unlink()
            except OSError:
                continue
        total -= entry["size"]
        freed += entry["size"]
        removed += 1
        with _WARMED_LOCK:
            _WARMED.discard(vid)
    return {"removed": removed, "freed": freed, "total": total}


def trim_stream_cache_loop():
    time.sleep(60)
    while True:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            stats = trim_stream_cache()
            if stats["removed"]:
                print(f"[bridge {stamp}] stream cache: removed {stats['removed']} episode(s), "
                      f"freed {stats['freed'] / 1e9:.1f} GB, now {stats['total'] / 1e9:.1f} GB "
                      f"(cap {STREAM_CACHE_MAX_GB:g} GB)", flush=True)
        except Exception as exc:
            print(f"[bridge {stamp}] stream cache trim failed: {type(exc).__name__}", flush=True)
        time.sleep(STREAM_CACHE_INTERVAL)


def resolve_playback(params: dict) -> dict:
    series_id = params.get("series_id") or params.get("seriesId") or params.get("id") or ""
    episode = params.get("episode") or params.get("ep") or params.get("index") or "1"
    vid = params.get("vid") or ""
    if not series_id and not vid:
        return {"error": "missing series_id"}
    series_id = resolve_rank_index(str(series_id), str(episode))
    if not backend_endpoint()[0]:
        return {"error": "backend unavailable"}

    def get_json(target_path, query, timeout=60):
        # Resolve the endpoint per call: the app (and its backend) can
        # restart mid-request, which leaves the 3s endpoint cache pointing
        # at a dead port.
        port, key = backend_endpoint()
        if not port:
            raise ConnectionError("backend unavailable")
        query = dict(query)
        query["api_key"] = key
        url = f"http://127.0.0.1:{port}" + target_path + "?" + urllib.parse.urlencode(query)
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read(4 * 1024 * 1024))

    if vid:
        try:
            value = get_json("/video_url", {"vid": str(vid)})
        except Exception as exc:
            detail = ""
            if isinstance(exc, urllib.error.HTTPError):
                try:
                    detail = exc.read(4096).decode("utf-8", "replace")[:400]
                except Exception:
                    detail = ""
            return {"error": f"upstream: {type(exc).__name__}", "detail": detail}
        stream = value.get("url") or ""
        return {
            "url": stream, "stream_url": stream, "playback_url": stream,
            "episode": 1, "total": 1, "episode_count": 1, "title": "",
        }

    # Episode count comes from /episodes; /play with ep=1 only ever returns that
    # one episode and makes the app believe the series is a single episode.
    listing = {}
    for _ in range(2):
        try:
            listing = get_json("/episodes", {"series_id": str(series_id)}, timeout=30)
            break
        except Exception:
            if time.time() - BACKEND.get("boot", 0) >= 45:
                break
            BACKEND["checked"] = 0
            time.sleep(3)
    episodes = listing.get("episodes") or []
    chosen = None
    chosen_pos = -1
    want = str(episode)
    for pos, item in enumerate(episodes):
        if str(item.get("index")) == want:
            chosen = item
            chosen_pos = pos
            break
    if chosen is None and episodes:
        chosen = episodes[0]
        chosen_pos = 0
    want_index = chosen.get("index") if chosen else (episode if str(episode).isdigit() else 1)
    # The app's first playback after a restart can land while the freshly
    # spawned backend is still warming up, and the endpoint cache may still
    # point at the port of the killed previous backend. Retry with fresh
    # endpoint discovery until the boot window closes; outside the window a
    # transport failure still gets one rediscovery attempt.
    value = None
    exc = None
    deadline = time.time() + 25
    while True:
        try:
            value = get_json("/play", {"series_id": str(series_id), "ep": str(want_index)})
            break
        except Exception as error:
            exc = error
            booting = time.time() - BACKEND.get("boot", 0) < 45
            transport = not isinstance(error, urllib.error.HTTPError)
            if time.time() >= deadline or not (booting or transport):
                break
            BACKEND["checked"] = 0  # force rediscovery of the (new) backend
            time.sleep(3)
    if value is None:
        assert exc is not None
        detail = ""
        if isinstance(exc, urllib.error.HTTPError):
            try:
                detail = exc.read(4096).decode("utf-8", "replace")[:400]
            except Exception:
                detail = ""
        return {"error": f"upstream: {type(exc).__name__}", "detail": detail}
    played = (value.get("episodes") or []) or [{}]
    current = played[0]
    stream_path = str(current.get("stream_url") or "")
    if not stream_path:
        return {"error": "episode unavailable", "title": value.get("title") or ""}
    port, key = backend_endpoint()
    base = f"http://127.0.0.1:{port}"
    stream = base + stream_path + "&api_key=" + urllib.parse.quote(key)
    total = len(episodes) or len(played)
    next_item = episodes[chosen_pos + 1] if 0 <= chosen_pos + 1 < len(episodes) else None
    if next_item and next_item.get("vid"):
        _warm_stream(base, key, str(next_item["vid"]))
    played_vid = str(current.get("vid") or "")
    if not played_vid.isdigit():
        found = re.search(r"[?&]vid=(\d+)", stream_path)
        played_vid = found.group(1) if found else ""
    if played_vid:
        touch_stream_cache(played_vid)
    return {
        "url": stream,
        "stream_url": stream,
        "playback_url": stream,
        "episode": current.get("index") or want_index,
        "total": total,
        "episode_count": total,
        "title": current.get("title") or (chosen or {}).get("title") or value.get("title") or "",
        "series_title": value.get("title") or listing.get("meta", {}).get("title") or "",
        "duration": current.get("duration") or (chosen or {}).get("duration"),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "HongguoLocalAuth/0.1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        sys.stderr.write(f"[bridge {stamp}] " + (fmt % args) + "\n")

    def send_json(self, value, status=200):
        body = json_bytes(value)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_bytes(self, body: bytes, kind="application/octet-stream", status=200):
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def proxy_backend(self, target_path: str, query: str, method: str = "GET"):
        """Forward a request to the app's bundled backend, injecting its session key.

        The key rotates on every app start; resolving it here keeps the local UI
        at a stable address and lets an open page survive an app restart.
        """
        port, key = backend_endpoint()
        if not port:
            return self.send_json({"error": "backend_unavailable",
                                   "detail": "红果 App 的本地后端尚未启动"}, 503)
        pairs = [(name, value) for name, value in urllib.parse.parse_qsl(query, keep_blank_values=True)
                 if name != "api_key"]
        pairs.append(("api_key", key))
        target = target_path + "?" + urllib.parse.urlencode(pairs)
        headers = {}
        requested_range = self.headers.get("Range")
        if requested_range:
            headers["Range"] = requested_range
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=300)
        try:
            connection.request(method, target, headers=headers)
            response = connection.getresponse()
        except Exception as exc:
            connection.close()
            return self.send_json({"error": "backend_unavailable",
                                   "detail": type(exc).__name__}, 502)
        self.send_response(response.status)
        for name in ("Content-Type", "Content-Length", "Content-Range",
                     "Accept-Ranges", "Content-Disposition", "Last-Modified", "ETag"):
            value = response.getheader(name)
            if value is not None:
                self.send_header(name, value)
        if response.getheader("Content-Length") is None:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if method != "HEAD":
            try:
                while True:
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True
        connection.close()

    def local_emulate(self, path: str, params: dict):
        """Serve content for the app's redirected rank/detail/search requests."""
        # The binary URL patch rewrites the website base to the 25-char alias
        # http://127.0.0.1:8787/abc, so base + "/rank/hot-drama" etc. arrive
        # here; normalize onto the /ab/ prefixes used by the emulators below.
        if path.startswith("/abc/"):
            path = "/ab/" + path[len("/abc/"):]
        if path.startswith("/novelfm"):
            return self.send_json(novelfm_payload(params))
        if path.startswith("/ab/rank/"):
            slug = path[len("/ab/rank/"):].split("/")[0]
            body = rank_page(slug)
            record_rank_served(slug, body)
            return self.send_bytes(body, "text/html; charset=utf-8")
        if path.startswith("/ab/detail"):
            return self.send_bytes(detail_page(params.get("series_id") or ""),
                                   "text/html; charset=utf-8")
        if path.startswith("/ab/search"):
            return self.send_bytes(web_search_page(params), "text/html; charset=utf-8")
        return self.send_bytes(generic_page(), "text/html; charset=utf-8")

    def add_favorite(self):
        """Insert one series into the app's 收藏 table (read when the app starts)."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return self.send_json({"error": "invalid_json"}, 400)
        if not isinstance(payload, dict):
            return self.send_json({"error": "invalid_payload"}, 400)
        series_id = str(payload.get("seriesId") or payload.get("series_id") or "").strip()
        if not re.fullmatch(r"[0-9]{6,25}", series_id):
            return self.send_json({"error": "invalid_series_id"}, 400)
        tags = payload.get("tags") if isinstance(payload.get("tags"), list) else []
        actors = payload.get("actors") if isinstance(payload.get("actors"), list) else []
        try:
            episode_count = int(payload.get("episodeCount") or 0)
        except (TypeError, ValueError):
            episode_count = 0
        record = {
            "seriesId": series_id,
            "title": str(payload.get("title") or "")[:300],
            "cover": str(payload.get("cover") or "")[:2000],
            "intro": str(payload.get("intro") or "")[:4000],
            "actors": [str(item)[:80] for item in actors[:20]],
            "tags": [str(item)[:40] for item in tags[:12]],
            "episodeCount": max(0, episode_count),
            "webAccessibleEpisodes": 0,
            "hotText": str(payload.get("hotText") or "")[:200],
        }
        try:
            connection = sqlite3.connect(str(LIBRARY_DB), timeout=10)
            connection.execute("PRAGMA busy_timeout=8000")
            connection.execute(
                "INSERT INTO favorites(series_id, series_json, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(series_id) DO UPDATE SET series_json=excluded.series_json, "
                "updated_at=excluded.updated_at",
                (series_id, json.dumps(record, ensure_ascii=False), int(time.time())),
            )
            connection.commit()
            connection.close()
        except Exception as exc:
            return self.send_json({"error": "favorite_write_failed",
                                   "detail": type(exc).__name__}, 500)
        return self.send_json({"ok": True, "seriesId": series_id, "restart_required": True})

    def open_app(self):
        try:
            subprocess.Popen([str(LAUNCHER)], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        except Exception as exc:
            return self.send_json({"error": type(exc).__name__}, 500)
        return self.send_json({"ok": True})

    def restart_app(self):
        if not spawn_app_restart():
            return self.send_json({"error": "spawn failed"}, 500)
        return self.send_json({"ok": True})

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def route(self):
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        if path in ("/lab", "/lab/"):
            try:
                body = LAB_PAGE.read_bytes()
            except OSError:
                return self.send_bytes("<h1>本地搜片页未安装</h1>".encode(), "text/html; charset=utf-8", 404)
            return self.send_bytes(body, "text/html; charset=utf-8")
        if path == "/app-ready":
            port, _ = backend_endpoint()
            return self.send_json({"ready": bool(port), "port": port})
        if path == "/favorite" and self.command == "POST":
            return self.add_favorite()
        params = form_body(self)
        if path.startswith("/novelfm") or path.startswith("/ab/") or path.startswith("/abc/"):
            return self.local_emulate(path, params)
        if path == "/open-app":
            return self.open_app()
        if path == "/restart-app":
            return self.restart_app()
        if path == "/autologin":
            return self.send_json({"ok": autologin_current("api")})
        if path == "/v5-reset" and self.command == "POST":
            removed = reset_v5_state()
            payload = {"ok": True, "removed": removed}
            if str(params.get("restart") or "").lower() in ("1", "true", "yes"):
                payload["restarting"] = spawn_app_restart()
            else:
                payload["note"] = "restart the app to reload the backend (POST /v5-reset?restart=1)"
            return self.send_json(payload)
        if path == "/app" or path.startswith("/app/"):
            return self.proxy_backend(path[4:] or "/", parsed.query,
                                      "HEAD" if self.command == "HEAD" else "GET")
        if path in ("/oauth/device/code", "/device/code"):
            session, device_code = create_session()
            base = f"http://127.0.0.1:{self.server.server_port}"
            return self.send_json({
                "device_code": device_code,
                "user_code": device_code[:8].upper(),
                "verification_uri": f"{base}/login/{device_code}",
                "verification_uri_complete": f"{base}/login/{device_code}",
                "login_url": f"{base}/login/{device_code}",
                "qr_image": f"{base}/qr/{device_code}.png",
                "qr_code": f"{base}/qr/{device_code}.png",
                "qr_url": f"{base}/qr/{device_code}.png",
                "interval": 2,
                "expires_in": max(1, int(session["expires"] - time.time())),
                "status": "pending",
            })
        if path in ("/oauth/token", "/token"):
            device_code = params.get("device_code") or params.get("deviceCode") or ""
            with LOCK:
                session = SESSIONS.get(device_code)
            if session is None:
                return self.send_json({"status": "expired", "error": "expired_token"})
            payload = settle(session, device_code)
            self.send_json(payload)
            if payload.get("status") == "authenticated":
                sys.stderr.write("[bridge] login ok: " + payload.get("display_name", "") + "\n")
            return
        if path.startswith("/qr/") and path.endswith(".png"):
            device_code = path[4:-4]
            with LOCK:
                session = SESSIONS.get(device_code)
            if session is None:
                return self.send_bytes(b"", "image/png", 404)
            return self.send_bytes(session["png"], "image/png")
        if path.startswith("/login/"):
            device_code = path.split("/")[-1]
            with LOCK:
                session = SESSIONS.get(device_code)
            if session is None:
                return self.send_bytes("<h1>二维码已失效</h1>".encode(), "text/html; charset=utf-8", 404)
            base = f"http://127.0.0.1:{self.server.server_port}"
            html = f"""<!doctype html><meta charset="utf-8">
<title>红果扫码登录</title>
<style>body{{font-family:-apple-system,"PingFang SC",sans-serif;background:#151517;color:#f4f4f5;
display:flex;flex-direction:column;align-items:center;justify-content:center;min-height:100vh;margin:0}}
img{{width:280px;height:280px;background:#fff;border-radius:16px;padding:12px}}
p{{color:#a1a1aa}}.ok{{color:#4ade80}}</style>
<img id="q" src="{base}/qr/{device_code}.png" alt="二维码">
<p id="s">打开红果 App，扫码并确认登录</p>
<script>
const s=document.getElementById('s'),q=document.getElementById('q');
let g=-1;
setInterval(async()=>{{
  try{{const r=await fetch('{base}/login-status/{device_code}');const j=await r.json();
  if(typeof j.qr_generation==='number'&&j.qr_generation!==g){{g=j.qr_generation;q.src='{base}/qr/{device_code}.png?g='+g;}}
  if(j.status==='authenticated'){{s.className='ok';s.textContent='已登录：'+j.display_name;}}
  else if(j.status==='pending'){{s.textContent='等待手机确认…';}}
  else if(j.status!=='pending'){{s.textContent='二维码已过期，正在刷新…';}}}}catch(e){{}}
}},2000);
</script>"""
            return self.send_bytes(html.encode(), "text/html; charset=utf-8")
        if path.startswith("/login-status/"):
            device_code = path.split("/")[-1]
            with LOCK:
                session = SESSIONS.get(device_code)
            if session is None:
                return self.send_json({"status": "expired"})
            value = settle(session, device_code)
            value["qr_generation"] = int(session.get("generation", 0))
            return self.send_json(value)
        if path in ("/playback", "/play"):
            value = resolve_playback(params)
            sys.stderr.write(
                "[bridge] playback params=" + json.dumps(sanitize(params), ensure_ascii=False)
                + " -> " + json.dumps(
                    {k: v for k, v in value.items() if k in ("error", "title", "episode", "total")},
                    ensure_ascii=False,
                ) + "\n"
            )
            return self.send_json(value, 200 if "error" not in value else 502)
        if path in ("/logout", "/revoke"):
            global ACCOUNT
            token = params.get("token") or params.get("access_token") or ""
            with LOCK:
                TOKENS.pop(token, None)
                ACCOUNT = None
            try:
                ACCOUNT_FILE.unlink()
            except FileNotFoundError:
                pass
            return self.send_json({"status": "ok"})
        if path in ("/health", "/"):
            return self.send_json({"status": "ok", "service": "hongguo-local-auth"})
        return self.send_json({"error": "not_found"}, 404)

    def do_GET(self):
        try:
            self.route()
        except Exception as exc:
            self.send_json({"error": type(exc).__name__}, 500)

    def do_POST(self):
        try:
            self.route()
        except Exception as exc:
            self.send_json({"error": type(exc).__name__}, 500)

    def do_HEAD(self):
        try:
            self.route()
        except Exception as exc:
            self.send_json({"error": type(exc).__name__}, 500)


def backend_get(path_qs: str, timeout=40):
    port, key = backend_endpoint()
    if not port:
        return None
    joiner = "&" if "?" in path_qs else "?"
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request("GET", path_qs + joiner + urllib.parse.urlencode({"api_key": key}))
        response = connection.getresponse()
        return json.loads(response.read())
    except Exception:
        return None
    finally:
        connection.close()


def novelfm_payload(params: dict) -> dict:
    query = ""
    for name in ("query", "keyword", "word", "q", "search_key", "searchKey"):
        value = params.get(name)
        if value:
            query = str(value).strip()
            break
    rows = []
    if query:
        payload = backend_get("/search/page?" + urllib.parse.urlencode({"q": query}))
        rows = (payload or {}).get("results") or []
    items = []
    for row in rows[:20]:
        sid = str(row.get("series_id") or row.get("seriesId") or "")
        title = row.get("title") or ""
        cover = row.get("cover") or ""
        count = row.get("episode_cnt") or row.get("episodeCount") or 0
        items.append({
            "book_id": sid, "bookId": sid,
            "book_name": title, "bookName": title, "book_title": title,
            "book_identity": sid, "bookIdentity": sid,
            "thumb_pic": cover, "thumb_url": cover, "cover": cover,
            "cover_url": cover, "series_cover": cover,
            "chapter_number": count, "serial_count": count,
            "episode_count": count, "episode_cnt": count,
            "series_id": sid,
        })
    page = {
        "searchList": items, "results": items, "books": items,
        "page_size": len(items), "pageSize": len(items),
        "offset": 0, "session": "", "cursor": "", "end_marker": "",
        "has_more": False, "hasMore": False, "total": len(items),
    }
    value = {"code": 0, "status": 0, "message": "success", "msg": "success"}
    value.update(page)
    value["data"] = dict(page)
    return value


# The app's four compiled-in rank slugs -> backend boards, page title, and a
# fallback board. The two browse boards hit the category/landpage API, which
# upstream now signature-checks: they only return data when the backend runs
# with HG_SIGN_LIST=1 (launch.sh / LSEnvironment set it). If that ever fails,
# the fallback /rank board keeps the tab populated.
RANK_PAGES = {
    "hot-drama": ("hot", "热门短剧榜", None),
    "hot-comic-drama": ("recommend", "漫剧推荐榜", None),
    "hot-real-drama": ("browse:short_play", "热门真人短剧榜", "new"),
    "hot-ai-drama": ("browse:ai_series", "热门AI短剧榜", "recommend"),
}


def backend_get_retry(path_qs: str, timeout=40, attempts=2, delay=1.5):
    """backend_get with a short retry. The app's bundled backend restarts
    together with the app, so requests that land in the first seconds after a
    relaunch hit a not-yet-listening backend; one quick retry rescues most."""
    for attempt in range(attempts):
        payload = backend_get(path_qs, timeout=timeout)
        if payload:
            return payload
        if attempt + 1 < attempts:
            time.sleep(delay)
    return None


RANK_CACHE_DIR = STATE_DIR / "cache"
RANK_TTL = 600  # seconds a cached rank page is served as-is before a refresh


RANK_INDEX_RE = re.compile(r'href="/detail\?series_id=(\d+)"[^>]*aria-label="播放第 1 集 ([^"]*)"')
RANK_SERVED = {"slug": "", "time": 0.0, "cards": []}
RANK_SERVED_LOCK = threading.Lock()
RANK_INDEX_STICKY = {"n": 0, "series": ""}


def record_rank_served(slug: str, body: bytes):
    """Remember the last rank page handed to the app, in card order.

    The app's discover view addresses a card by its position: playback
    requests carry series_id=<card index> (1, 2, 3...), while history-based
    and auto-advance continuations carry real ids. Serving time and card
    order are the context needed to translate the index back.
    """
    try:
        cards = [(sid, html.unescape(title))
                 for sid, title in RANK_INDEX_RE.findall(body.decode("utf-8", "replace"))]
    except Exception:
        cards = []
    with RANK_SERVED_LOCK:
        RANK_SERVED.update(slug=slug, time=time.time(), cards=cards)


def resolve_rank_index(series_id: str, episode: str) -> str:
    """Map an app-side card index to the real series id of that card.

    A small number (real ids are 19 digits) means the app played card #N of
    the discover list. Episode 1 is a fresh card press: resolve against the
    latest served page. A higher episode is an auto-advance continuation:
    keep the earlier mapping so a board refresh mid-session cannot switch
    the series under the player.
    """
    if not series_id.isdigit() or len(series_id) > 10:
        return series_id
    n = int(series_id)
    if not 1 <= n <= 200:
        return series_id
    with RANK_SERVED_LOCK:
        cards = list(RANK_SERVED["cards"])
        served_age = time.time() - RANK_SERVED["time"]
    if not cards or served_age > 7200:
        return series_id
    try:
        continuation = int(episode) > 1
    except ValueError:
        continuation = False
    if continuation and RANK_INDEX_STICKY["n"] == n and RANK_INDEX_STICKY["series"]:
        return RANK_INDEX_STICKY["series"]
    if n <= len(cards):
        sid, title = cards[n - 1]
        RANK_INDEX_STICKY.update(n=n, series=sid)
        print(f"[bridge] rank-index playback: card {n} -> series {sid} ({title})",
              flush=True)
        return sid
    return series_id


def board_items(board: str) -> list:
    if board.startswith("browse:"):
        payload = backend_get_retry("/browse?genre=" + board.split(":", 1)[1] +
                                    "&sort=hot_score&limit=60") or {}
    else:
        payload = backend_get_retry("/rank?board=" + board + "&limit=60") or {}
    items = []
    for row in (payload.get("items") or [])[:60]:
        sid = str(row.get("series_id") or "")
        if not sid.isdigit():
            continue
        items.append({
            "series_id": sid,
            "title": row.get("title") or "",
            "cover": row.get("cover") or "",
            "intro": row.get("intro") or "",
            "count": int(row.get("episode_cnt") or row.get("episode_count") or 0) or 1,
            "hot": row.get("play_cnt") or 0,
        })
    return items


def rank_items(slug: str):
    board, title, fallback = RANK_PAGES.get(slug, ("hot", "热门短剧榜", None))
    items = board_items(board)
    if not items and fallback:
        items = board_items(fallback)
    return title, items


def escape_html(value) -> str:
    return html.escape(str(value), quote=True)


def render_rank_page(slug: str, title: str, items: list) -> bytes:
    cards = []
    for idx, item in enumerate(items):
        hot = item["hot"]
        try:
            hot_text = f"{hot / 10000:.1f}万热度" if hot >= 10000 else f"{hot}热度"
        except Exception:
            hot_text = ""
        rid = "rank-title-" + str(idx + 1)
        cards.append(
            "<article aria-labelledby=\"" + rid + "\">"
            "<h2 id=\"" + rid + "\">" + escape_html(item["title"]) + "</h2>"
            "<a class=\"rank-link\" href=\"/detail?series_id=" + item["series_id"] +
            "\" aria-label=\"播放第 1 集 " + escape_html(item["title"]) + "\">"
            "<img src=\"" + escape_html(item["cover"]) + "\" alt=\"" + escape_html(item["title"]) + "\">"
            "<p class=\"item-title\">" + escape_html(item["title"]) + "</p>"
            "<p class=\"item-categories\"><span class=\"description\">" +
            escape_html(item["intro"][:120]) + "</span></p>"
            "<div class=\"item-tags\"><span class=\"episode\">更新至" + str(item["count"]) +
            "集 全共" + str(item["count"]) + "集</span>"
            "<span class=\"hot-score\">" + escape_html(hot_text) + "</span></div>"
            "</a></article>"
        )
    router = {"loaderData": {"rank_" + slug + "/page": {
        "req": {"webID": "LOCAL" + str(int(time.time()))}}}}
    body = ("<!doctype html><html><head><meta charset='utf-8'><title>" + escape_html(title) +
            "</title></head><body><h1>" + escape_html(title) + "</h1>" + "".join(cards) +
            "<script>_ROUTER_DATA = " + json.dumps(router, ensure_ascii=False) + ";</script></body></html>")
    return body.encode("utf-8")


def rank_page(slug: str) -> bytes:
    """Stale-while-revalidate rank serving. The app's rank client has a short
    timeout and shows the 发现片单 toast on any empty/late/parse-failed reply,
    so: fresh cache → serve instantly; stale cache → serve it and refresh in
    the background; no cache → synchronous fetch. A populated page is always
    preferred over a live one."""
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    cache_path = RANK_CACHE_DIR / ("rank-" + slug + ".html")
    cached = b""
    try:
        cached = cache_path.read_bytes()
    except Exception:
        pass
    if cached:
        try:
            fresh = (time.time() - cache_path.stat().st_mtime) < RANK_TTL
        except Exception:
            fresh = False
        if fresh:
            return cached

        def refresh():
            try:
                title, items = rank_items(slug)
                if items:
                    RANK_CACHE_DIR.mkdir(parents=True, exist_ok=True)
                    cache_path.write_bytes(render_rank_page(slug, title, items))
            except Exception:
                pass

        threading.Thread(target=refresh, daemon=True).start()
        print(f"[bridge {stamp}] rank {slug}: serving stale cache, refreshing", flush=True)
        return cached
    try:
        title, items = rank_items(slug)
    except Exception:
        title, items = "", []
    if items:
        body = render_rank_page(slug, title, items)
        try:
            RANK_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            cache_path.write_bytes(body)
        except Exception:
            pass
        return body
    print(f"[bridge {stamp}] rank {slug}: backend empty and no cache yet", flush=True)
    return render_rank_page(slug, title or "热门短剧榜", items)


def detail_page(series_id: str) -> bytes:
    payload = backend_get_retry("/episodes?series_id=" + urllib.parse.quote(str(series_id))) or {}
    meta = payload.get("meta") or {}
    eps = payload.get("episodes") or []
    count = len(eps) or int(meta.get("episode_cnt") or 0)
    title = meta.get("title") or ""
    cover = meta.get("cover") or ""
    intro = meta.get("intro") or ""
    info = {
        "series_id": str(meta.get("series_id") or series_id),
        "series_title": title, "series_name": title, "title": title,
        "series_cover": cover, "cover_url": cover, "thumb_url": cover,
        "description": intro, "series_intro": intro,
        "episode_count": count, "total_episode": count, "episode_cnt": count,
        "web_accessible_episodes": count, "accessible_episode_cnt": count,
        "vid_list": [e.get("vid") for e in eps],
        "video_data": [{"index": e.get("index"), "vid": e.get("vid"), "title": e.get("title"),
                        "duration": e.get("duration"), "stream_url": "/stream?vid=" + str(e.get("vid"))}
                       for e in eps],
        "celebrities": [], "hot_score_data": meta.get("play_cnt") or 0,
        "isSuccess": True, "status": 200, "publicWeb": True, "genre": "短剧",
    }
    router = {"loaderData": {"detail/page": info,
                             "detail_" + info["series_id"] + "/page": info},
              "data": info}
    body = ("<!doctype html><html><head><meta charset='utf-8'><title>" + escape_html(title) +
            "</title></head><body><p class=\"title\">" + escape_html(title) + "</p>"
            "<script>_ROUTER_DATA = " + json.dumps(router, ensure_ascii=False) + ";</script></body></html>")
    return body.encode("utf-8")


def web_search_page(params: dict) -> bytes:
    query = ""
    for name in ("keyword", "q", "query", "searchKeyword", "word"):
        value = params.get(name)
        if value:
            query = str(value).strip()
            break
    payload = backend_get("/search/page?" + urllib.parse.urlencode({"q": query})) or {}
    rows = payload.get("results") or []
    vids = []
    for row in rows[:20]:
        sid = str(row.get("series_id") or "")
        vids.append({
            "book_id": sid, "book_title": row.get("title") or "",
            "thumb_url": row.get("cover") or "",
            "series_id": sid, "series_title": row.get("title") or "",
            "series_cover": row.get("cover") or "", "series_intro": row.get("intro") or "",
            "episode_cnt": row.get("episode_cnt") or 0,
            "abstract": row.get("intro") or "",
        })
    route_data = {"isSuccess": True, "status": 200, "video_data": vids,
                  "genre": "短剧", "publicWeb": True, "searchList": vids, "results": vids}
    router = {"loaderData": {"search/page": route_data}, "data": route_data}
    body = ("<!doctype html><html><head><meta charset='utf-8'><title>搜索</title></head><body>"
            "<script>_ROUTER_DATA = " + json.dumps(router, ensure_ascii=False) + ";</script></body></html>")
    return body.encode("utf-8")


def generic_page() -> bytes:
    body = ("<!doctype html><html><head><meta charset='utf-8'></head><body>"
            "<script>_ROUTER_DATA = {\"loaderData\": {}};</script></body></html>")
    return body.encode("utf-8")


def main():
    global ACCOUNT
    ACCOUNT = load_account()
    if ACCOUNT:
        print("[bridge] restored saved account: " + ACCOUNT["name"], flush=True)

    def warm_rank_cache():
        # Seed the rank disk cache so the very first app relaunch after
        # installing already has last-good pages to fall back on.
        time.sleep(15)
        port, _ = backend_endpoint()
        if not port:
            return
        for slug in RANK_PAGES:
            try:
                rank_page(slug)
            except Exception:
                pass

    threading.Thread(target=warm_rank_cache, daemon=True).start()
    threading.Thread(target=watch_app, daemon=True).start()
    threading.Thread(target=trim_stream_cache_loop, daemon=True).start()
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        raise SystemExit("invalid port")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.daemon_threads = True
    print(f"[bridge {time.strftime('%Y-%m-%d %H:%M:%S')}] listening on "
          f"http://127.0.0.1:{args.port} (pid {os.getpid()})", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
