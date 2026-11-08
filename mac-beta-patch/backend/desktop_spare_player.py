"""Independent playback-only anonymous device. Never load an account session.

One durable device registration is reused; failed/unknown registration is not
replayed. This adapter is staged separately until real media acceptance passes.
"""
import json
import re
import threading
import time
from pathlib import Path

import requests

ENDPOINT = '/novel/player/video_model/v1/'
_lock = threading.Lock()


class SpareError(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__('备用播放路径暂不可用，原登录和播放资料已保留')


def device_path(directory, name='playback-spare.dpapi'):
    if name not in ('playback-spare.dpapi', 'playback-install.dpapi'):
        raise SpareError('invalid-state-path')
    root = Path(directory)
    if not root.is_absolute() or not root.is_dir() or root.is_symlink():
        raise SpareError('invalid-directory')
    path = root / name
    for item in (path, path.with_suffix('.attempt.json')):
        if item.is_symlink() or (item.exists() and item.stat().st_nlink != 1):
            raise SpareError('invalid-state-path')
    return path


def anonymous_client():
    client = requests.Session()
    # No .netrc credentials or inherited cookie jar. Native launch already
    # supplies the established network environment; TLS verification stays on.
    client.trust_env = False
    return client


def read_context(context, video, *, client_factory=anonymous_client, clock=time.monotonic, peer_iid=None):
    """One exact existing media route, with registration cookies only."""
    from phone_account_device import COOKIE_NAMES
    from phone_account_media import prepare, HOST, LIMIT
    from phone_account_transport import region_headers
    from desktop_video_model import parse_video_model
    if peer_iid is not None:
        from desktop_playback_request import prepare as prepare_anonymous
        url,headers,body=prepare_anonymous(context,video,int(time.time()*1000),peer_iid=peer_iid)
    else:
        url, headers, body = prepare(context, video, int(time.time() * 1000))
    cookies = {k:v for k,v in context.get('cookies', {}).items()
               if k in COOKIE_NAMES and isinstance(v, str) and len(v) < 8192}
    # A uid-routed context is not an anonymous spare, even if its session cookie
    # was accidentally omitted. Do not silently repurpose account state.
    if cookies.get('store-region-src') == 'uid':
        raise SpareError('not-anonymous')
    if peer_iid is None:headers.update(region_headers(cookies))
    client = client_factory()
    try:
        client.trust_env = False
        client.auth = None
        client.cookies.clear()
        client.headers.clear()
        for name, value in cookies.items():
            client.cookies.set(name, value, domain=HOST, path='/')
        request = client.prepare_request(requests.Request('POST', url, data=body, headers=headers))
        if request.url != url or request.body != body:
            raise SpareError('encoding-changed')
        started = clock()
        with client.send(request, timeout=(8,20), allow_redirects=False, stream=True, verify=True) as response:
            if response.status_code != 200:
                raise SpareError('http-rejected')
            chunks, size = [], 0
            for chunk in response.iter_content(32768):
                size += len(chunk)
                if size > LIMIT or clock()-started > 45:
                    raise SpareError('response-limit')
                chunks.append(chunk)
            if not size:
                raise SpareError('empty-response')
            value = json.loads(b''.join(chunks))
        if not isinstance(value, dict) or type(value.get('code')) is not int or value['code'] != 0:
            raise SpareError('service-rejected')
        entry = value.get('data', {}).get(video) if isinstance(value.get('data'), dict) else None
        if not isinstance(entry, dict) or not entry.get('video_model'):
            raise SpareError('invalid-model')
        result = {'code':0, 'data':{'video_model':entry['video_model']}}
        model = parse_video_model(result)
        from desktop_codec_fallback import recover_model, CodecCompatibilityError
        try:
            compatible = recover_model(model, agent=headers['User-Agent'])
        except CodecCompatibilityError:
            raise SpareError('unsupported-codec') from None
        if compatible is not model:
            result = {'code': 0, 'data': {'video_model': compatible}}
        return result
    except SpareError:
        raise
    except (requests.RequestException, ValueError, TypeError, KeyError):
        raise SpareError('transport-or-response') from None
    finally:
        client.close()


def read_spare(directory, video):
    from phone_account_device import initialize, load_context, InitializationError
    if not isinstance(video, str) or not re.fullmatch(r'[1-9][0-9]{7,23}', video):
        raise SpareError('invalid-episode')
    with _lock:
        path = device_path(directory)
        try:
            initialize(path, client_factory=anonymous_client)
            context = load_context(path)
        except InitializationError:
            raise SpareError('device-unavailable') from None
        peer = None
        peer_path=path.with_name('account-device.dpapi')
        if peer_path.is_file() and not peer_path.is_symlink() and peer_path.stat().st_nlink==1:
            # Preserve the existing proven pair, reading only its registered
            # installation ID, never the account session or account cookies.
            try:peer=(load_context(peer_path) or {}).get('params',{}).get('iid')
            except (ValueError,OSError):peer=None
        def valid_peer(value):
            return isinstance(value,str) and re.fullmatch(r'[1-9][0-9]{4,29}',value) and value!=context.get('params',{}).get('iid')
        if not valid_peer(peer):
            # A fresh guest has never registered an account device. Give the
            # playback request its own durable anonymous installation instead
            # of falling back to the old media format. The initializer's
            # durable attempt marker prevents automatic registration retries.
            installation = device_path(directory, 'playback-install.dpapi')
            try:
                initialize(installation, client_factory=anonymous_client)
                peer=(load_context(installation) or {}).get('params',{}).get('iid')
            except InitializationError:
                raise SpareError('device-unavailable') from None
            if not valid_peer(peer):
                raise SpareError('invalid-installation-pair')
    # Network reads do not hold the registration/state lock across episodes.
    return read_context(context, video, peer_iid=peer)


def empty_media_error(error):
    # An explicit service rejection/challenge is never an empty response.
    reason = getattr(error, 'reason', None)
    if reason is not None:
        return reason == 'upstream-empty'
    detail = getattr(error, 'diagnostic', None)
    return isinstance(detail, dict) and type(detail.get('http_status')) is int and detail['http_status'] == 200 \
        and type(detail.get('body_bytes')) is int and detail['body_bytes'] == 0


def install(client, directory, *, reader=None, clock=time.monotonic, observe=None):
    """Recover empty media and briefly reuse a proven route across episodes."""
    original = client.api
    if reader is None:
        from desktop_fresh_playback import attach
        reader = attach(client, read_spare)
    gate = threading.Lock()
    blocked_until = 0.0
    episode_blocked_until = {}
    notified = False
    preferred_until = 0.0

    from desktop_track_cache import ModelReads, ModelReadBusy
    pending = ModelReads(limit=2)

    def shared_read(video):
        def load():
            nonlocal blocked_until
            with gate:
                now = clock()
                for expired in [key for key, until in episode_blocked_until.items() if until <= now]:
                    episode_blocked_until.pop(expired)
                if now < blocked_until or now < episode_blocked_until.get(video, 0.0):
                    return None
            try:
                result = reader(directory, video)
                from desktop_video_model import parse_video_model
                parse_video_model(result)
                return result
            except Exception as error:
                with gate:
                    until = clock() + 30
                    # Empty/missing metadata or an unsupported codec describes this episode, not the
                    # availability of all other episodes. Keep duplicate
                    # quality/playback requests suppressed for this episode.
                    if isinstance(error, SpareError) and error.reason in ('empty-response', 'invalid-model', 'unsupported-codec'):
                        episode_blocked_until.pop(video, None)
                        episode_blocked_until[video] = until
                        while len(episode_blocked_until) > 32:
                            episode_blocked_until.pop(next(iter(episode_blocked_until)))
                    else:
                        # Transport failures, explicit denials and unknown
                        # errors still back off the whole spare route.
                        blocked_until = max(blocked_until, until)
                raise
        # Same-episode quality/playback callers share one outcome. Different
        # episodes can progress independently, with at most two reads active.
        return pending.resolve(video, load)

    original_quality = getattr(client, '_desktop_quality_model', None)

    def quality_model(video):
        # A guest can receive a playable default track alongside unsupported
        # lower definitions. Reuse the same anonymous reader for explicit
        # quality discovery, without changing successful account metadata.
        from desktop_video_model import parse_video_model
        video = str(video)
        if not re.fullmatch(r'[1-9][0-9]{7,23}', video):
            raise ValueError('Invalid episode')
        try:
            model = original_quality(video) if callable(original_quality) else None
        except Exception as error:
            if not empty_media_error(error):
                raise
            model = None
        if model is not None:
            return model
        result = shared_read(video)
        return parse_video_model(result) if result is not None else None

    client._desktop_quality_model = quality_model

    def playable(result):
        from desktop_video_model import parse_video_model
        from desktop_quality import desktop_track_compatible
        model = parse_video_model(result)
        mapper = getattr(client, 'video_model_tracks', None)
        if callable(mapper):
            rows = mapper(model)
        else:
            videos = model['video_list']
            rows = [{'video_meta': row} for row in
                    (videos.values() if isinstance(videos, dict) else videos) if isinstance(row, dict)]
        return any(desktop_track_compatible(row) for row in rows)

    def api(method, path, body=None, extra_query=None, max_retries=3, signed=True):
        nonlocal notified, preferred_until
        eligible = method == 'POST' and path == ENDPOINT and not extra_query and isinstance(body, dict) \
            and re.fullmatch(r'[1-9][0-9]{7,23}', str(body.get('video_id', '')))
        with gate:
            preferred = eligible and clock() < preferred_until
        attempted = False
        if preferred:
            attempted = True
            try:
                result = shared_read(str(body['video_id']))
                if result is not None and playable(result):
                    # Fixed window: a chain of successes cannot permanently
                    # suppress the original public/account route.
                    return result
            except ModelReadBusy:
                # Rapid episode changes can fill both spare slots. Preserve
                # the original route without cancelling another episode,
                # retrying the busy spare or treating capacity as a denial.
                pass
            except Exception as error:
                with gate:
                    preferred_until = 0.0
                if not isinstance(error, SpareError) or error.reason not in (
                        'empty-response', 'invalid-model', 'transport-or-response', 'unsupported-codec'):
                    raise
            with gate:
                preferred_until = 0.0
        try:
            result = original(method, path, body=body, extra_query=extra_query, max_retries=max_retries, signed=signed)
            if eligible:
                with gate:
                    preferred_until = 0.0
            return result
        except Exception as original_error:
            if not eligible or attempted or not empty_media_error(original_error):
                raise
            try:
                result = shared_read(str(body['video_id']))
            except Exception:
                raise original_error from None
            if result is None:
                raise original_error
            if playable(result):
                with gate:
                    if clock() >= preferred_until:
                        preferred_until = clock() + 30
            with gate:
                if not notified:
                    notified = True
                    try:
                        if observe is not None:
                            observe('spare-playback-recovered')
                        else:
                            target = device_path(directory).with_name('playback-spare-status.json')
                            temporary = target.with_suffix('.tmp')
                            if any(p.is_symlink() or (p.exists() and p.stat().st_nlink != 1) for p in (target, temporary)):
                                raise SpareError('invalid-status-path')
                            temporary.write_text(json.dumps({'state':'recovered','at':int(time.time())}),encoding='utf-8')
                            temporary.replace(target)
                    except Exception: pass
            return result
    client.api = api
    return True
