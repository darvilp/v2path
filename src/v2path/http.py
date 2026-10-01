"""Outbound HTTP/1.1 delivery using the standard library, without implicit retries."""
from __future__ import annotations

import asyncio
import http.client
import inspect
import re
import socket
import ssl as tls
import threading
import time
from dataclasses import dataclass, field, replace
from urllib.parse import urlsplit

from .ack import read_ack, value
from .core import Message, parse
from .delivery import DeliveryResult, RetryPolicy, HTTPResponse

_TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
_RESERVED = {'host', 'content-length', 'transfer-encoding', 'connection', 'expect', 'upgrade', 'trailer'}
_DIAGNOSTIC_HEADERS = {'content-type', 'retry-after', 'date', 'x-request-id'}


def _headers(*sources):
    merged = {}
    for source in sources:
        for name, val in source.items():
            if not isinstance(name, str) or not _TOKEN.fullmatch(name):
                raise ValueError('Invalid HTTP header name')
            if name.lower() in _RESERVED:
                raise ValueError('Transport-controlled HTTP header')
            if not isinstance(val, str) or any(ord(c) < 32 or ord(c) == 127 for c in val):
                raise ValueError('Invalid HTTP header value')
            val.encode('latin-1')
            merged[name.lower()] = val
    return merged


def _target(url, method):
    if not isinstance(url, str) or any(ord(c) <= 32 or ord(c) == 127 for c in url):
        raise ValueError('Invalid HTTP URL')
    parts = urlsplit(url)
    if parts.scheme not in {'http', 'https'} or not parts.hostname or parts.username is not None or parts.password is not None or parts.fragment:
        raise ValueError('Use an HTTP(S) URL without credentials or a fragment')
    port = parts.port if parts.port is not None else (443 if parts.scheme == 'https' else 80)
    if port == 0:
        raise ValueError('HTTP destination port must be nonzero')
    if not isinstance(method, str) or not _TOKEN.fullmatch(method):
        raise ValueError('Invalid HTTP method')
    method = method.upper()
    if method in {'CONNECT', 'TRACE'}:
        raise ValueError('HTTP tunnelling and TRACE are not delivery methods')
    path = (parts.path or '/') + ('?' + parts.query if parts.query else '')
    path.encode('ascii')  # Caller percent-encodes non-ASCII URL components.
    return parts, port, path, method


class _HTTPResponse(http.client.HTTPResponse):
    def _read_status(self):
        while True:
            version, status, reason = super()._read_status()
            if not 100 <= status < 200 or status == 101:
                return version, status, reason
            # http.client skips 100 itself, but otherwise exposes 102/103 as final.
            # Consume interim headers on this same stream and response deadline.
            http.client.parse_headers(self.fp)


@dataclass(frozen=True)
class HTTPRequest:
    """Frozen logical delivery. Credential headers are resolved separately."""
    url: str
    method: str
    body: bytes
    message: bytes
    encoding: str


@dataclass(frozen=True)
class HTTPDestination:
    url: str
    method: str = 'POST'
    headers: dict = field(default_factory=dict)
    encoder: object = None
    header_provider: object = None
    response: object = 'status'
    options: dict = field(default_factory=dict)

    def __post_init__(self):
        _target(self.url, self.method)
        _headers(self.headers)


class HTTPClient:
    def __init__(self, url, *, method='POST', headers=None, encoder=None,
                 header_provider=None, response='status', accepted_statuses=None,
                 ssl=None, connect_timeout=10, send_timeout=30, response_timeout=30,
                 max_request_bytes=16*1024*1024, max_response_bytes=16*1024*1024,
                 retry=None):
        self._parts, self._port, self._path, self.method = _target(url, method)
        if min(connect_timeout, send_timeout, response_timeout, max_request_bytes, max_response_bytes) <= 0:
            raise ValueError('HTTP limits and timeouts must be positive')
        if response not in ('status', 'hl7_ack') and not callable(response):
            raise ValueError('Response policy must be status, hl7_ack or a callback')
        for callback in (encoder, header_provider):
            if callback is not None and not callable(callback):
                raise TypeError('HTTP callbacks must be callable')
        self.url = url
        self.headers = _headers({'Content-Type': 'application/hl7-v2'}, headers or {})
        self.encoder, self.header_provider, self.response = encoder, header_provider, response
        self.accepted_statuses = frozenset(range(200, 300) if accepted_statuses is None else accepted_statuses)
        if any(not isinstance(s, int) or not 100 <= s <= 599 for s in self.accepted_statuses):
            raise ValueError('Invalid accepted HTTP status')
        if ssl is not None and not isinstance(ssl, tls.SSLContext):
            raise TypeError('ssl must be an SSLContext')
        self.ssl = ssl if ssl is not None else tls.create_default_context() if self._parts.scheme == 'https' else None
        self.connect_timeout, self.send_timeout, self.response_timeout = connect_timeout, send_timeout, response_timeout
        self.max_request_bytes, self.max_response_bytes = max_request_bytes, max_response_bytes
        self.retry = retry or RetryPolicy()
        self._lock = asyncio.Lock()
        self._conn = self._socket = None
        self._abort = threading.Event()
        self._closing = False
        self._closed_event = asyncio.Event()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    def _interrupt(self):
        self._abort.set()
        sock = self._socket
        if sock is not None:
            try:
                socket.socket.shutdown(sock, socket.SHUT_RDWR)
            except OSError:
                pass

    def _disconnect(self):
        if self._conn is not None:
            self._conn.close()
        self._conn = self._socket = None

    async def close(self):
        self._closing = True
        self._closed_event.set()
        self._interrupt()
        async with self._lock:
            self._disconnect()

    async def prepare(self, message):
        original = message.copy() if isinstance(message, Message) else parse(bytes(message))
        raw = original.to_bytes()
        body = self.encoder(original.copy()) if self.encoder else raw
        if inspect.isawaitable(body):
            body = await body
        if not isinstance(body, bytes):
            raise TypeError('An HTTP encoder must return bytes')
        if len(body) > self.max_request_bytes:
            raise ValueError('HTTP request exceeds configured limit')
        if self.response == 'hl7_ack' and not value(original, 'MSH-10'):
            raise ValueError('HTTP ACK policy requires a control ID')
        return HTTPRequest(self.url, self.method, body, raw, original.encoding)

    async def send(self, message):
        try:
            request = await self.prepare(message)
        except Exception as exc:
            return DeliveryResult('request_error', detail=type(exc).__name__)
        return await self.send_prepared(request)

    async def send_prepared(self, request, *, retry=None):
        if request.url != self.url or request.method != self.method:
            raise ValueError('Frozen HTTP request does not match configured target/method')
        if not isinstance(request.body, bytes) or len(request.body) > self.max_request_bytes:
            return DeliveryResult('request_error', detail='Invalid frozen request body or size')
        try:
            original = parse(request.message, encoding=request.encoding)
            if self.response == 'hl7_ack' and not value(original, 'MSH-10'):
                raise ValueError('HTTP ACK policy requires a control ID')
        except Exception as exc:
            return DeliveryResult('request_error', detail=type(exc).__name__)
        policy = self.retry if retry is None else retry
        async with self._lock:
            if self._closing:
                return DeliveryResult('connection_error', detail='HTTP client is closed')
            attempt = 0
            while True:
                attempt += 1
                try:
                    dynamic = self.header_provider(original.copy(), request.body, attempt) if self.header_provider else {}
                    if inspect.isawaitable(dynamic):
                        dynamic = await dynamic
                    headers = _headers(self.headers, dynamic)
                except Exception as exc:
                    result = DeliveryResult('request_error', detail=type(exc).__name__)
                else:
                    if self._closing:
                        return DeliveryResult('connection_error', detail='HTTP client is closed', attempts=attempt)
                    self._abort.clear()
                    worker = asyncio.create_task(asyncio.to_thread(self._exchange, request, headers))
                    try:
                        result = await asyncio.shield(worker)
                    except asyncio.CancelledError:
                        self._interrupt()
                        # A thread must finish before releasing ordering/connection ownership.
                        while not worker.done():
                            try:
                                await asyncio.shield(worker)
                            except asyncio.CancelledError:
                                continue
                        self._disconnect()
                        raise
                    if result.http_response is not None and result.outcome == 'received':
                        result = await self._evaluate(result.http_response, original)
                result = replace(result, attempts=attempt)
                rule = policy.for_result(result)
                if self._closing or attempt >= rule.attempts:
                    return result
                try:
                    await asyncio.wait_for(self._closed_event.wait(), rule.delay)
                    return result
                except asyncio.TimeoutError:
                    pass

    def _connect_socket(self):
        deadline = time.monotonic() + self.connect_timeout
        # getaddrinfo cannot be interrupted by Python. Retain ownership of this
        # worker until it returns; an elapsed deadline prevents subsequent I/O.
        addresses = socket.getaddrinfo(self._parts.hostname, self._port, type=socket.SOCK_STREAM)
        error = None
        for family, kind, protocol, _, address in addresses:
            remaining = deadline - time.monotonic()
            if self._abort.is_set() or remaining <= 0:
                raise TimeoutError('HTTP connection deadline elapsed')
            sock = socket.socket(family, kind, protocol)
            self._socket = sock
            try:
                sock.settimeout(remaining)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                sock.connect(address)
                if self._abort.is_set():
                    raise TimeoutError('HTTP connection interrupted')
                if self._parts.scheme == 'https':
                    sock = self.ssl.wrap_socket(sock, server_hostname=self._parts.hostname,
                                                do_handshake_on_connect=False)
                    self._socket = sock
                    if self._abort.is_set():
                        raise TimeoutError('TLS handshake interrupted')
                    sock.do_handshake()
                return sock
            except OSError as exc:
                sock.close()
                self._socket = None
                error = exc
        raise error or OSError('No HTTP address resolved')

    def _exchange(self, request, headers):
        sent = False
        response = None
        snapshot = None
        pairs = ()
        body = b''
        timer = None
        def deadline(seconds):
            nonlocal timer
            if timer:
                timer.cancel()
                timer.join()
            if self._abort.is_set():
                raise TimeoutError('HTTP operation interrupted')
            timer = threading.Timer(seconds, self._interrupt)
            timer.daemon = True
            timer.start()
        try:
            if self._conn is None:
                deadline(self.connect_timeout)
                if self._parts.scheme == 'https':
                    self._conn = http.client.HTTPSConnection(self._parts.hostname, self._port,
                        timeout=self.connect_timeout, context=self.ssl)
                else:
                    self._conn = http.client.HTTPConnection(self._parts.hostname, self._port, timeout=self.connect_timeout)
                self._conn.response_class = _HTTPResponse
                self._conn.sock = self._connect_socket()
            deadline(self.send_timeout)
            self._socket.settimeout(self.send_timeout)
            sent = True
            self._conn.request(request.method, self._path, request.body, headers)
            deadline(self.response_timeout)
            self._socket.settimeout(self.response_timeout)
            response = self._conn.getresponse()
            pairs = tuple(response.getheaders())
            snapshot = HTTPResponse(response.status, pairs, b'')
            # Reject ambiguous framing instead of reusing a desynchronized socket.
            lengths = [v.strip() for k,v in pairs if k.lower() == 'content-length']
            transfer = [v.strip().lower() for k,v in pairs if k.lower() == 'transfer-encoding']
            if (lengths and transfer) or len(lengths) > 1 or (lengths and not re.fullmatch(r'[0-9]+', lengths[0])) or (transfer and transfer != ['chunked']):
                raise http.client.HTTPException('Invalid response framing')
            if response.status == 101:
                raise http.client.HTTPException('Protocol switching is unsupported')
            body = response.read(self.max_response_bytes + 1)
            snapshot = HTTPResponse(response.status, pairs, body[:self.max_response_bytes])
            if response.length is not None and response.length > 0 and len(body) <= self.max_response_bytes:
                raise http.client.IncompleteRead(body, response.length)
            if len(body) > self.max_response_bytes:
                raise http.client.HTTPException('HTTP response exceeds configured limit')
            if self._abort.is_set():
                raise TimeoutError('HTTP response deadline elapsed')
            snapshot = HTTPResponse(response.status, pairs, body)
            if response.will_close or self._conn.sock is None:
                self._disconnect()
            return DeliveryResult('received', http_response=snapshot)
        except (TimeoutError, OSError) as exc:
            self._disconnect()
            return DeliveryResult('uncertain' if sent else 'connection_error', detail=type(exc).__name__, http_response=snapshot)
        except http.client.HTTPException as exc:
            self._disconnect()
            if isinstance(exc, http.client.IncompleteRead) and response is not None:
                snapshot = HTTPResponse(response.status, pairs, exc.partial[:self.max_response_bytes])
            return DeliveryResult('uncertain' if isinstance(exc, (http.client.RemoteDisconnected, http.client.IncompleteRead)) else 'protocol_error', detail=type(exc).__name__, http_response=snapshot)
        finally:
            if timer:
                timer.cancel()
                timer.join()
            if response:
                response.close()
            if self._abort.is_set():
                self._disconnect()

    async def _evaluate(self, response, original):
        try:
            if callable(self.response):
                result = self.response(response, original.copy())
                if inspect.isawaitable(result):
                    result = await result
                if not isinstance(result, DeliveryResult):
                    raise TypeError('Response evaluator must return DeliveryResult')
                return replace(result, http_response=response)
            if response.status not in self.accepted_statuses:
                return DeliveryResult('http_error', http_response=response)
            if self.response == 'status':
                return DeliveryResult('confirmed', http_response=response)
            code = read_ack(response.body, value(original, 'MSH-10'))
            return DeliveryResult('confirmed' if code in {'AA','CA'} else 'negative_ack', code,
                                  acknowledgements=(response.body,), http_response=response)
        except Exception as exc:
            return DeliveryResult('response_error', detail=type(exc).__name__, http_response=response)


def response_record(response, limit=65536):
    """Explicit diagnostic allowlist; never store cookies or authorization fields."""
    import base64
    return dict(status=response.status,
                headers=[(k,v) for k,v in response.headers if k.lower() in _DIAGNOSTIC_HEADERS],
                body_base64=base64.b64encode(response.body[:limit]).decode('ascii'),
                truncated=len(response.body) > limit)
