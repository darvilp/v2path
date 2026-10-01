"""Persistent asyncio MLLP endpoints. Importing this module starts no tasks."""
from __future__ import annotations

import asyncio
import inspect
import socket
from dataclasses import dataclass, replace

from .ack import AckRequest, make_ack, read_ack, value
from .core import Message, parse
from .delivery import DeliveryResult, RetryPolicy
from .errors import ParseError
from .framing import MLLPDecoder, frame


@dataclass(frozen=True)
class InboundContext:
    peer: object
    require_completion: bool


@dataclass
class Acceptance:
    completed: asyncio.Future


class AdmissionRejected(Exception):
    pass


class MLLPClient:
    def __init__(self, host, port, *, ssl=None, connect_timeout=10, send_timeout=30,
                 ack_timeout=30, max_frame_bytes=16 * 1024 * 1024, retry=None):
        if min(connect_timeout, send_timeout, ack_timeout, max_frame_bytes) <= 0:
            raise ValueError('Limits and timeouts must be positive')
        self.host, self.port, self.ssl = host, port, ssl
        self.connect_timeout, self.send_timeout, self.ack_timeout = connect_timeout, send_timeout, ack_timeout
        self.max_frame_bytes, self.retry = max_frame_bytes, retry or RetryPolicy()
        self._reader = self._writer = None
        self._lock = asyncio.Lock()
        self._decoder = MLLPDecoder(max_frame_bytes)
        self._frames = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def close(self):
        writer, self._writer = self._writer, None
        self._reader = None
        self._frames.clear()
        self._decoder = MLLPDecoder(self.max_frame_bytes)
        if writer:
            writer.close()
            try:
                await asyncio.shield(writer.wait_closed())
            except (OSError, ConnectionError):
                pass

    async def _response(self):
        while not self._frames:
            chunk = await self._reader.read(65536)
            if not chunk:
                raise ConnectionError('Connection closed while awaiting acknowledgement')
            self._frames.extend(self._decoder.feed(chunk))
        return self._frames.pop(0)

    async def send(self, message: Message | bytes):
        payload = message.to_bytes() if isinstance(message, Message) else bytes(message)
        original = parse(payload, encoding=message.encoding if isinstance(message, Message) else None)
        if any(s.name in {'FHS', 'BHS', 'BTS', 'FTS'} for s in original.segments()):
            return DeliveryResult('protocol_error', detail='Network frames require one bare message')
        request = AckRequest.from_message(original)
        expected = value(original, 'MSH-10')
        if (request.accept != 'NE' or request.application != 'NE') and not expected:
            return DeliveryResult('protocol_error', detail='A control ID is required for ACK correlation')
        if len(payload) > self.max_frame_bytes:
            return DeliveryResult('protocol_error', detail='Outbound message exceeds frame limit')
        wire = frame(payload)
        async with self._lock:
            attempts = 0
            while True:
                attempts += 1
                result = replace(await self._attempt(wire, request, expected), attempts=attempts)
                rule = self.retry.for_result(result)
                if attempts >= rule.attempts:
                    return result
                await asyncio.sleep(rule.delay)

    async def _attempt(self, wire, request, expected):
        sent = False
        acks = []
        stages = [('accept', request.accept), ('application', request.application)]
        stages = [(stage, q) for stage, q in stages if q != 'NE']
        current = None
        try:
            if self._writer is None or self._writer.is_closing():
                self._reader, self._writer = await asyncio.wait_for(
                    asyncio.open_connection(self.host, self.port, ssl=self.ssl), self.connect_timeout)
            sent = True
            self._writer.write(wire)
            await asyncio.wait_for(self._writer.drain(), self.send_timeout)
            skipped = False
            response = None
            for stage, qualifier in stages:
                current = qualifier
                if response is None:
                    response = await asyncio.wait_for(self._response(), self.ack_timeout)
                code = read_ack(response, expected)
                if stage == 'accept' and not code.startswith('C') and qualifier in {'ER', 'SU'} and request.application != 'NE':
                    skipped = True
                    continue
                acks.append(response)
                response = None
                if (code.startswith('C')) != (stage == 'accept'):
                    raise ParseError('ACK received for an unexpected stage')
                if code in {'AE', 'AR', 'CE', 'CR'}:
                    # A late second response must never be consumed by the next send.
                    await self.close()
                    return DeliveryResult('negative_ack', code, acknowledgements=tuple(acks))
                if qualifier == 'ER':
                    raise ParseError('Positive ACK received when only errors were requested')
            if self._frames or self._decoder.pending:
                raise ParseError('Unexpected extra ACK data')
            if skipped:
                await self.close()
            return DeliveryResult('unconfirmed' if skipped else 'confirmed' if stages else 'sent', code if stages else None, acknowledgements=tuple(acks))
        except asyncio.CancelledError:
            await self.close()
            raise
        except asyncio.TimeoutError:
            await self.close()
            outcome = 'unconfirmed' if current in {'ER', 'SU'} else 'uncertain' if sent else 'connection_error'
            return DeliveryResult(outcome, detail='Configured response/transport deadline elapsed', acknowledgements=tuple(acks))
        except ParseError as exc:
            await self.close()
            return DeliveryResult('protocol_error', detail=str(exc), acknowledgements=tuple(acks))
        except (OSError, ConnectionError) as exc:
            await self.close()
            return DeliveryResult('uncertain' if sent else 'connection_error', detail=type(exc).__name__, acknowledgements=tuple(acks))


class MLLPServer:
    def __init__(self, host, port, on_message, *, ack_mode='communication', ssl=None,
                 on_error=None, max_connections=32, max_frame_bytes=16 * 1024 * 1024,
                 frame_timeout=30, idle_timeout=300, send_timeout=30):
        if ack_mode not in ('communication', 'application'):
            raise ValueError('ack_mode must be communication or application')
        if min(max_connections, max_frame_bytes, frame_timeout, idle_timeout, send_timeout) <= 0:
            raise ValueError('Limits and timeouts must be positive')
        self.host, self.port, self.on_message = host, port, on_message
        self.ack_mode, self.ssl, self.on_error = ack_mode, ssl, on_error
        self.max_connections, self.max_frame_bytes = max_connections, max_frame_bytes
        self.frame_timeout, self.idle_timeout, self.send_timeout = frame_timeout, idle_timeout, send_timeout
        self._listener = None
        self._accept_task = None
        self._accept_future = None
        self._opening = {}
        self._stopping = False
        self._tasks = set()
        self._writers = set()
        self.errors = []

    @property
    def address(self):
        return self._listener.getsockname()

    async def start(self):
        if self._listener is not None:
            raise RuntimeError('Server already started')
        self._stopping = False
        loop = asyncio.get_running_loop()
        addresses = await loop.getaddrinfo(self.host, self.port, type=socket.SOCK_STREAM,
                                           flags=socket.AI_PASSIVE)
        error = None
        for family, kind, protocol, _, address in addresses:
            listener = socket.socket(family, kind, protocol)
            try:
                if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                    listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                else:
                    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                listener.bind(address)
                listener.listen(self.max_connections)
                listener.setblocking(False)
                self._listener = listener
                break
            except OSError as exc:
                listener.close()
                error = exc
        if self._listener is None:
            raise error or OSError('No listening address resolved')
        self._accept_task = asyncio.create_task(self._listen())
        return self

    async def _listen(self):
        loop = asyncio.get_running_loop()
        while not self._stopping:
            # Keep ownership even if shutdown cancels us after accept succeeds
            # but before this coroutine resumes with the new socket.
            self._accept_future = asyncio.create_task(loop.sock_accept(self._listener))
            try:
                conn, _ = await asyncio.shield(self._accept_future)
            except OSError as exc:
                self._accept_future = None
                await self._error(b'', 'connection_error', type(exc).__name__)
                return
            self._accept_future = None
            if self._stopping or len(self._tasks) + len(self._opening) >= self.max_connections:
                conn.close()
                continue
            task = asyncio.create_task(self._open_socket(conn))
            self._opening[task] = conn

    async def _open_socket(self, conn):
        try:
            reader = asyncio.StreamReader()
            protocol = asyncio.StreamReaderProtocol(reader, self._accept)
            options = dict(ssl=self.ssl)
            if self.ssl:
                options.update(ssl_handshake_timeout=self.frame_timeout, ssl_shutdown_timeout=self.send_timeout)
            await asyncio.get_running_loop().connect_accepted_socket(lambda: protocol, conn, **options)
        except asyncio.CancelledError:
            conn.close()
            raise
        except Exception as exc:
            conn.close()
            await self._error(b'', 'connection_error', type(exc).__name__)
        finally:
            self._opening.pop(asyncio.current_task(), None)

    async def __aenter__(self):
        return await self.start()

    async def __aexit__(self, *exc):
        await self.close()

    async def stop_accepting(self):
        self._stopping = True
        if self._accept_task:
            self._accept_task.cancel()
            await asyncio.gather(self._accept_task, return_exceptions=True)
        if self._accept_future:
            pending, self._accept_future = self._accept_future, None
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            if not pending.cancelled() and pending.exception() is None:
                conn, _ = pending.result()
                conn.close()
        if self._listener:
            self._listener.close()

    async def close(self):
        await self.stop_accepting()
        opening = tuple(self._opening)
        for task in opening:
            task.cancel()
        await asyncio.gather(*opening, return_exceptions=True)
        # A setup task cancelled before its first instruction cannot run finally.
        for task, conn in tuple(self._opening.items()):
            conn.close()
            self._opening.pop(task, None)
        for writer in tuple(self._writers):
            writer.close()
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for writer in tuple(self._writers):
            await self._closed_writer(writer)
        self._writers.clear()

    async def _error(self, payload, outcome, detail):
        result = DeliveryResult(outcome, detail=detail)
        self.errors.append(result)
        if len(self.errors) > 128:
            del self.errors[:-128]
        if self.on_error:
            try:
                response = self.on_error(payload, result)
                if inspect.isawaitable(response):
                    await response
            except Exception as exc:
                self.errors.append(DeliveryResult('error_handler_failed', detail=type(exc).__name__))
                del self.errors[:-128]

    def _accept(self, reader, writer):
        rejected = self._stopping
        if rejected:
            writer.close()
            task = asyncio.create_task(self._closed_writer(writer))
        else:
            self._writers.add(writer)
            task = asyncio.create_task(self._connected(reader, writer))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    @staticmethod
    async def _closed_writer(writer):
        try:
            await asyncio.shield(writer.wait_closed())
        except (OSError, ConnectionError):
            pass

    async def _connected(self, reader, writer):
        task = asyncio.current_task()
        decoder = MLLPDecoder(self.max_frame_bytes)
        started = None
        payload = b''
        try:
            while True:
                now = asyncio.get_running_loop().time()
                timeout = self.idle_timeout if started is None else max(0, self.frame_timeout - (now - started))
                chunk = await asyncio.wait_for(reader.read(65536), timeout)
                if self._stopping:
                    break
                if not chunk:
                    decoder.finish()
                    break
                if started is None:
                    started = asyncio.get_running_loop().time()
                for payload in decoder.iter_feed(chunk):
                    if self._stopping:
                        break
                    await self._handle(payload, writer)
                    payload = b''
                    # Admission/completion processing is not frame assembly time.
                    started = asyncio.get_running_loop().time()
                if not decoder.pending:
                    started = None
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            if decoder.pending:
                await self._error(bytes(decoder._buffer), 'protocol_error', 'Frame assembly deadline elapsed')
        except Exception as exc:
            await self._error(payload or bytes(decoder._buffer), 'protocol_error', type(exc).__name__ + ': ' + str(exc))
        finally:
            writer.close()
            try:
                await asyncio.shield(writer.wait_closed())
            except (OSError, ConnectionError):
                pass
            self._writers.discard(writer)
            self._tasks.discard(task)

    async def _reply(self, writer, msg, code, detail=''):
        writer.write(frame(make_ack(msg, code, detail=detail).to_bytes()))
        await asyncio.wait_for(writer.drain(), self.send_timeout)

    async def _handle(self, payload, writer):
        try:
            msg = parse(payload)
            if any(s.name in {'FHS', 'BHS', 'BTS', 'FTS'} for s in msg.segments()):
                raise ParseError('Network frames require one bare message')
            if value(msg, 'MSH-9.1') == 'ACK':
                raise ParseError('Unsolicited ACK is not routed or acknowledged')
            request = AckRequest.from_message(msg)
            if (request.accept != 'NE' or request.application != 'NE') and not value(msg, 'MSH-10'):
                raise ParseError('A control ID is required for ACK correlation')
        except ParseError:
            raise
        complete = self.ack_mode == 'application' if request.original else request.application != 'NE'
        try:
            accepted = self.on_message(msg, InboundContext(writer.get_extra_info('peername'), complete))
            if inspect.isawaitable(accepted):
                accepted = await accepted
        except (AdmissionRejected, asyncio.TimeoutError) as exc:
            q = request.application if request.original else request.accept
            if AckRequest.sends(q, False):
                await self._reply(writer, msg, 'AR' if request.original else 'CE', str(exc))
            await self._error(payload, 'rejected', str(exc))
            return
        if isinstance(accepted, DeliveryResult):
            future = asyncio.get_running_loop().create_future()
            future.set_result(accepted)
            accepted = Acceptance(future)
        if not hasattr(accepted, 'completed'):
            raise TypeError('Message handler must return Acceptance or DeliveryResult')
        if request.original:
            if complete:
                result = await asyncio.shield(accepted.completed)
                await self._reply(writer, msg, 'AA' if result.success else 'AE', '' if result.success else result.outcome)
            else:
                await self._reply(writer, msg, 'AA')
        else:
            if AckRequest.sends(request.accept, True):
                await self._reply(writer, msg, 'CA')
            if request.application != 'NE':
                result = await asyncio.shield(accepted.completed)
                if AckRequest.sends(request.application, result.success):
                    await self._reply(writer, msg, 'AA' if result.success else 'AE', '' if result.success else result.outcome)
