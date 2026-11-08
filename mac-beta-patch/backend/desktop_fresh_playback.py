"""Bounded anonymous recovery with process-only, series-scoped model caching.

Original playback remains first. No saved account/device files are read or
changed, no background worker or automatic retry, and no identity pool is kept.
"""
import base64
import copy
import json
import re
import threading
import time
from collections import OrderedDict,deque
from pathlib import Path
from urllib.parse import urlsplit,parse_qs
import requests
from desktop_spare_player import SpareError
from desktop_video_model import parse_video_model

def exchange(url,headers,body,*,limit=2*1024*1024,client_factory=requests.Session):
    with client_factory() as client:
        client.trust_env=False;client.auth=None;client.cookies.clear();client.headers.clear()
        request=client.prepare_request(requests.Request('POST',url,headers=headers,data=body))
        if request.url!=url or request.body!=body:raise SpareError('encoding-changed')
        with client.send(request,timeout=(8,20),verify=True,allow_redirects=False,stream=True) as response:
            if response.status_code!=200:raise SpareError('http-rejected')
            data=bytearray();started=time.monotonic()
            for chunk in response.iter_content(32768):
                data.extend(chunk)
                if len(data)>limit or time.monotonic()-started>45:raise SpareError('response-limit')
            if not data:raise SpareError('empty-response')
            from phone_account_device import registration_cookies
            return bytes(data),registration_cookies(response,client)

def fetch_batch(videos):
    from phone_account_device import prepare_registration,parse_identity,USER_AGENT
    from desktop_playback_request import prepare_fresh
    try:
        url,body,headers,params=prepare_registration()
        data,cookies=exchange(url,headers,body)
        issued=parse_identity(data)
        params.update(device_id=issued['device_id'],iid=issued['iid'])
        context={'params':params,'cookies':cookies,'device_token':issued['device_token'],'user_agent':USER_AGENT}
        url,headers,body=prepare_fresh(context,videos,int(time.time()*1000))
        from phone_account_transport import region_headers
        headers.update(region_headers(cookies))
        data,_=exchange(url,headers,body)
        value=json.loads(data)
        if not isinstance(value,dict) or type(value.get('code')) is not int or value['code']!=0:
            raise SpareError('service-rejected')
        rows=value.get('data');result={}
        if not isinstance(rows,dict):raise SpareError('invalid-model')
        for video in videos:
            entry=rows.get(video)
            if not isinstance(entry,dict):continue
            try:result[video]=parse_video_model({'code':0,'data':entry})
            except ValueError:continue
        if not result:raise SpareError('invalid-model')
        return result
    except SpareError:raise
    except Exception:raise SpareError('transport-or-response') from None

def lifetime(model,now):
    deadlines=[]
    def accept(value):
        try:
            stamp=float(value)
            if stamp>1e12:stamp/=1000
            if stamp>1e9:deadlines.append(stamp)
        except (ValueError,TypeError):pass
    for key in ('expire_time','expires','expire_timestamp'):accept(model.get(key))
    rows=model.get('video_list',{})
    for row in (rows.values() if isinstance(rows,dict) else rows):
        if not isinstance(row,dict):continue
        for key in ('main_url','backup_url'):
            raw=row.get(key)
            if not isinstance(raw,str):continue
            try:
                url=raw if raw.startswith('https://') else base64.b64decode(raw+'='*(-len(raw)%4)).decode()
                query=parse_qs(urlsplit(url).query)
                for name in ('x-expires','expires','expire','expiration'):
                    for value in query.get(name,[]):accept(value)
            except (ValueError,UnicodeError):continue
    return max(0,min(900,min(deadlines)-now-120)) if deadlines else 300

class Recovery:
    def __init__(self,*,fetch=fetch_batch,clock=time.monotonic,wall=time.time):
        self.fetch=fetch;self.clock=clock;self.wall=wall
        self.lock=threading.RLock();self.fetch_lock=threading.Lock()
        self.series=OrderedDict();self.models=OrderedDict()
        self.attempts=deque();self.failures=0;self.next_allowed=0.;self.active_until=0.

    def observe(self,series,episodes):
        if not isinstance(episodes,list):return
        ids=[str(e.get('vid','')) for e in episodes if isinstance(e,dict)]
        ids=[v for v in ids if re.fullmatch(r'[1-9][0-9]{7,23}',v)]
        if not ids:return
        with self.lock:
            self.series[str(series)]=list(dict.fromkeys(ids))[:10000];self.series.move_to_end(str(series))
            while len(self.series)>8:self.series.popitem(last=False)

    def cached(self,video):
        with self.lock:
            value=self.models.get(video)
            if value and value[0]>self.clock():return copy.deepcopy(value[1])
            self.models.pop(video,None)
        return None

    def reject(self,video,address):
        """Discard only the model that actually supplied a rejected address.

        A late failure must not remove a newer model or reset fetch budgets.
        No request or saved device/account mutation occurs here.
        """
        if not isinstance(address,str) or not address:return False
        with self.lock:
            entry=self.models.get(str(video))
            if entry is None:return False
            rows=entry[1].get('video_list',{})
            for row in (rows.values() if isinstance(rows,dict) else rows):
                if not isinstance(row,dict):continue
                for key in ('main_url','backup_url'):
                    raw=row.get(key)
                    if not isinstance(raw,str):continue
                    try:url=raw if raw.startswith(('https://','http://')) else base64.b64decode(raw+'='*(-len(raw)%4)).decode()
                    except (ValueError,UnicodeError):continue
                    if url==address:
                        self.models.pop(str(video),None)
                        return True
        return False

    def resolve(self,video):
        cached=self.cached(video)
        if cached is not None:return cached
        # Serialize only recovery fetches. The state lock must stay available
        # to cached playback and the original episode-list observer during I/O.
        with self.fetch_lock:
            with self.lock:
                cached=self.cached(video)
                if cached is not None:return cached
                now=self.clock()
                while self.attempts and now-self.attempts[0]>=600:self.attempts.popleft()
                if now<self.next_allowed or len(self.attempts)>=3:raise SpareError('recovery-cooldown')
                videos=[video]
                for ids in reversed(self.series.values()):
                    if video in ids:
                        start=ids.index(video);videos=ids[start:start+15];break
                self.attempts.append(now);self.next_allowed=now+3
            try:
                rows=self.fetch(videos)
                if video not in rows:raise SpareError('invalid-model')
                with self.lock:
                    self.active_until=self.clock()+120;self.failures=0
                    for vid,model in rows.items():
                        if vid not in videos:continue
                        ttl=lifetime(model,self.wall())
                        if ttl>0:
                            self.models[vid]=(self.clock()+ttl,copy.deepcopy(model));self.models.move_to_end(vid)
                    while len(self.models)>64:self.models.popitem(last=False)
                return copy.deepcopy(rows[video])
            except Exception:
                with self.lock:
                    self.failures+=1;self.next_allowed=self.clock()+min(120,15*2**min(self.failures-1,3))
                raise

def attach(client,old_reader):
    recovery=Recovery()
    client._desktop_reject_media_model=recovery.reject
    original=getattr(client,'get_episodes',None)
    if callable(original):
        def episodes(series,*args,**kwargs):
            result=original(series,*args,**kwargs)
            if isinstance(result,(tuple,list)) and len(result)==2:recovery.observe(series,result[1])
            return result
        client.get_episodes=episodes
    def read(directory,video):
        model=recovery.cached(video)
        if model is None:
            if recovery.clock()>=recovery.active_until:
                try:return old_reader(directory,video)
                except SpareError as error:
                    if error.reason not in ('empty-response','invalid-model'):raise
            model=recovery.resolve(video)
        from desktop_codec_fallback import recover_model,CodecCompatibilityError
        from desktop_playback_request import AGENT
        try:model=recover_model(model,agent=AGENT)
        except CodecCompatibilityError:raise SpareError('unsupported-codec') from None
        return {'code':0,'data':{'video_model':model}}
    def observed(directory,video):
        state='ready'
        try:return read(directory,video)
        except SpareError as error:
            state=error.reason;raise
        except Exception:
            state='local-error';raise
        finally:
            # Numeric and typed diagnostics only; no identity, URL or content.
            try:
                root=Path(directory);path=root/'playback-fresh-diagnostic.json';tmp=path.with_suffix('.tmp')
                if root.is_absolute() and root.is_dir() and not any(p.is_symlink() or p.exists() and p.stat().st_nlink!=1 for p in (path,tmp)):
                    with recovery.lock:
                        tmp.write_text(json.dumps({'state':state,'batchAttempts':len(recovery.attempts),'cachedModels':len(recovery.models),'failedBatches':recovery.failures}),encoding='utf8')
                        tmp.replace(path)
            except OSError:pass
    return observed
