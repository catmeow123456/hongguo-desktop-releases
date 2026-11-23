"""macOS adapter for the Windows 1.0.138 SDK V5 playback fallback (2026-11-23).

Upstream retired the old anonymous media route: the app's account route and
the fresh-playback fallback now receive HTTP 200 with an empty body, so
playback fails with PLAY_FAILED. Windows 1.0.138 answers with an SDK V5
reader (desktop_sdk_player_v5 + phone_*_v5); those files are ported
unmodified. This module only adapts the platform parts:

  - Windows DPAPI state protection -> AES-GCM with a per-user key file
    (mode 0600, created once in the backend data directory). The threat
    model matches DPAPI's "user-scoped at rest": same-user malware can read
    either; other users cannot.
  - The reader and its anonymous device live in the backend data directory
    next to the other runtime state; nothing is written into the repo.

If `unicorn` (arm64 CPU emulator for the native signing library) or
`Crypto` is missing, the adapter is disabled and the backend behaves
exactly as before.
"""
import importlib.util
import json
import os
import sys
import threading
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parent
PROTO = BACKEND / "phone-account-protocol"
RECOVERABLE = ("empty-response", "invalid-model")  # same gate as 1.0.138 SdkFallback
_readers = {}
_lock = threading.Lock()
_protection_installed = False


def _log(message):
    try:
        print("[v5] %s" % message, file=sys.stderr, flush=True)
    except Exception:
        pass


def _data_dir():
    value = os.environ.get("HONGGUO_BACKEND_DATA_DIR")
    root = Path(value) if value else Path.home() / "Library" / "Application Support" / "cn.guoban.desktop-companion"
    root.mkdir(parents=True, exist_ok=True)
    return root


def available():
    """The reader needs the emulator and a cipher; never guess on import."""
    try:
        return importlib.util.find_spec("unicorn") is not None and importlib.util.find_spec("Crypto") is not None
    except Exception:
        return False


def _install_protection():
    """Replace Windows-only dpapi() with a reversible AES-GCM file key."""
    global _protection_installed
    if _protection_installed:
        return
    from Crypto.Cipher import AES
    import phone_account_device as device

    key_path = _data_dir() / "playback-v5.key"

    def read_key():
        data = key_path.read_bytes()
        if len(data) != 32:
            raise ValueError("v5-state-key-invalid")
        return data

    def protect(data, decrypt=False):
        if not isinstance(data, (bytes, bytearray)):
            raise ValueError("v5-state-bytes-required")
        data = bytes(data)
        if decrypt:
            return AES.new(read_key(), AES.MODE_GCM, nonce=bytes(data[:12])).decrypt_and_verify(
                bytes(data[12:-16]), bytes(data[-16:]))
        cipher = AES.new(read_key(), AES.MODE_GCM, nonce=os.urandom(12))
        encrypted, tag = cipher.encrypt_and_digest(data)
        return cipher.nonce + encrypted + tag

    # Create the key exclusively; a lost key invalidates the saved device and
    # SDK session, which then re-register on the next playback (no replay).
    if not key_path.exists():
        try:
            handle = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(handle, os.urandom(32))
            finally:
                os.close(handle)
        except FileExistsError:
            pass
    read_key()
    device.dpapi = protect
    _protection_installed = True


def _reader(root):
    with _lock:
        reader = _readers.get(root)
        if reader is not None:
            return reader
        for entry in (str(BACKEND), str(PROTO)):
            if entry not in sys.path:
                sys.path.insert(0, entry)
        _install_protection()
        from desktop_sdk_player_v5 import Reader
        from desktop_spare_player import SpareError, anonymous_client
        from phone_account_device import InitializationError, initialize

        device_file = root / "playback-spare.dpapi"
        try:
            initialize(device_file, client_factory=anonymous_client)
        except InitializationError as error:
            # Upstream keeps the durable attempt marker: one network attempt,
            # manual review after a crash. Recovery = delete the files below.
            _log("device init failed (%s); reset = delete playback-spare.* in %s" % (error, root))
            raise SpareError("v5-device-unavailable") from None
        reader = Reader(root)
        _readers[root] = reader
        return reader


def read_models(videos, fallback_error=None):
    """Return {video: model} via SDK V5, or {} when V5 cannot help.

    Called only after the original fresh-playback fallback failed; when {}
    is returned the caller re-raises the original error, so behavior stays
    identical whenever the emulator is missing or the route is unhealthy.
    """
    reason = getattr(fallback_error, "reason", None)
    if fallback_error is not None and reason not in RECOVERABLE:
        return {}
    if not videos:
        return {}
    if not available():
        _log("disabled: unicorn or Crypto not installed")
        return {}
    started = time.time()
    root = _data_dir()
    try:
        reader = _reader(root)
    except Exception as error:
        _log("no reader: %s" % (getattr(error, "reason", None) or error))
        return {}
    models = {}
    for video in videos:
        try:
            models[video] = reader.read_model(str(video))
        except Exception as error:
            _log("%s: %s" % (video, getattr(error, "reason", None) or type(error).__name__))
    if models:
        stats = dict(reader.stats)
        stats["seconds"] = round(time.time() - started, 1)
        _log("read %d/%d model(s) %s" % (len(models), len(videos), json.dumps(stats, ensure_ascii=False)))
    return models
