"""Decode advertised media-address envelopes using standard SHA512/AES-CBC.

The protocol salt is a public format constant, not a user/session credential.
Seed, derived keys and signed URLs are never written to disk by this module.
"""
import base64
import binascii
import copy
import hashlib

from desktop_range_reader import RangeReadError, media_url

SALT = bytes.fromhex(
    '4dd4c2e6b83162090e52b3c7a6733ba41cb2462b829ab58a196b39db57177524f49baf7f08e8d68d26a72e37c1a95a2f1f05a51892aef2949732b62a38aadd58')


def _base64(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 16384:
        raise ValueError('Invalid media address envelope')
    value = value.strip().strip('"')
    try:
        return base64.b64decode(value + '=' * (-len(value) % 4), altchars=b'-_', validate=True)
    except (ValueError, binascii.Error):
        raise ValueError('Invalid media address envelope') from None


def decode_address(value, seed):
    if not isinstance(value, str):
        raise ValueError('Invalid media address envelope')
    if value.startswith('https://'):
        text = value
    else:
        payload = _base64(value)
        if payload.startswith(b'\xa8'):
            if len(payload) < 20 or payload[2:4] != b'\x01\x00' or (len(payload) - 4) % 16:
                raise ValueError('Unsupported media address envelope')
            secret = _base64(seed)
            if not 1 <= len(secret) <= 4096:
                raise ValueError('Invalid media address seed')
            material = hashlib.sha512(hashlib.sha512(secret).digest() + SALT).digest()
            from Crypto.Cipher import AES
            plaintext = AES.new(material[:16], AES.MODE_CBC, material[16:32]).decrypt(payload[4:])
            pad = plaintext[-1]
            if not 1 <= pad <= 16 or plaintext[-pad:] != bytes([pad]) * pad:
                raise ValueError('Invalid media address padding')
            payload = plaintext[:-pad].rstrip(b'\0')
        try:
            text = payload.decode('utf8')
        except UnicodeError:
            raise ValueError('Invalid media address encoding') from None
    text = text.strip().replace('\\/', '/').replace('\\u0026', '&').replace('&amp;', '&')
    return media_url(text)


def decode_model_urls(model):
    result = copy.deepcopy(model)
    rows = result.get('video_list')
    if not isinstance(rows, (dict, list)) or not 1 <= len(rows) <= 64:
        raise ValueError('Invalid compatibility tracks')
    seed = result.get('key_seed')
    usable = {}
    entries = rows.items() if isinstance(rows, dict) else enumerate(rows)
    for key, row in entries:
        if not isinstance(row, dict):
            continue
        try:
            row['main_url'] = decode_address(row.get('main_url'), seed)
        except (ValueError, RangeReadError):
            continue
        backups = row.pop('backup_url', None), row.pop('backup_url_1', None)
        for address in backups:
            if not address:
                continue
            try:
                row['backup_url'] = decode_address(address, seed)
            except (ValueError, RangeReadError):
                continue
            break
        usable[key] = row
    if not usable:
        raise ValueError('No usable compatibility address')
    result['video_list'] = usable if isinstance(rows, dict) else list(usable.values())
    result.pop('key_seed', None)
    return result
