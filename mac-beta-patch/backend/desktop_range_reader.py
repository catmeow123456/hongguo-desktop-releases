"""Seekable, bounded CDN input. No account state, disk cache or offline receipt."""
from collections import OrderedDict
import io
import math
import re
import time
from urllib.parse import urljoin,urlsplit


class RangeReadError(OSError):
    def __init__(self, message, *, fallback_allowed=True, source_expired=False, upstream_status=None, origin_reason=None):
        super().__init__(message)
        self.fallback_allowed = fallback_allowed
        self.source_expired = source_expired
        self.diagnostic = ({'http_status': upstream_status}
                           if type(upstream_status) is int and 100 <= upstream_status <= 599 else {})
        if origin_reason in ('invalid','scheme','host','credentials','fragment','port','missing-location'):
            self.diagnostic['origin_reason']=origin_reason


def media_url(value):
    reason=None
    try:
        if not isinstance(value,str) or len(value)>8192:reason='invalid'
        else:
            parsed = urlsplit(value)
            if parsed.scheme!='https':reason='scheme'
            # Keep the previously observed CDN suffix boundary at every hop.
            elif not parsed.hostname or not parsed.hostname.endswith(('.qznovelvod.com','.idouyinvod.com')):reason='host'
            elif parsed.username or parsed.password:reason='credentials'
            elif parsed.fragment:reason='fragment'
            elif parsed.port not in (None,443):reason='port'
    except (ValueError,TypeError):
        reason='invalid'
    if reason:raise RangeReadError('Unsupported media origin',origin_reason=reason)
    return value


def media_redirect(current,location,*,upstream_status=None,range_start=None,range_end=None,redirect_hop=None):
    try:
        if not isinstance(location,str) or not location.strip():
            raise RangeReadError('Unsupported media origin',origin_reason='missing-location')
        if len(location)>8192:
            raise RangeReadError('Unsupported media origin',origin_reason='invalid')
        try:target=urljoin(current,location)
        except (ValueError,TypeError):
            raise RangeReadError('Unsupported media origin',origin_reason='invalid') from None
        return media_url(target)
    except RangeReadError as error:
        # Reproduce the failing request without retaining its URL, host, signed
        # query or validator. A one-byte probe need not follow the same CDN route.
        if type(upstream_status) is int and upstream_status in (301,302,303,307,308):
            error.diagnostic['http_status']=upstream_status
        if (type(range_start) is int and type(range_end) is int
                and 0<=range_start<=range_end<1024**3):
            error.diagnostic.update(range_start=range_start,range_end=range_end)
        if type(redirect_hop) is int and 0<=redirect_hop<=2:
            error.diagnostic['redirect_hop']=redirect_hop
        raise


class RangeReader(io.RawIOBase):
    BLOCK = 256*1024

    def __init__(self, request, url, size, *, cancelled=lambda:False,
                 cache_bytes=8*1024*1024, clock=time.monotonic):
        super().__init__()
        if type(size) is not int or not 0<size<=1024**3:
            raise RangeReadError('Unsupported media size')
        if type(cache_bytes) is not int or not self.BLOCK<=cache_bytes<=16*1024**2:
            raise RangeReadError('Invalid media cache budget')
        self._url=media_url(url)
        self._request=request
        self.size=size
        self._cancelled=cancelled;self._clock=clock
        self._deadline=clock()+15
        self._cache=OrderedDict();self._capacity=cache_bytes//self.BLOCK
        self._position=0;self._etag=None;self._failure=None
        self.received=0;self.requests=0;self.cache_high_water=0
        self._max_bytes=min(size,4*1024**2)
        self._max_requests=20
        self._prepared=False
        self._stream=None;self._stream_next=0;self._sequential=False

    @property
    def cached_bytes(self):return sum(map(len,self._cache.values()))

    @property
    def fallback_allowed(self):
        return self._failure is None or self._failure.fallback_allowed

    @property
    def source_expired(self):
        return self._failure is not None and self._failure.source_expired

    @property
    def recoverable_failure(self):
        return self._failure is not None and self._failure.fallback_allowed

    def allow_full_playback(self):
        self._check()
        if self._prepared:raise RangeReadError('Input already prepared')
        self._prepared=True
        # Eviction/reopening may reread headers or interleaved audio. Bound
        # total transfer separately from bounded resident bytes.
        self._max_bytes=2*self.size+8*1024**2
        self._max_requests=math.ceil(self._max_bytes/self.BLOCK)+8
        self._deadline=self._clock()+1800

    def readable(self):return True
    def seekable(self):return True
    def tell(self):return self._position

    def _check(self):
        if self.closed:raise RangeReadError('Media input closed',fallback_allowed=False)
        if self._failure is not None:raise self._failure
        if self._cancelled():
            self._failure=RangeReadError('Media input cancelled',fallback_allowed=False)
            raise self._failure
        if self._clock()>=self._deadline:
            self._failure=RangeReadError('Media read time budget exhausted')
            raise self._failure

    def seek(self, offset, whence=0):
        self._check()
        if type(offset) is not int or whence not in (0,1,2):
            raise RangeReadError('Invalid media seek')
        target=offset+(0 if whence==0 else self._position if whence==1 else self.size)
        if target<0:raise RangeReadError('Negative media seek')
        self._position=target
        return target

    def read(self, size=-1):
        self._check()
        if type(size) is not int or size<0:
            raise RangeReadError('A bounded media read is required')
        # RawIO is allowed to return fewer bytes than requested. Never allocate
        # an entire media file because a consumer asks for a large read.
        end=min(self.size,self._position+min(size,1024**2))
        output=bytearray()
        while self._position<end:
            index,offset=divmod(self._position,self.BLOCK)
            chunk=self._chunk(index)
            part=chunk[offset:offset+end-self._position]
            output.extend(part);self._position+=len(part)
        return bytes(output)

    def readinto(self, target):
        data=self.read(len(target));target[:len(data)]=data
        return len(data)

    def _chunk(self,index):
        self._check()
        if index in self._cache:
            self._cache.move_to_end(index)
            return self._cache[index]
        try:
            data=self._fetch(index)
            self._remember(index,data)
            return data
        except Exception as error:
            # Third-party exceptions may contain signed URLs. Only keep a
            # fixed failure category; a decoder's repeated read never retries.
            self._failure=(error if isinstance(error,RangeReadError)
                           else RangeReadError('Media range transfer failed'))
            self._close_stream()
            raise self._failure from None

    def _remember(self,index,data):
        self._cache[index]=data
        while len(self._cache)>self._capacity:
            oldest=next(iter(self._cache))
            # MOV probes reopen the header. Keep that block in the same memory
            # budget while a single response supplies all remaining bytes.
            if self._sequential and oldest==0:
                oldest=next(key for key in self._cache if key!=0)
            del self._cache[oldest]
        self.cache_high_water=max(self.cache_high_water,self.cached_bytes)

    def _close_stream(self):
        response=self._stream;self._stream=None
        if response is not None:response.close()

    def _open_sequential(self):
        for hop in range(3):
            self._check()
            if self.requests>=self._max_requests:raise RangeReadError('Media request budget exhausted')
            self.requests+=1
            remaining=self._deadline-self._clock()
            response=self._request('GET',self._url,
                headers={'Range':f'bytes=0-{self.size-1}','Accept-Encoding':'identity'},
                stream=True,allow_redirects=False,timeout=(min(4,remaining/2),min(8,remaining/2)))
            keep=False
            try:
                status=response.status_code
                if status in (401,429):raise RangeReadError('Media request rejected',fallback_allowed=False,upstream_status=status)
                if status in (403,404,410):
                    raise RangeReadError('Media address unavailable',source_expired=True,upstream_status=status)
                if status in (301,302,303,307,308):
                    redirected=media_redirect(self._url,response.headers.get('Location',''),
                        upstream_status=status,range_start=0,range_end=self.size-1,redirect_hop=hop)
                    if hop==2 or redirected==self._url:raise RangeReadError('Media redirect budget exhausted')
                    self._url=redirected;continue
                if (status not in (200,206)
                        or int(response.headers.get('Content-Length','-1'))!=self.size
                        or response.headers.get('Content-Encoding','identity').lower()!='identity'
                        or (status==206 and response.headers.get('Content-Range')!=f'bytes 0-{self.size-1}/{self.size}')):
                    raise RangeReadError('Media range representation changed',upstream_status=status)
                self._stream=response;keep=True;return
            finally:
                if not keep:response.close()

    def _stream_chunk(self,index):
        if index<self._stream_next:raise RangeReadError('Media backward seek unavailable')
        if self._stream is None:
            if self._stream_next:raise RangeReadError('Media input stream ended')
            self._open_sequential()
        while self._stream_next<=index:
            self._check()
            count=min(self.BLOCK,self.size-self._stream_next*self.BLOCK)
            if count<=0 or self.received+count>self._max_bytes:raise RangeReadError('Media byte budget exhausted')
            chunks=bytearray()
            while len(chunks)<count:
                self._check()
                part=self._stream.raw.read(min(32768,count-len(chunks)),decode_content=False)
                if not part:raise RangeReadError('Incomplete media range')
                self.received+=len(part);chunks.extend(part)
                if len(chunks)>count:raise RangeReadError('Oversized media range')
            self._check()
            data=bytes(chunks);self._remember(self._stream_next,data);self._stream_next+=1
        if self._stream_next*self.BLOCK>=self.size:self._close_stream()
        return data

    def _fetch(self,index):
        if self._sequential:return self._stream_chunk(index)
        start=index*self.BLOCK;end=min(self.size,start+self.BLOCK)-1
        count=end-start+1
        if count<=0 or self.received+count>self._max_bytes:
            raise RangeReadError('Media byte budget exhausted')
        headers={'Range':f'bytes={start}-{end}','Accept-Encoding':'identity'}
        if self._etag:headers['If-Match']=self._etag
        for hop in range(3):
            self._check()
            if self.requests>=self._max_requests:
                raise RangeReadError('Media request budget exhausted')
            self.requests+=1
            remaining=self._deadline-self._clock()
            with self._request('GET',self._url,headers=headers,stream=True,
                    allow_redirects=False,timeout=(min(4,remaining/2),min(8,remaining/2))) as response:
                status=response.status_code
                if status in (401,429):
                    raise RangeReadError('Media request rejected',fallback_allowed=False,upstream_status=status)
                if status in (403,404,410):
                    raise RangeReadError('Media address unavailable',source_expired=True,upstream_status=status)
                if status in (301,302,303,307,308):
                    redirected=media_redirect(self._url,response.headers.get('Location',''),
                        upstream_status=status,range_start=start,range_end=end,redirect_hop=hop)
                    if hop==2 or redirected==self._url:
                        raise RangeReadError('Media redirect budget exhausted')
                    self._url=redirected
                    continue
                if status!=206:raise RangeReadError('Media range unavailable',upstream_status=status)
                if (response.headers.get('Content-Range')!=f'bytes {start}-{end}/{self.size}'
                        or response.headers.get('Content-Encoding','identity').lower()!='identity'
                        or int(response.headers.get('Content-Length','-1'))!=count):
                    raise RangeReadError('Media range representation changed')
                etag=response.headers.get('ETag','')
                if (not re.fullmatch(r'"[\x21\x23-\x7e]{1,256}"',etag)
                        or (self._etag is not None and etag!=self._etag)):
                    if self._etag is None and self.received==0:
                        # Do not combine unvalidated ranges. Discard this body
                        # and obtain every byte from one continuous response.
                        self._sequential=True
                        break
                    raise RangeReadError('Media strong validator unavailable or changed')
                self._etag=etag
                chunks=bytearray()
                while len(chunks)<count:
                    self._check()
                    part=response.raw.read(min(32768,count-len(chunks)),decode_content=False)
                    if not part:raise RangeReadError('Incomplete media range')
                    self.received+=len(part);chunks.extend(part)
                    if len(chunks)>count:raise RangeReadError('Oversized media range')
                self._check()
                return bytes(chunks)
        if self._sequential:return self._stream_chunk(index)
        raise RangeReadError('Media redirect budget exhausted')

    def close(self):
        if hasattr(self,'_stream'):self._close_stream()
        if hasattr(self,'_cache'):self._cache.clear()
        self._url='';self._etag=None
        self._request=None
        super().close()
