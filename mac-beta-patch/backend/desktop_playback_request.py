"""Playback-only request framing; saved identity and login signing are unchanged."""
import base64
import copy
import gzip
import hashlib
import json
import re
import secrets
import struct
from urllib.parse import parse_qsl,quote,urlencode,urlsplit,urlunsplit

FIELDS=frozenset('ac device_platform os ssmix language iid device_id channel aid app_name version_code version_name device_type device_brand os_api os_version manifest_version_code resolution dpi update_version_code'.split())
PROFILE=dict(version_name='7.1.3.32',version_code='71332',update_version_code='71332',
             manifest_version_code='71332',channel='update_64',device_brand='Redmi',device_type='25053RT47C',
             os_version='16',os_api='36',resolution='1280*2772',dpi='520')
AGENT='com.phoenix.read/71332 (Linux; U; Android 16; zh_CN; 25053RT47C; Build/BP2A.250605.031.A3; Cronet/TTNetVersion:04657795 2026-01-23 QuicVersion:c67e9834 2025-09-08)'
FRAME=bytes.fromhex('03000000f7e85ffad7d7dc3bd62ac87057cf6118')

def prepare(context,video,now_ms,*,peer_iid,device_level=3):
    if not isinstance(context,dict) or not isinstance(context.get('params'),dict):raise ValueError('invalid-device')
    if not isinstance(peer_iid,str) or not re.fullmatch(r'[1-9][0-9]{4,29}',peer_iid) or peer_iid==context['params'].get('iid'):
        raise ValueError('invalid-installation-pair')
    saved=context.get('cookies',{})
    if not isinstance(saved,dict) or saved.get('store-region-src')=='uid':raise ValueError('anonymous-device-required')
    return _prepare(context,video,now_ms,installation_iid=peer_iid,anonymous=True,device_level=device_level)


def prepare_account(context,video,now_ms):
    """Use this account's registered identity; transport owns auth headers/cookies."""
    if not isinstance(context,dict) or not isinstance(context.get('params'),dict):raise ValueError('invalid-device')
    return _prepare(context,video,now_ms,installation_iid=context['params'].get('iid'),anonymous=False)


def prepare_fresh(context, videos, now_ms):
    """One anonymous registration, one bounded batch; no saved account identity."""
    if not isinstance(context,dict) or context.get('cookies',{}).get('store-region-src')=='uid':
        raise ValueError('anonymous-device-required')
    if not isinstance(videos,list) or not 1<=len(videos)<=15 or len(set(videos))!=len(videos) \
            or any(not isinstance(v,str) or not re.fullmatch(r'[1-9][0-9]{7,23}',v) for v in videos):
        raise ValueError('invalid-playback-batch')
    return _prepare(context,videos[0],now_ms,installation_iid=context.get('params',{}).get('iid'),
                    anonymous=True,device_level=4,batch=videos)


def _prepare(context,video,now_ms,*,installation_iid,anonymous,device_level=3,batch=None):
    from phone_account_media import _prepare_unsigned
    from phone_account_medusa import proof_message,envelope
    from phone_account_medusa_digest import selector
    from phone_account_signing import passport_components
    if type(device_level) is not int or device_level not in (3,4):raise ValueError('invalid-playback-device-level')
    if not isinstance(context,dict) or not isinstance(context.get('params'),dict):raise ValueError('invalid-device')
    if not isinstance(installation_iid,str) or not re.fullmatch(r'[1-9][0-9]{4,29}',installation_iid):raise ValueError('invalid-installation')
    saved=context.get('cookies',{})
    if not isinstance(saved,dict):raise ValueError('invalid-device-cookies')
    scoped=copy.deepcopy(context);scoped['params'].update(PROFILE);scoped['user_agent']=AGENT
    url,_,compressed=_prepare_unsigned(scoped,video,now_ms)
    payload=json.loads(gzip.decompress(compressed));payload['biz_param']['device_level']=device_level
    if batch is not None:payload['mixed_video_id_map']={'1004':batch}
    body=json.dumps(payload,sort_keys=True,separators=(',',':')).encode()
    parts=urlsplit(url);params={k:v for k,v in parse_qsl(parts.query,keep_blank_values=True) if k in FIELDS}
    params.update(ac='wifi',device_platform='android',os='android',ssmix='a',language='zh',iid=installation_iid)
    seconds=now_ms//1000;body_digest=hashlib.md5(body).digest()
    for delta in range(256):
        params['_rticket']=str(now_ms+delta)
        query=(urlencode(sorted(params.items()),quote_via=quote,safe="-_.!~*'()")+'&ts='+str(seconds)).encode()
        if (selector(hashlib.new('sm3',query).digest(),body_digest,seconds)&15)%3==2:break
    else:raise ValueError('playback-signature-unavailable')
    nonce=tuple(secrets.randbits(32) for _ in range(3))
    message=proof_message(query,body,seconds,params['device_id'],nonce[0],version_name='7.1.3.32',proof_identity=context['params'].get('openudid') or 'none')
    from phone_account_medusa_digest import digest_branch_two
    digest=hashlib.new('sm3',query).digest();proof=digest_branch_two(digest,body_digest,seconds)
    raw=base64.b64decode(envelope(message,digest,proof,seconds,nonce[1],nonce[2]))
    signature=base64.b64encode(bytes(a^b for a,b in zip(FRAME,struct.pack('<I',seconds)*5))+raw[20:]).decode()
    cookies={'passport_csrf_token':'','passport_csrf_token_default':'','store-region':saved.get('store-region') or 'cn','store-region-src':'did'}
    for key in ('odin_tt','ttreq'):
        if saved.get(key):cookies[key]=saved[key]
    cookies['install_id']=installation_iid
    token=context.get('device_token','')
    for value in [*cookies.values(),token]:
        if not isinstance(value,str) or len(value)>8192 or re.search(r'[\x00-\x20;\x7f-\uffff]',value):raise ValueError('invalid-device-header')
    headers={'User-Agent':AGENT,'Accept':'application/json; charset=utf-8,application/x-protobuf',
             'Content-Type':'application/json; charset=utf-8','Accept-Encoding':'gzip, deflate',
             'X-Xs-From-Web':'0','X-Vc-Bdturing-Sdk-Version':'4.0.3.cn','Lc':'101','Sdk-Version':'2',
             'Passport-Sdk-Version':'5051452','X-SS-DP':str(params.get('aid','8662')),'X-SS-REQ-TICKET':str(now_ms),
             'X-TT-Store-Region':'cn','X-TT-Store-Region-Src':'uid','Cookie':'; '.join(k+'='+v for k,v in cookies.items())}
    if not anonymous:
        # Do not shadow the account transport's cookie jar or authenticated region.
        for key in ('Cookie','X-TT-Store-Region','X-TT-Store-Region-Src'):headers.pop(key)
    headers.update(passport_components(query,body,seconds));headers['X-Medusa']=signature
    if token:headers['X-TT-DT']=token
    trace=f'{now_ms&0xffffffff:08x}10{secrets.token_hex(8)}';headers['X-TT-Trace-ID']=f'00-{trace}-{trace[:16]}-01'
    return urlunsplit((parts.scheme,parts.netloc,parts.path,query.decode(),'')),headers,body
