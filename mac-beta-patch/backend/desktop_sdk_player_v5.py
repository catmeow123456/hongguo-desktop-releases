"""Deployable anonymous V5 model reader; existing accounts are never loaded."""
import base64
import copy
import json
from collections import OrderedDict
from pathlib import Path
import re
import threading
import time

from desktop_spare_player import SpareError
from desktop_video_model import parse_video_model


def media_connection_not_established(error):
    from requests.exceptions import ConnectionError, ConnectTimeout, ProxyError, SSLError
    from urllib3.exceptions import MaxRetryError, NewConnectionError

    if isinstance(error, (ProxyError, SSLError)):
        return False
    if isinstance(error, ConnectTimeout):
        return True
    return isinstance(error, ConnectionError) and any(
        isinstance(cause, NewConnectionError)
        or isinstance(cause, MaxRetryError) and isinstance(cause.reason, NewConnectionError)
        for cause in error.args
    )


class Reader:
    def __init__(self, directory, *, allow_initialize=True, sender=None):
        self.root=Path(directory)
        if not self.root.is_absolute() or not self.root.is_dir() or self.root.is_symlink():
            raise SpareError('invalid-v5-data-directory')
        self.allow_initialize=allow_initialize
        self.sender=sender
        self.lock=threading.RLock()
        self.models=OrderedDict()
        self.cooldowns={}
        self.assets=None; self.engine=None; self.engine_uses=0
        self.binding=None; self.session=None; self.ladon=0
        self.stats={'state':'idle','attempts':0,'failures':0,'cacheHits':0,'sdkExchanges':0}

    def _save_diagnostic(self):
        path=self.root/'playback-v5-diagnostic.json'
        temp=path.with_suffix('.tmp')
        try:
            for p in (path,temp):
                if p.is_symlink() or p.exists() and (not p.is_file() or p.stat().st_nlink!=1): return
            temp.write_text(json.dumps(self.stats),encoding='utf8');temp.replace(path)
        except OSError: pass

    def _core(self, payload, mode, seconds):
        from phone_sdk_core_v5 import SdkDigestCore
        # The emulator uses a bounded arena. Recreate before a long-lived
        # player can exhaust it; network reads are never replayed on failure.
        if self.engine is None or self.engine_uses>=16 or self.engine.heap>=self.engine.HEAP+0x600000:
            self.engine=None
            self.engine=SdkDigestCore(self.assets.image);self.engine_uses=0
        self.engine_uses+=1
        try:return self.engine.digest(payload,mode,seconds)[0][:16]
        except Exception:
            self.engine=None;raise

    def _context(self):
        from phone_account_device import load_context
        from phone_sdk_session_v5 import device_binding
        device=self.root/'playback-spare.dpapi'
        if not device.is_file() or device.is_symlink() or device.stat().st_nlink!=1:
            raise SpareError('v5-device-required')
        context=load_context(device)
        binding=device_binding(context)
        if self.binding is not None and self.binding!=binding:
            # Never reuse a previous device's model or SDK session.
            self.models.clear();self.session=None;self.engine=None;self.ladon=0
        self.binding=binding
        return context

    def _prepare(self, video, context):
        from phone_sdk_assets_v5 import load_assets, SdkAssetsError
        from requests import RequestException
        from phone_sdk_materials_v5 import decode_app_config,read_sdk_image
        from phone_sdk_session_v5 import SessionStore,send_prepared
        from phone_playback_request_v5 import prepare_playback_v5
        if self.assets is None:
            try:
                self.assets=load_assets(self.root/'playback-sdk'/'74332',allow_download=self.allow_initialize)
            except (SdkAssetsError, RequestException) as error:
                # A failed immutable-material read has no SDK exchange side effect.
                # Do not conflate this with unknown/rejected session exchanges.
                recoverable=isinstance(error,RequestException) or str(error) in (
                    'sdk-materials-not-installed','sdk-material-range-unavailable',
                    'sdk-material-transfer-limit','sdk-material-redirect-limit')
                self.stats['materialState']='unavailable' if recoverable else 'invalid'
                raise SpareError('sdk-material-unavailable' if recoverable else 'sdk-material-invalid') from None
            self.stats['materialState']='ready'
        materials,image=decode_app_config(self.assets.configuration),read_sdk_image(self.assets.image)
        if self.session is None:
            def sdk_send(request):
                self.stats['sdkExchanges']+=1
                return (self.sender or send_prepared)(request)
            store=SessionStore(self.root,sender=sdk_send)
            self.session=store.ensure(context,self.assets,core=self._core,allow_exchange=self.allow_initialize)
            self.ladon=self.session['ladon']
        request=prepare_playback_v5(context,video,int(time.time()*1000),token=self.session['token'],
            previous_ladon=self.ladon,materials=materials,image=image,core=self._core)
        self.ladon=request.next_ladon
        return request

    def preparation_status(self):
        """Local evidence only; checking status never initializes or resets a device."""
        from phone_sdk_session_v5 import SessionStore, SdkSessionError
        with self.lock:
            try:return SessionStore(self.root).review(self._context())
            except SdkSessionError as error:
                state='busy' if str(error)=='sdk-state-busy' else 'review-required'
            except Exception:
                state='review-required'
            return {'state':state,'canContinue':False,'canPrepareNewAttempt':False}

    def prepare_recovery(self,ticket,*,confirmed=False):
        from phone_sdk_session_v5 import SessionStore, SdkSessionError
        from phone_sdk_recovery_v5 import prepare
        with self.lock:
            if self.session is not None:raise SdkSessionError('sdk-recovery-not-needed')
            result=prepare(SessionStore(self.root),self._context(),ticket,confirmed=confirmed)
            self.cooldowns.clear();self.engine=None;self.engine_uses=0;self.ladon=0
            self.stats.update(state='idle',recoveryState='prepared');self._save_diagnostic()
            return result

    def read_model(self, video):
        from phone_sdk_session_v5 import send_prepared
        from desktop_codec_fallback import recover_model
        from desktop_fresh_playback import lifetime
        video=str(video)
        if not re.fullmatch(r'[1-9][0-9]{7,23}',video):raise SpareError('invalid-episode')
        with self.lock:
            context=self._context()
            cached=self.models.get(video)
            if cached and cached[0]>time.monotonic():
                self.stats['cacheHits']+=1;self._save_diagnostic();return copy.deepcopy(cached[1])
            if self.cooldowns.get(video,0)>time.monotonic():raise SpareError('v5-read-cooldown')
            established = self.session is not None
            media_connect_failure = False
            try:
                request=self._prepare(video,context)
                self.stats.update(state='request-in-flight',attempts=self.stats['attempts']+1);self._save_diagnostic()
                try:
                    data=(self.sender or send_prepared)(request)
                except Exception as error:
                    media_connect_failure = established and media_connection_not_established(error)
                    raise
                value=json.loads(data)
                if value.get('code')!=0:raise SpareError('service-rejected')
                model=parse_video_model({'code':0,'data':value['data'][video]})
                model=recover_model(model,agent=request.headers['User-Agent'])
                self.models[video]=(time.monotonic()+lifetime(model,time.time()),copy.deepcopy(model))
                self.models.move_to_end(video)
                while len(self.models)>8:self.models.popitem(last=False)
                self.stats.update(state='ready',recoveryState='ready',cachedModels=len(self.models),tracks=len(model['video_list']))
                self._save_diagnostic();return model
            except Exception as error:
                # A validated first-step response plus no second-step claim
                # means preparation stopped locally. Let the existing Retry
                # action continue it immediately, without retrying transport
                # whose outcome is unknown or bypassing a server rejection.
                continuable=False
                recovery_state='ready' if self.session is not None else 'review-required'
                if self.session is None:
                    try:
                        from phone_sdk_session_v5 import SessionStore
                        review=SessionStore(self.root).review(context)
                        continuable=review.get('canContinue') is True
                        recovery_state=review['state']
                    except Exception:pass
                now = time.monotonic()
                self.cooldowns={k:v for k,v in self.cooldowns.items() if v>now}
                if not continuable:self.cooldowns[video]=now+(2 if media_connect_failure else 30)
                self.stats.update(state='continuation-ready' if continuable else 'failed',recoveryState=recovery_state,failures=self.stats['failures']+1,errorType=type(error).__name__)
                self._save_diagnostic()
                if continuable:raise SpareError('sdk-continuation-ready') from None
                if media_connect_failure:raise SpareError('media-connect-unavailable') from None
                if isinstance(error,SpareError):raise
                raise SpareError('v5-model-unavailable') from None

    def __call__(self, directory, video):
        if Path(directory).resolve()!=self.root.resolve():raise SpareError('v5-profile-mismatch')
        return {'code':0,'data':{'video_model':self.read_model(video)}}

    def reject(self, video, address):
        with self.lock:
            cached=self.models.get(str(video))
            if not cached:return False
            rows=cached[1].get('video_list',{})
            for row in rows.values() if isinstance(rows,dict) else rows:
                if not isinstance(row,dict):continue
                for key in ('main_url','backup_url'):
                    raw=row.get(key)
                    if not isinstance(raw,str):continue
                    try:url=raw if raw.startswith(('https://','http://')) else base64.b64decode(raw+'='*(-len(raw)%4)).decode()
                    except (ValueError,UnicodeError):continue
                    if url==address:
                        self.models.pop(str(video),None);return True
            return False
