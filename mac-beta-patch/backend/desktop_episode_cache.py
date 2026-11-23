"""Keep failed episode responses out of the six-hour successful-result cache.

This is a cache admission check, not a replacement playback parser. Valid
provider objects are returned unchanged. No retries, account calls or I/O.
"""


def usable_episode_cache(value, series_id):
    if not isinstance(value, (tuple, list)) or len(value) != 2:
        return False
    meta, episodes = value
    if not isinstance(meta, dict) or str(meta.get("series_id", "")) != str(series_id):
        return False
    if not isinstance(episodes, list) or not episodes:
        return False
    for episode in episodes:
        if not isinstance(episode, dict):
            return False
        index = episode.get("index")
        if isinstance(index, str):
            if not index.isascii() or not index.isdecimal():
                return False
            # Reject excessive decimal strings before conversion.
            if len(index) > 6:
                return False
            index = int(index)
        if type(index) is not int or not 1 <= index <= 100000:
            return False
    return True
