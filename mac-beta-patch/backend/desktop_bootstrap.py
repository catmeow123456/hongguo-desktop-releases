"""Managed macOS entry point. No UI, persisted keys, or phone fallback."""
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
import json
import re
import threading
import ctypes
import struct
import signal

_stage = "handshake"
_signer_exit_code = None
_signer_errors = set()
_signer_native = set()

def drain_signer(stream):
    for raw in iter(stream.readline, b""):
        line = raw.decode("utf-8", errors="replace")
        _signer_errors.update(re.findall(r"\b[A-Za-z][A-Za-z0-9_.]*(?:Exception|Error)\b", line))
        _signer_native.update(re.findall(r"\bSIG(?:ABRT|SEGV|BUS|ILL|TRAP)\b", line))
        for marker in ("Library not loaded", "code signature", "incompatible architecture", "double free", "pointer being freed was not allocated", "Incorrect checksum", "No such file"):
            if marker in line:
                _signer_native.add(marker)
        frame = re.search(r"^#\s+[CVJ]\s+\[([A-Za-z0-9_.-]{1,80})\+", line)
        if frame:
            _signer_native.add("frame-library:" + frame.group(1))
            offset = re.search(r"\+(0x[0-9a-fA-F]{1,16})\](?:\s+([A-Za-z_][A-Za-z0-9_.$]{0,127})\+(0x[0-9a-fA-F]{1,16}))?", line)
            if offset:
                _signer_native.add("frame-offset:" + offset.group(1))
                if offset.group(2):
                    _signer_native.add("frame-symbol:" + offset.group(2) + "+" + offset.group(3))
        for marker in ("jvm.cfg", "Could not find", "Could not reserve", "Error opening", "could not open"):
            if marker in line:
                _signer_errors.add(marker)

def stage(value):
    global _stage
    _stage = value


def measure_audio(path):
    """Decode at most five seconds to numeric RMS; never save media samples."""
    from array import array
    import math
    import av
    count, maximum = 0, 0.0
    with av.open(str(path)) as media:
        tracks = len(media.streams.audio)
        if tracks:
            resampler = av.AudioResampler(format="fltp", layout="mono", rate=48000)
            for index, frame in enumerate(media.decode(audio=0)):
                for output in resampler.resample(frame):
                    values = array("f")
                    values.frombytes(bytes(output.planes[0])[:output.samples * 4])
                    if values:
                        maximum = max(maximum, math.sqrt(sum(v * v for v in values) / len(values)))
                        count += len(values)
                if count >= 240000 or index >= 500:
                    break
    return {"audioTracks": tracks, "decodedSamples": count, "rmsMax": maximum}


def acceptance_media_diagnostics(data):
    """Opt-in bounded stage/counter evidence; no URLs, locals or source text."""
    if os.environ.get("HONGGUO_ACCEPTANCE_DIAGNOSTICS") != "1":
        return
    destination = data / "media-stages.json"
    samples = []
    audio_checked = set()
    audio_results = []
    (data / "media-audio.json").write_text(json.dumps({"processId": os.getpid(), "results": []}))
    started = time.monotonic()
    def sample():
        while True:
            stages = []
            for frame in sys._current_frames().values():
                modules = set()
                worker = False
                while frame:
                    module = Path(frame.f_code.co_filename).name
                    modules.add(module)
                    worker |= module == "desktop_hls_service.py" and frame.f_code.co_name == "_run"
                    frame = frame.f_back
                if not worker:
                    continue
                stages.append("encode" if "desktop_hls.py" in modules else
                              "source" if "server.py" in modules else "metadata")
            cache = data / "stream-cache"
            files = [p for p in cache.rglob("*") if p.is_file()] if cache.exists() else []
            try:
                hls_root = os.environ.get("HONGGUO_HLS_WORK_DIR")
                if hls_root and len(audio_results) < 2:
                    for playlist in Path(hls_root).rglob("index.m3u8"):
                        if playlist not in audio_checked and (playlist.parent / "complete.marker").is_file():
                            audio_checked.add(playlist)
                            try:
                                audio_results.append({"status": "decoded", **measure_audio(playlist)})
                            except Exception:
                                audio_results.append({"status": "decodeFailed"})
                            (data / "media-audio.json").write_text(json.dumps({"processId": os.getpid(), "results": audio_results}))
                            break
                samples.append({"seconds": round(time.monotonic() - started, 1),
                                "workers": sorted(stages), "cacheFiles": len(files),
                                "cacheBytes": sum(p.stat().st_size for p in files)})
                del samples[:-180]
                destination.write_text(json.dumps(samples))
            except OSError:
                pass
            time.sleep(2)
    threading.Thread(target=sample, daemon=True).start()


def validate_session(value):
    if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise RuntimeError("Invalid desktop session credential")

def launcher_path(value):
    # Rust canonicalize returns verbatim Windows paths. Some JVM launchers
    # cannot resolve their runtime/configuration from this spelling.
    text = str(value)
    if text.startswith("\\\\?\\UNC\\"):
        return "\\\\" + text[8:]
    if text.startswith("\\\\?\\") and len(text) > 6 and text[5:7] == ":\\":
        return text[4:]
    return text


def api_socket():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        # The OS chooses a free port; keep the exclusive socket until shutdown.
        # Never probe, release, and then try to reacquire an advertised port.
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        listener.setblocking(False)
        return listener
    except BaseException:
        listener.close()
        raise


def announce_endpoint(listener):
    # One bounded control line, only after START and successful socket ownership.
    # stdout is a private parent pipe, not a log or a persisted endpoint file.
    port = listener.getsockname()[1]
    # Binary output avoids Windows text-mode CRLF translation of the protocol.
    sys.stdout.buffer.write(f"HONGGUO_PORT_V1 {port}\n".encode("ascii"))
    sys.stdout.buffer.flush()
    # Providers may print. They must not retain or block the control pipe.
    with open(os.devnull, "w") as sink:
        os.dup2(sink.fileno(), sys.stdout.fileno())
    os.environ["PORT"] = str(port)
    return port


def parse_owned_listener(text, pid):
    owner = None
    ports = []
    for line in text.splitlines():
        if line.startswith("p"):
            owner = int(line[1:])
        elif line.startswith("n"):
            if owner != pid or not re.fullmatch(r"127\.0\.0\.1:[0-9]{1,5}", line[1:]):
                raise RuntimeError("Owned signer has an unexpected listener")
            port = int(line.rsplit(":", 1)[1])
            if not 1 <= port <= 65535:
                raise RuntimeError("Invalid signer port")
            ports.append(port)
    if len(ports) > 1:
        raise RuntimeError("Owned signer endpoint is ambiguous")
    return ports[0] if ports else None


def owned_listener_port(pid, query=None):
    result = subprocess.run(["/usr/sbin/lsof", "-nP", "-a", "-p", str(pid),
                             "-iTCP", "-sTCP:LISTEN", "-Fpn"],
                            capture_output=True, timeout=3, check=False)
    if result.returncode == 1 and not result.stdout:
        return None
    if result.returncode != 0 or len(result.stdout) > 65536:
        raise RuntimeError("Cannot inspect owned signer listener")
    return parse_owned_listener(result.stdout.decode("ascii", "strict"), pid)


def watch_parent():
    # The Rust parent creates this dedicated group atomically before START.
    group = os.getpgrp()
    if group != os.getpid():
        raise RuntimeError("Dedicated desktop process group required")
    def wait_for_eof():
        try:
            while sys.stdin.read(1):
                pass
        finally:
            os.killpg(group, signal.SIGKILL)
    threading.Thread(target=wait_for_eof, name="desktop-parent-watch", daemon=True).start()


def wait_signer(process, timeout=45):
    global _signer_exit_code
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            _signer_exit_code = process.returncode
            raise RuntimeError("Signing service exited")
        port = owned_listener_port(process.pid)
        if port is not None:
            if process.poll() is not None:
                _signer_exit_code = process.returncode
                raise RuntimeError("Signing service exited")
            return port
        time.sleep(0.05)
    raise RuntimeError("Signing service startup timed out")


def serve_owned_api(server, listener, signer):
    """Stop serving when our signer exits; never attach to or restart a signer."""
    global _signer_exit_code
    stopped = threading.Event()
    exited = []

    def watch():
        while not stopped.is_set():
            code = signer.poll()
            if code is not None:
                exited.append(code)
                server.should_exit = True
                return
            stopped.wait(0.25)

    monitor = threading.Thread(target=watch, name="desktop-signer-watch", daemon=True)
    monitor.start()
    try:
        server.run(sockets=[listener])
    finally:
        stopped.set()
        monitor.join()
    # Also catch an exit between the final poll and a normal API return. The
    # caller terminates a healthy signer only after this function has returned.
    code = exited[0] if exited else signer.poll()
    if code is not None:
        _signer_exit_code = code
        stage("signer_runtime")
        raise RuntimeError("Owned signing service exited while serving")


def main():
    # This must precede provider imports and subprocess creation: the parent
    # only sends START after creating the dedicated process group.
    if sys.stdin.readline(16) != "START\n":
        raise RuntimeError("Desktop parent handshake required")
    watch_parent()
    validate_session(os.environ.get("HONGGUO_SESSION_API_KEY", ""))
    stage("configuration")
    root = Path(launcher_path(Path(__file__).resolve())).parent
    data = Path(os.environ["HONGGUO_BACKEND_DATA_DIR"])
    if not data.is_absolute():
        raise RuntimeError("An absolute application data directory is required")
    data.mkdir(parents=True, exist_ok=True)
    content_config = Path(os.environ.get("HONGGUO_CONTENT_CONFIG") or data / "content-config.json")
    if not content_config.is_absolute() or not content_config.is_file():
        raise RuntimeError("Content configuration is missing")
    os.environ["HONGGUO_CONTENT_CONFIG"] = str(content_config)
    os.environ["ADMIN_TOKEN"] = secrets.token_hex(32)
    os.environ.pop("SIGN_SERVER", None)
    os.environ["BIND_HOST"] = "127.0.0.1"
    os.environ["HONGGUO_STREAM_CACHE"] = str(data / "stream-cache")
    acceptance_media_diagnostics(data)
    if os.environ.get("HONGGUO_HLS_WORK_DIR"):
        os.environ["HONGGUO_HLS_WORK_DIR"] = launcher_path(os.environ["HONGGUO_HLS_WORK_DIR"])
    os.environ["IMPERSONATE"] = ""
    os.environ["DEVICE_POOL_SIZE"] = "0"  # Desktop never rotates device identities.
    os.environ.pop("API_KEYS", None)
    # Do not inherit arbitrary developer proxy settings in packaged mode.
    os.environ.pop("HONGGUO_PROXY", None)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        os.environ.pop(name, None)
    sys.path.insert(0, str(root))  # -I intentionally excludes the script directory.
    stage("api_bind")
    listener = api_socket()  # Keep ownership through uvicorn startup.
    signer = None
    reader = None
    try:
        port = announce_endpoint(listener)
        # The signer owns its OS-assigned port continuously from bind to exit.
        stage("signer_spawn")
        signer = subprocess.Popen(
            [str(root / "jre/bin/java"), "-Djava.net.preferIPv4Stack=true", "--add-opens", "java.base/java.lang=ALL-UNNAMED",
             "-Xmx512m", "-XX:+ExitOnOutOfMemoryError", "-cp", "unidbg-sign.jar",
             "com.hongguo.sign.FqTrace", "serve", "0"],
            cwd=root / "sign", stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        reader = threading.Thread(target=drain_signer, args=(signer.stdout,), daemon=True)
        reader.start()
        stage("signer_ready")
        signer_port = wait_signer(signer)
        os.environ["SIGN_SERVER"] = f"http://127.0.0.1:{signer_port}"
        stage("provider_import")
        import hongguo
        from desktop_public_player import install
        install(hongguo, data)
        from fresh_player import install as install_fresh_player
        install_fresh_player(hongguo)
        from guest_search_fallback import install as install_guest_search
        install_guest_search(hongguo)
        import server
        from guest_search_fallback import install_pages as install_guest_pages
        install_guest_pages(server)

        import uvicorn
        stage("api_run")
        config = uvicorn.Config(server.app, host="127.0.0.1", port=port,
                                log_config=None, access_log=False, log_level="critical")
        serve_owned_api(uvicorn.Server(config), listener, signer)
    finally:
        listener.close()
        if signer is not None:
            if signer.poll() is None:
                signer.terminate()
            try:
                signer.wait(timeout=5)
            except subprocess.TimeoutExpired:
                signer.kill()
                signer.wait(timeout=5)
        if reader is not None:
            reader.join(timeout=1)


def write_current_failure(error):
    """Best-effort fixed fields in a directory owned by this child invocation."""
    stages = {"handshake", "configuration", "api_bind", "signer_spawn", "signer_ready",
              "provider_import", "api_run", "signer_runtime"}
    types = {"RuntimeError", "ValueError", "TypeError", "OSError", "FileNotFoundError",
             "PermissionError", "ImportError", "ModuleNotFoundError", "TimeoutError",
             "MemoryError", "UnicodeDecodeError", "JSONDecodeError", "KeyError"}
    try:
        directory = Path(os.environ["HONGGUO_STARTUP_DIAGNOSTIC_DIR"])
        if not directory.is_absolute() or not directory.is_dir():
            return
        code = _signer_exit_code
        if type(code) is not int or not -2147483648 <= code <= 4294967295:
            code = None
        value = {"schema": 1, "stage": _stage if _stage in stages else "unknown",
                 "error_type": type(error).__name__ if type(error).__name__ in types else "unknown",
                 "signer_exit_code": code}
        # Never overwrite an unexpected file/link. No paths, error text, signer
        # stderr, credentials or stable identifiers enter the portable summary.
        with (directory / "failure.json").open("x", encoding="utf-8") as stream:
            json.dump(value, stream)
    except Exception:
        pass


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        write_current_failure(error)
        # Only a fixed stage and exception type; never serialize error text.
        try:
            data = Path(os.environ["HONGGUO_BACKEND_DATA_DIR"])
            if data.is_absolute() and data.is_dir():
                (data / "startup-status.json").write_text(json.dumps({"stage": _stage, "error_type": type(error).__name__, "signer_exit_code": _signer_exit_code, "signer_errors": sorted(_signer_errors), "native_markers": sorted(_signer_native)}), encoding="utf-8")
        except Exception:
            pass
        # Parent detects process exit. Never emit raw exceptions containing URLs.
        sys.exit(1)
