"""Anonymous guest-search fallback.

The legacy signed search route is rejected upstream with code 100103
(param_invalid). A freshly registered anonymous device using the version-3
search signature frame still gets results, so both /search and /search/page
fall back to it only when the original path fails. Nothing is written to disk.
"""
import base64
import json
import sys
import threading
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_PROTO = _ROOT / "phone-account-protocol"
if _PROTO.is_dir() and str(_PROTO) not in sys.path:
    sys.path.insert(0, str(_PROTO))

import desktop_guest_search as G
from desktop_fresh_playback import exchange
from phone_account_device import USER_AGENT, parse_identity, prepare_registration

_CONTEXT = {"value": None, "time": 0.0}
_CONTEXT_TTL = 1800.0
_LOCK = threading.Lock()


def _new_context():
    url, body, headers, params = prepare_registration()
    data, cookies = exchange(url, headers, body)
    issued = parse_identity(data)
    params.update(device_id=issued["device_id"], iid=issued["iid"])
    return {"params": params, "cookies": cookies,
            "device_token": issued["device_token"], "user_agent": USER_AGENT}


def _context():
    with _LOCK:
        value = _CONTEXT["value"]
        if value is not None and time.monotonic() - _CONTEXT["time"] < _CONTEXT_TTL:
            return value
    value = _new_context()
    with _LOCK:
        _CONTEXT["value"] = value
        _CONTEXT["time"] = time.monotonic()
    return value


def _invalidate():
    with _LOCK:
        _CONTEXT["value"] = None
        _CONTEXT["time"] = 0.0


def _row(item):
    count = item.get("episodeCount", 0)
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        count = 0
    return {
        "series_id": item["seriesId"],
        "title": item["title"],
        "episode_cnt": count,
        "score": "",
        "play_cnt": 0,
        "hot": "",
        "copyright": "",
        "cover": item.get("cover", ""),
        "intro": (item.get("intro") or "")[:60],
        "vid": "",
        "duration": 0,
        "horiz_cover": "",
        "cover_tags": [],
    }


def _search_once(query, offset, limit):
    for attempt in (0, 1):
        try:
            return G.search(_context(), query, offset, "short")
        except Exception:
            if attempt:
                raise
            _invalidate()


def page(query, offset=0, limit=40):
    payload = _search_once(query, offset, limit)
    if not payload.get("items"):
        try:
            alternate = G.search(_context(), query, offset, "manju")
            if alternate.get("items"):
                payload = alternate
        except Exception:
            pass
    rows = [_row(item) for item in (payload.get("items") or [])][:limit]
    more = bool(payload.get("hasMore")) and bool(rows)
    cursor = None
    if more:
        token = base64.urlsafe_b64encode(
            json.dumps({"q": query, "o": int(payload.get("nextOffset", offset))},
                       ensure_ascii=True).encode()).decode()
        cursor = token
    return {"query": query, "results": rows, "has_more": more,
            "next_cursor": cursor, "coverage": "unverified",
            "unique_results_seen": len(rows)}


def search_all(query, max_items=20):
    results, seen, offset = [], set(), 0
    for _ in range(8):
        payload = page(query, offset, max_items)
        for row in payload["results"]:
            if row["series_id"] not in seen:
                seen.add(row["series_id"])
                results.append(row)
        cursor = payload.get("next_cursor")
        if not cursor or len(results) >= max_items:
            break
        try:
            offset = int(json.loads(base64.urlsafe_b64decode(cursor))["o"])
        except Exception:
            break
        if offset <= 0:
            break
    return results[:max_items]


def install(hongguo):
    original = hongguo.search

    def search(query, max_items=None):
        try:
            return original(query, max_items)
        except Exception:
            return search_all(query, max_items or 20)

    hongguo.search = search
    return search


class Pager:
    def page(self, query, cursor=None):
        query = (query or "").strip()
        if not query or len(query) > 80:
            raise ValueError("Invalid search query")
        offset = 0
        if cursor:
            try:
                state = json.loads(base64.urlsafe_b64decode(cursor))
                if state.get("q") != query:
                    raise ValueError
                offset = int(state["o"])
            except Exception:
                raise ValueError("Search cursor expired or mismatched")
            if not 0 <= offset <= 50000:
                raise ValueError("Search cursor expired or mismatched")
        return page(query, offset, 40)


def install_pages(server_module):
    original = server_module._search_pager
    fallback = Pager()

    class Wrapper:
        def page(self, query, cursor=None):
            try:
                return original.page(query, cursor)
            except Exception:
                return fallback.page(query, cursor)

    server_module._search_pager = Wrapper()
    return server_module._search_pager
