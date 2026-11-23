"""One provider-advertised compatible-model read for ByteVC2-only episodes.

No account transport, device mutation, retry, stored address or decoder changes.
Interface behavior is independently implemented from observed metadata.
"""
import json
import time
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from desktop_video_model import parse_video_model

HOSTS = frozenset(('vas-lf-x.snssdk.com', 'vas-hl-x.snssdk.com'))
LIMIT = 2 * 1024 * 1024


class CodecCompatibilityError(ValueError):
    pass


def unsupported_only(model):
    rows = model.get('video_list') if isinstance(model, dict) else None
    rows = list(rows.values()) if isinstance(rows, dict) else rows
    if not isinstance(rows, list) or not rows:
        return False
    for row in rows:
        if not isinstance(row, dict):
            return False
        meta = row.get('video_meta')
        codec = (meta if isinstance(meta, dict) else {}).get('codec_type', row.get('codec_type'))
        if not isinstance(codec, str) or codec.strip().lower() not in ('bytevc2', 'bvc2'):
            return False
    return True


def compatibility_url(model):
    descriptor = model.get('fallback_api')
    value = descriptor.get('fallback_api') if isinstance(descriptor, dict) else descriptor
    try:
        if not isinstance(value, str) or len(value) > 16384 or any(ord(c) < 33 for c in value):
            raise ValueError()
        parts = urlsplit(value)
        if (parts.scheme != 'https' or parts.hostname not in HOSTS or parts.port not in (None, 443)
                or parts.username or parts.password or parts.fragment
                or not parts.path.startswith('/video/fplay/1/') or '..' in parts.path):
            raise ValueError()
        pairs = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True, max_num_fields=128)
                 if k not in ('force_fids', 'codec_type')]
        pairs.append(('codec_type', '1'))
        return urlunsplit((parts.scheme, parts.netloc, parts.path,
                          urlencode(pairs, quote_via=quote), ''))
    except (ValueError, TypeError):
        raise CodecCompatibilityError('invalid-compatibility-address') from None


def parse_compatible(value, original):
    """Accept bounded nested data/video_info envelopes, never ambiguous models."""
    models = []
    nodes = 0

    def visit(node, depth):
        nonlocal nodes
        nodes += 1
        if depth > 6 or nodes > 64:
            raise CodecCompatibilityError('invalid-compatibility-model')
        if not isinstance(node, dict):
            return
        if 'code' in node and (type(node['code']) is not int or node['code'] != 0):
            raise CodecCompatibilityError('compatibility-rejected')
        if 'video_list' in node or 'video_model' in node:
            models.append(parse_video_model({'code': 0, 'data': node}))
            return
        for key in ('video_info', 'data'):
            child = node.get(key)
            if isinstance(child, dict):
                visit(child, depth + 1)
        # Some envelopes key the sole model by its numeric episode id.
        for key, child in node.items():
            if isinstance(key, str) and key.isascii() and key.isdigit() and 8 <= len(key) <= 24:
                visit(child, depth + 1)

    visit(value, 0)
    if len(models) != 1 or unsupported_only(models[0]):
        raise CodecCompatibilityError('no-compatible-model')
    model = models[0]
    if original.get('video_id') and model.get('video_id') and original['video_id'] != model['video_id']:
        raise CodecCompatibilityError('different-video-model')
    return {**original, **model}


def recover_model(model, *, agent, client_factory=None, clock=time.monotonic):
    if not unsupported_only(model):
        return model
    target = compatibility_url(model)
    if client_factory is None:
        import requests
        client_factory = requests.Session
    client = client_factory()
    try:
        client.trust_env = False
        client.auth = None
        client.cookies.clear()
        client.headers.clear()
        started = clock()
        with client.get(target, headers={'User-Agent': agent, 'Referer': 'https://novel.snssdk.com/',
                                        'Accept': 'application/json'},
                        stream=True, allow_redirects=False, timeout=(5, 15), verify=True) as response:
            if response.status_code != 200:
                raise CodecCompatibilityError('compatibility-rejected')
            chunks, size = [], 0
            for chunk in response.iter_content(32768):
                size += len(chunk)
                if size > LIMIT or clock() - started > 20:
                    raise CodecCompatibilityError('compatibility-limit')
                chunks.append(chunk)
            if not size:
                raise CodecCompatibilityError('compatibility-empty')
            compatible = parse_compatible(json.loads(b''.join(chunks)), model)
            from desktop_codec_urls import decode_model_urls
            try:
                decoded = decode_model_urls(compatible)
            except ValueError:
                raise CodecCompatibilityError('no-compatible-model') from None
            if unsupported_only(decoded):
                raise CodecCompatibilityError('no-compatible-model')
            return decoded
    finally:
        client.close()
