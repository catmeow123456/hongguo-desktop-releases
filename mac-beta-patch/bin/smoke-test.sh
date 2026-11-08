#!/bin/bash
# Read-only smoke test for the local bridge and the app backend.
set -euo pipefail

python3 - <<'PY'
import json
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8787"

def get(path, timeout=30):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as response:
        return response.status, response.read(4 * 1024 * 1024)

def jget(path, timeout=30):
    status, body = get(path, timeout)
    return status, json.loads(body)

status, _ = get("/health")
assert status == 200, "bridge health failed"

_, ready = jget("/app-ready")
assert ready.get("ready") is True, "app backend not ready"

status, page = get("/lab")
assert status == 200 and "红果搜片".encode() in page, "search page missing"

_, data = jget("/app/search/page?q=" + urllib.parse.quote("闪婚"))
rows = data.get("results") or []
assert rows, "search returned no results"
print("search:", len(rows), "results")

series_id = rows[0]["series_id"]
_, listing = jget("/app/episodes?series_id=" + urllib.parse.quote(str(series_id)))
episodes = listing.get("episodes") or []
assert episodes, "no episodes"
print("episodes:", len(episodes))

vid = episodes[0]["vid"]
request = urllib.request.Request(BASE + "/app/stream?vid=" + urllib.parse.quote(str(vid)),
                                 headers={"Range": "bytes=0-65535"})
with urllib.request.urlopen(request, timeout=30) as response:
    assert response.status == 206, "range request failed"
    chunk = response.read(65536)
assert chunk, "empty stream"
print("stream: 206,", len(chunk), "bytes")
print("OK")
PY
