"""Independent anonymous search worker; existing device only, one GET per call.

Search uses its verified version-3 outer signature frame. Account/playback
signers are imported unchanged. No registration, account restore or disk writes.
"""
import base64,hashlib,json,secrets,struct,sys,time
from pathlib import Path
from urllib.parse import quote,urlencode,urlsplit

HOST='api5-normal-sinfonlinea.fqnovel.com'
PATH='/reading/bookapi/search/tab/v'
LIMIT=2*1024*1024
FIELDS='iid device_id ac channel aid app_name cdid version_code version_name device_platform os ssmix device_type device_brand language os_api os_version manifest_version_code resolution dpi update_version_code host_abi dragon_device_type pv_player compliance_status need_personal_recommend player_so_load is_android_pad_screen openudid'.split()

def inputs(query,offset,mode):
    if not isinstance(query,str) or not query.strip() or len(query)>80 or any(ord(c)<32 for c in query):raise ValueError('invalid-query')
    if type(offset) is not int or not 0<=offset<=50000:raise ValueError('invalid-offset')
    if mode not in ('manju','short'):raise ValueError('invalid-mode')
    return query.strip()

def prepare(context,query,offset,mode,now_ms=None):
    from phone_account_favorites import request_context
    from phone_account_signing import passport_components
    from phone_account_medusa import medusa,VERSION
    from phone_account_medusa_digest import selector
    query=inputs(query,offset,mode)
    cookies=context.get('cookies',{})
    if set(cookies)-{'odin_tt','ttreq','store-region','store-region-src'} or cookies.get('store-region-src')=='uid':raise ValueError('not-anonymous')
    if not isinstance(cookies.get('ttreq'),str) or not cookies['ttreq']:raise ValueError('guest-ticket-unavailable')
    values=request_context(context)['params']
    values['openudid']=str(context['params'].get('openudid',''))
    if set(FIELDS)-values.keys() or values['aid']!='8662':raise ValueError('invalid-device')
    if any(not str(values[k]).isascii() or not str(values[k]).isdigit() for k in ('iid','device_id')):raise ValueError('invalid-device')
    now=int(time.time()*1000) if now_ms is None else now_ms
    if type(now) is not int or not 0<=now//1000<=0xffffffff:raise ValueError('invalid-clock')
    pairs=[(k,str(values[k])) for k in FIELDS]
    pairs.extend({'query':query,'tab_type':'11','count':'100','offset':str(offset),'use_correct':'true','bookshelf_search_plan':'4','search_source':'1','tab_name':'feed','bookstore_tab':'16','user_is_login':'0',
        'selected_items':'content_type_dynamic_comic' if mode=='manju' else 'content_type_short_play'}.items())
    for delta in range(256):
        ticket=now+delta;seconds=ticket//1000
        encoded=urlencode(pairs+[('ts',str(seconds)),('_rticket',str(ticket))],quote_via=quote,safe="-_.!~*'()").encode()
        if (selector(hashlib.new('sm3',encoded).digest(),bytes(16),seconds)&15)%3==2:break
    else:raise ValueError('signature-unavailable')
    headers=passport_components(encoded,b'',seconds)
    raw=base64.b64decode(medusa(encoded,b'',seconds,str(values['device_id'])),validate=True)
    mask=struct.pack('<I',seconds)*5
    if bytes(a^b for a,b in zip(raw[:20],mask))!=VERSION:raise ValueError('unknown-signature-frame')
    frame=bytes.fromhex('03000000f7e85ffad7d7dc3bd62ac87057cf6118')
    headers['X-Medusa']=base64.b64encode(bytes(a^b for a,b in zip(frame,mask))+raw[20:]).decode()
    headers.update({'Accept':'application/json; charset=utf-8,application/x-protobuf','User-Agent':context['user_agent'],
        'X-SS-DP':'8662','lc':'101','X-Reading-Request':str(ticket)+'-'+str(secrets.randbelow(2**31)),
        'X-SS-REQ-TICKET':str(ticket),'X-TT-Request-Tag':'t=0;n=0','Sdk-Version':'2',
        'Passport-SDK-Version':'50561','X-VC-Bdturing-SDK-Version':'3.7.2.cn','X-XS-From-Web':'0','X-TT-DT':''})
    guest={'passport_csrf_token':'','passport_csrf_token_default':'','store-region':cookies.get('store-region',''),
        'store-region-src':cookies.get('store-region-src',''),'install_id':str(values['iid']),'ttreq':cookies['ttreq']}
    if any(len(str(v))>8192 or any(c in str(v) for c in '\r\n;') for v in guest.values()):raise ValueError('invalid-guest-cookie')
    headers['Cookie']='; '.join(k+'='+str(v) for k,v in guest.items())
    return 'https://'+HOST+PATH+'?'+encoded.decode(),headers

def parse(payload,offset):
    if not isinstance(payload,dict) or type(payload.get('code')) is not int or payload['code']!=0:raise ValueError('search-rejected')
    tabs=payload.get('search_tabs')
    if not isinstance(tabs,list) or len(tabs)>30:raise ValueError('invalid-search-tabs')
    tabs=[t for t in tabs if isinstance(t,dict) and t.get('tab_type')==11]
    if len(tabs)!=1:raise ValueError('missing-search-tab')
    tab=tabs[0];rows=tab.get('data');more=tab.get('has_more');nxt=tab.get('next_offset',offset)
    if not isinstance(rows,list) or len(rows)>1000 or type(more) not in (bool,int) or more not in (0,1):raise ValueError('invalid-search-page')
    if more and (type(nxt) is not int or not offset<nxt<=50000):raise ValueError('invalid-continuation')
    found={}
    def add(identity,detail):
        sid=str(identity);title=detail.get('series_title')
        if not sid.isascii() or not sid.isdecimal() or not 8<=len(sid)<=24 or not isinstance(title,str) or not title.strip() or len(title)>300:return
        count=detail.get('episode_cnt',0);count=count if type(count) is int and 0<=count<=100000 else 0
        cover=detail.get('series_cover','');cover=cover if isinstance(cover,str) and len(cover)<=8192 else ''
        intro=detail.get('series_intro','');intro=intro[:4000] if isinstance(intro,str) else ''
        found[sid]={'seriesId':sid,'title':title.strip(),'cover':cover,'intro':intro,'actors':[],'tags':[],
            'episodeCount':count,'webAccessibleEpisodes':0,'source':'novelfm'}
    for row in rows:
        if not isinstance(row,dict):continue
        videos=row.get('video_data');videos=[videos] if isinstance(videos,dict) else videos
        if not isinstance(videos,list):continue
        for video in videos[:100]:
            if not isinstance(video,dict) or not isinstance(video.get('video_detail'),dict):continue
            detail=video['video_detail'];add(video.get('series_id',''),detail)
            cards=detail.get('video_detail_list',[])
            if isinstance(cards,list):
                for card in cards[:100]:
                    if isinstance(card,dict):add(card.get('series_id_str') or card.get('series_id',''),card)
    if len(found)>100:raise ValueError('too-many-series')
    if more and not found:raise ValueError('empty-continuation')
    return {'items':list(found.values()),'hasMore':bool(more),'nextOffset':nxt if more else offset}

def search(context,query,offset,mode,client_factory=None):
    url,headers=prepare(context,query,offset,mode)
    return parse(read_response(url,headers,client_factory),offset)

def read_response(url,headers,client_factory=None):
    import requests
    destination=urlsplit(url)
    if destination.scheme!='https' or destination.netloc!=HOST or destination.path not in (PATH,'/reading/user/subscribe/list/v1/') or destination.fragment:raise ValueError('invalid-read-route')
    factory=client_factory or requests.Session
    with factory() as client:
        client.trust_env=False;client.auth=None;client.headers.clear();client.cookies.clear()
        request=client.prepare_request(requests.Request('GET',url,headers=headers))
        if request.url!=url or request.body is not None or client.cookies:raise ValueError('request-changed')
        if client.get_adapter(url).max_retries.total!=0:raise ValueError('retry-prohibited')
        started=time.monotonic()
        with client.send(request,timeout=(5,12),allow_redirects=False,stream=True,verify=True) as response:
            if response.status_code!=200:raise ValueError('search-http-error')
            body=bytearray()
            for chunk in response.iter_content(65536):
                body.extend(chunk)
                if len(body)>LIMIT or time.monotonic()-started>17:raise ValueError('search-response-limit')
            return json.loads(body)

def worker(handler):
    # Paths are bound by the native parent, never taken from provider responses.
    if len(sys.argv)!=3 or sys.argv[1]!='--protocol-root':raise ValueError('invalid-runtime')
    protocol=Path(sys.argv[2]).resolve();sys.path.insert(0,str(protocol))
    args=json.loads(sys.stdin.buffer.read(16385))
    device=Path(args['devicePath']).resolve()
    if device.name!='playback-spare.dpapi' or not device.is_file():raise ValueError('guest-device-unavailable')
    def guard(event,values):
        if event=='open' and isinstance(values[0],str) and values[0].lower().endswith('.dpapi'):
            if Path(values[0]).resolve()!=device:raise ValueError('account-file-prohibited')
    sys.addaudithook(guard)
    from phone_account_device import load_context
    context=load_context(device)
    if not context:raise ValueError('guest-device-unavailable')
    result=handler(context,args)
    sys.stdout.buffer.write(json.dumps({'ok':True,'value':result},ensure_ascii=True).encode())

if __name__=='__main__':
    try:worker(lambda context,args:search(context,args['query'],args['offset'],args['mode']))
    except Exception:
        sys.stdout.buffer.write(b'{"ok":false,"error":"guest-search-unavailable"}')
