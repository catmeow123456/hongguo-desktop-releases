"""Recover empty media responses with a bounded anonymous device registration.

The public player route can return HTTP 200 with an empty body on this
network. When that happens, register one fresh anonymous device (stateless,
no account data) and read the same media model through the batch contract.
Failures are never retried automatically and no identity is persisted.
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROTO = ROOT / "phone-account-protocol"
if PROTO.is_dir() and str(PROTO) not in sys.path:
    sys.path.insert(0, str(PROTO))

VIDEO = re.compile(r"[1-9][0-9]{7,23}")


def _videos(path, body):
    if path == "/novel/player/video_model/v1/":
        value = body.get("video_id")
        return ([str(value)] if isinstance(value, (str, int)) and VIDEO.fullmatch(str(value)) else []), True
    if path == "/novel/player/multi_video_model/v1/":
        mapping = body.get("mixed_video_id_map")
        if not isinstance(mapping, dict):
            return [], False
        out = []
        for values in mapping.values():
            if not isinstance(values, list):
                continue
            for value in values:
                text = str(value)
                if VIDEO.fullmatch(text) and text not in out:
                    out.append(text)
        return out[:15], False
    return [], False


def install(client):
    original = client.api

    def api(method, path, body=None, extra_query=None, max_retries=3, signed=True):
        try:
            return original(method, path, body=body, extra_query=extra_query,
                            max_retries=max_retries, signed=signed)
        except Exception as error:
            detail = getattr(error, "diagnostic", None)
            empty = (isinstance(detail, dict) and detail.get("http_status") == 200
                     and detail.get("body_bytes") == 0)
            if not empty or method != "POST" or extra_query or not isinstance(body, dict):
                raise
            videos, single = _videos(path, body)
            if not videos:
                raise
            from desktop_fresh_playback import fetch_batch
            models = fetch_batch(videos)
            if single:
                model = models.get(videos[0])
                if model is None:
                    raise
                return {"code": 0, "data": {"video_model": json.dumps(model)}}
            data = {vid: {"video_model": json.dumps(model)}
                    for vid, model in models.items() if vid in videos}
            if not data:
                raise
            return {"code": 0, "data": data}

    client.api = api
    return True
