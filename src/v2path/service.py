"""Bounded in-process routes with required fanout and explicit failure replay."""
from __future__ import annotations

import asyncio
import base64
import fnmatch
import inspect
import json
import os
import shutil
import tempfile
from collections import deque
from dataclasses import dataclass, field, replace
from pathlib import Path
from uuid import uuid4

from .core import Message, parse
from .delivery import DeliveryResult, RetryPolicy
from .files import (DirectoryDestination, DirectorySource, FailureStore, FileDestination,
                    FileSource, Spool, filename, publish, utcnow)
from .http import HTTPClient, HTTPDestination, HTTPRequest, response_record
from .io import iter_messages
from .net import Acceptance, AdmissionRejected, MLLPClient, MLLPServer


@dataclass(frozen=True)
class MLLPSource:
    host: str = '127.0.0.1'
    port: int = 2575
    ack_mode: str = 'communication'
    options: dict = field(default_factory=dict)


@dataclass(frozen=True)
class MLLPDestination:
    host: str
    port: int
    options: dict = field(default_factory=dict)


@dataclass(frozen=True)
class RouteDestination:
    route: str
    wait_for: str = 'acceptance'

    def __post_init__(self):
        if self.wait_for not in {'acceptance', 'completion'}:
            raise ValueError('wait_for must be acceptance or completion')


@dataclass
class Route:
    name: str
    destinations: dict
    source: MLLPSource | FileSource | DirectorySource | None = None
    transform: object = None
    capacity: int = 128
    max_bytes: int = 64 * 1024 * 1024
    max_output_messages: int = 1000
    max_output_bytes: int = 64 * 1024 * 1024
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    allow_internal: bool = True

    def __post_init__(self):
        if not self.name or not self.destinations:
            raise ValueError('Routes require a name and at least one destination')
        if min(self.capacity, self.max_bytes, self.max_output_messages, self.max_output_bytes) <= 0:
            raise ValueError('Route limits must be positive')
        filename('{route}.hl7', route=self.name)
        for name in self.destinations:
            filename('{destination}.hl7', destination=name)


@dataclass
class Receipt(Acceptance):
    dispatched: asyncio.Future
    identity: str


@dataclass
class _Work:
    payload: bytes
    receipt: Receipt
    source: dict
    force: bool
    group: str | None
    created_at: str
    encoding: str | None


@dataclass
class _Delivery:
    payload: bytes
    future: asyncio.Future
    metadata: dict
    force: bool
    group: str | None
    request: HTTPRequest | None = None


@dataclass
class _Flush:
    future: asyncio.Future
    group: str | None = None
    pressure: bool = False


def _future():
    return asyncio.get_running_loop().create_future()


def _aggregate(results):
    return next((r for r in results if not r.success), DeliveryResult('completed'))


def _result_data(result):
    data = dict(outcome=result.outcome, code=result.code, detail=result.detail,
                attempts=result.attempts,
                acknowledgements=[base64.b64encode(a).decode('ascii') for a in
                                 (() if result.http_response is not None else result.acknowledgements)])
    if result.http_response is not None:
        data['http_response'] = response_record(result.http_response)
    return data


class _Destination:
    def __init__(self, service, route, name, endpoint):
        self.service, self.route, self.name, self.endpoint = service, route, name, endpoint
        self.queue = asyncio.Queue(route.capacity)
        self.spools = {}
        self.pending = {}
        self.group_results = {}
        self.sequence = 0
        self.client = MLLPClient(endpoint.host, endpoint.port, **endpoint.options) if isinstance(endpoint, MLLPDestination) else None
        if isinstance(endpoint, HTTPDestination):
            self.client = HTTPClient(endpoint.url, method=endpoint.method, headers=endpoint.headers,
                encoder=endpoint.encoder, header_provider=endpoint.header_provider, response=endpoint.response, **endpoint.options)
        self.task = None

    def mode(self, group):
        if isinstance(self.endpoint, FileDestination):
            return 'preserve'
        mode = self.endpoint.mode
        if mode == 'auto':
            chained = Path(self.endpoint.path).resolve() in self.service._input_directories
            return 'message' if chained else 'preserve' if group else 'batch'
        return 'batch' if mode == 'preserve' and not group else mode

    async def flush(self, group=None, pressure=False):
        f = _future()
        await self.queue.put(_Flush(f, group, pressure))
        return await asyncio.shield(f)

    async def run(self):
        while True:
            timeout = None
            deadlines = [spool.deadline for spool in self.spools.values() if spool.mode == 'batch']
            if deadlines:
                timeout = max(0, min(deadlines) - asyncio.get_running_loop().time())
            try:
                job = await asyncio.wait_for(self.queue.get(), timeout) if timeout is not None else await self.queue.get()
            except asyncio.TimeoutError:
                now = asyncio.get_running_loop().time()
                for key, spool in list(self.spools.items()):
                    if spool.mode == 'batch' and spool.deadline <= now:
                        await self._publish(key)
                continue
            if job is None:
                self.queue.task_done()
                break
            try:
                if isinstance(job, _Flush):
                    results = []
                    for key in list(self.spools):
                        if (job.group is None or key == job.group) and not (job.pressure and self.spools[key].mode == 'preserve'):
                            results.append(await self._publish(key))
                    if job.group is not None:
                        results.append(self.group_results.pop(job.group, DeliveryResult('completed')))
                    job.future.set_result(_aggregate(results))
                elif isinstance(self.endpoint, (DirectoryDestination, FileDestination)):
                    await self._file(job)
                else:
                    result = await self._send(job)
                    if not result.success and result.outcome != 'downstream_failure':
                        await self.service._failure(job.metadata, job.payload, result, request=job.request)
                    job.future.set_result(result)
            except Exception as exc:
                result = DeliveryResult('delivery_error', detail=type(exc).__name__ + ': ' + str(exc))
                if isinstance(job, _Delivery):
                    await self.service._failure(job.metadata, job.payload, result, request=job.request)
                if not job.future.done():
                    job.future.set_result(result)
            finally:
                self.queue.task_done()

    async def _send(self, job):
        if isinstance(self.client, HTTPClient):
            if job.request is None:
                try:
                    job.request = await self.client.prepare(parse(job.payload, encoding=job.metadata.get('encoding')))
                except Exception as exc:
                    return DeliveryResult('request_error', detail=type(exc).__name__)
            policy = self.route.retry if self.route.retry.rules else self.client.retry
            return await self.client.send_prepared(job.request, retry=policy)
        attempt = 0
        while True:
            attempt += 1
            try:
                if self.client:
                    result = await self.client.send(parse(job.payload, encoding=job.metadata.get('encoding')))
                else:
                    receipt = await self.service.submit(self.endpoint.route, job.payload,
                        source=dict(kind='route', route=self.route.name, delivery=job.metadata['delivery_id']),
                        require_completion=self.endpoint.wait_for == 'completion', _internal=True, _encoding=job.metadata.get('encoding'))
                    result = await asyncio.shield(receipt.completed) if self.endpoint.wait_for == 'completion' else DeliveryResult('accepted')
                    # Once a downstream route accepted the input, it owns retries
                    # and failure records. Re-enqueuing here duplicates its successes.
                    return result if result.success else DeliveryResult('downstream_failure', detail='Inspect downstream route failure records')
            except Exception as exc:
                result = DeliveryResult('delivery_error', detail=type(exc).__name__ + ': ' + str(exc))
            result = replace(result, attempts=attempt * result.attempts)
            rule = self.route.retry.for_result(result)
            if attempt >= rule.attempts:
                return result
            await asyncio.sleep(rule.delay)

    async def _file(self, job):
        mode = self.mode(job.group)
        key = job.group if mode == 'preserve' else None
        if key not in self.spools:
            self.sequence += 1
            spool = Spool(self.endpoint, self.route.name, self.name, mode,
                          job.metadata['source'].get('stem', ''), self.sequence)
            spool.replay_of = job.metadata.get('replay_of')
            spool.deadline = asyncio.get_running_loop().time() + getattr(self.endpoint, 'max_seconds', 60)
            self.spools[key], self.pending[key] = spool, []
        spool = self.spools[key]
        try:
            spool.add(parse(job.payload, encoding=job.metadata.get('encoding')), dict(job.metadata, payload_base64=base64.b64encode(job.payload).decode('ascii')))
        except Exception:
            # An interrupted write cannot be followed by more content in this artifact.
            await self._publish(key, DeliveryResult('file_error', detail='Artifact write failed'))
            raise
        if mode == 'preserve' and job.group and not job.force:
            # File groups spool payload AND member metadata to disk. This receipt
            # releases memory; source archival still waits for group publication.
            job.future.set_result(DeliveryResult('accepted'))
        else:
            self.pending[key].append(job.future)
        rotate = mode == 'message' or job.force
        if mode == 'batch':
            rotate |= (spool.count >= self.endpoint.max_messages or spool.size >= self.endpoint.max_bytes
                       or len(self.pending[key]) >= self.route.capacity)
        if rotate:
            await self._publish(key)

    async def _publish(self, key, failure=None):
        spool = self.spools[key]
        attempts = 0
        while True:
            attempts += 1
            try:
                spool.finish()
                if failure or not spool.complete:
                    result = failure or DeliveryResult('file_error', detail='Artifact finalization failed')
                else:
                    publish(spool.path, spool.final)
                    result = DeliveryResult('published', attempts=attempts)
            except Exception as exc:
                result = DeliveryResult('file_error', detail=type(exc).__name__ + ': ' + str(exc), attempts=attempts)
            if not spool.complete or failure or result.success or attempts >= self.route.retry.for_result(result).attempts:
                break
            await asyncio.sleep(self.route.retry.for_result(result).delay)
        if not result.success:
            await self.service._failure(dict(route=self.route.name, destination=self.name,
                kind='artifact' if spool.complete and not failure else 'artifact_incomplete', target=str(spool.final), batch_id=spool.identity,
                created_at=spool.created_at, source={'kind': 'batch'}, members=spool.count, replay_of=spool.replay_of),
                spool.path, result, members=spool.members)
        if key is not None and not result.success:
            self.group_results[key] = result
        for future in self.pending.pop(key):
            if not future.done():
                future.set_result(result)
        del self.spools[key]
        try:
            spool.discard()
        except OSError as exc:
            self.service._error(exc)
        return result


class _RouteState:
    def __init__(self, route):
        self.route = route
        self.queue = asyncio.Queue(route.capacity)
        self.condition = asyncio.Condition()
        self.count = self.size = 0
        self.pending = set()
        self.destinations = {}
        self.worker = None


class Service:
    def __init__(self, routes, *, failed_directory, drain_timeout=30):
        routes = list(routes)
        self.routes = {r.name: r for r in routes}
        if len(self.routes) != len(routes) or not routes:
            raise ValueError('Route names must be unique and routes must not be empty')
        if drain_timeout <= 0:
            raise ValueError('Drain timeout must be positive')
        self.failed_directory = Path(failed_directory)
        self.drain_timeout = drain_timeout
        self.errors = []
        self.servers = {}
        self._states = {}
        self._source_tasks = []
        self._stop = asyncio.Event()
        self._resume = asyncio.Event()
        self._paused = False
        self._replay_lock = asyncio.Lock()
        self._started = self._closing = False
        self._input_directories = set()
        self._validate()

    def _validate(self):
        owners, writers, graph = {}, {}, {name: set() for name in self.routes}
        source_paths = {}
        listener = {}
        def own(mapping, key, owner):
            if key in mapping:
                raise ValueError(f'Duplicate endpoint ownership: {key}')
            mapping[key] = owner
        for route in self.routes.values():
            source = route.source
            if source is None and not route.allow_internal:
                raise ValueError('A route needs a source or internal admission')
            if isinstance(source, (DirectorySource, FileSource)):
                path = Path(source.path).resolve()
                own(owners, path, route.name)
                source_paths[path] = route
                if isinstance(source, DirectorySource):
                    self._input_directories.add(path)
            elif isinstance(source, MLLPSource):
                # Port zero is explicitly an independently allocated listener.
                if source.port:
                    own(listener, self._tcp_key(source.host, source.port), route.name)
            elif source is not None:
                raise TypeError('Unsupported route source')
            for name, dest in route.destinations.items():
                if isinstance(dest, (DirectoryDestination, FileDestination)):
                    own(writers, Path(dest.path).resolve(), (route.name, name))
                    if isinstance(dest, FileDestination) and not isinstance(source, FileSource):
                        raise ValueError('A fixed output file requires a finite FileSource; use a rotating directory')
                    # Validate formatting and supplied envelope metadata before starting.
                    if isinstance(dest, DirectoryDestination) and dest.template:
                        filename(dest.template, **{k: 'x' for k in ('route','destination','timestamp','id','batch_id','sequence','source_stem')})
                    if dest.framing not in {'bare', 'mllp'} or dest.terminator not in {None, '\r', '\n', '\r\n'}:
                        raise ValueError('Invalid file framing or terminator')
                    if dest.file_header and not dest.batch_header:
                        raise ValueError('FHS requires BHS')
                    if dest.batch_header and dest.framing == 'mllp':
                        raise ValueError('Batch envelopes require bare framing')
                elif isinstance(dest, RouteDestination):
                    if dest.route not in self.routes or not self.routes[dest.route].allow_internal:
                        raise ValueError('Destination route is missing or rejects internal handoff')
                    graph[route.name].add(dest.route)
                elif isinstance(dest, (MLLPDestination, HTTPDestination)) and dest.options.get('retry') and route.retry.rules:
                    raise ValueError('Configure retry on the route or transport client, not both')
                elif not isinstance(dest, (MLLPDestination, HTTPDestination)):
                    raise TypeError('Unsupported destination')
        directory_sources = {Path(r.source.path).resolve(): r for r in self.routes.values()
                             if isinstance(r.source, DirectorySource)}
        output_directories = {Path(d.path).resolve() for r in self.routes.values()
                              for d in r.destinations.values() if isinstance(d, DirectoryDestination)}
        for route in self.routes.values():
            source = route.source
            if isinstance(source, FileSource):
                path = Path(source.path).resolve()
                watcher = directory_sources.get(path.parent)
                if watcher and fnmatch.fnmatch(path.name, watcher.source.pattern):
                    raise ValueError('A file source is also owned by a directory watcher')
            for dest in route.destinations.values():
                if isinstance(dest, FileDestination) and Path(dest.path).resolve().parent in output_directories:
                    raise ValueError('A fixed file destination overlaps a directory destination')
        for route in self.routes.values():
            source = route.source
            if isinstance(source, (DirectorySource, FileSource)) and source.archive is not None:
                if Path(source.archive).resolve() in self._input_directories:
                    raise ValueError('Archive location cannot be a watched input directory')
        for path in owners:
            if self.failed_directory.resolve() == path:
                raise ValueError('Failure storage cannot be an input endpoint')
        for route in self.routes.values():
            for dest in route.destinations.values():
                if isinstance(dest, (FileDestination, DirectoryDestination)):
                    path = Path(dest.path).resolve()
                    target = source_paths.get(path)
                    if target is None and isinstance(dest, FileDestination):
                        watcher = directory_sources.get(path.parent)
                        if watcher and fnmatch.fnmatch(path.name, watcher.source.pattern):
                            target = watcher
                    if target:
                        graph[route.name].add(target.name)
                        if isinstance(target.source, DirectorySource):
                            if target.source.readiness != 'atomic':
                                raise ValueError('Chained directory inputs must use atomic readiness')
                            template = getattr(dest, 'template', None) or '{route}-{timestamp}-{id}.hl7'
                            sample = path.name if isinstance(dest, FileDestination) else filename(template, **{k: 'x' for k in ('route','destination','timestamp','id','batch_id','sequence','source_stem')})
                            if not fnmatch.fnmatch(sample, target.source.pattern):
                                raise ValueError('Chained output filenames do not match the input glob')
                elif isinstance(dest, MLLPDestination):
                    target = listener.get(self._tcp_key(dest.host, dest.port))
                    if target:
                        graph[route.name].add(target)
        visiting, visited = set(), set()
        def visit(name):
            if name in visiting:
                raise ValueError('Routes contain a delivery cycle')
            if name not in visited:
                visiting.add(name)
                for target in graph[name]:
                    visit(target)
                visiting.remove(name)
                visited.add(name)
        for name in graph:
            visit(name)

    @staticmethod
    def _tcp_key(host, port):
        return ('local' if host.lower() in {'localhost','127.0.0.1','::1','0.0.0.0','::'} else host.lower(), port)

    def _error(self, exc):
        self.errors.append(exc)
        del self.errors[:-128]

    async def _failure(self, metadata, payload, result, *, members=None, request=None):
        replay_of = metadata.get('replay_of')
        if replay_of:
            # Keep one actionable record per failed delivery across replay attempts.
            # The payload remains the original frozen output.
            return self.failed_directory / replay_of
        metadata = dict(metadata)
        if request is not None:
            metadata.update(kind='http', http_request=dict(url=request.url, method=request.method, encoding=request.encoding))
        elif metadata.get('route') in self.routes and isinstance(
                self.routes[metadata['route']].destinations.get(metadata.get('destination')), HTTPDestination):
            metadata['kind'] = 'http_prepare'
        identity = uuid4().hex
        while True:
            try:
                return self.failures.write(dict(metadata, **_result_data(result)), payload,
                                           members=members, identity=identity, request_body=request.body if request is not None else None)
            except OSError as exc:
                self._paused = True
                self._resume.clear()
                self._error(exc)
                for state in self._states.values():
                    async with state.condition:
                        state.condition.notify_all()
                await self._resume.wait()

    def resume(self):
        """Retry pending failure-store writes after the operator repairs storage."""
        self._paused = False
        self._resume.set()

    async def __aenter__(self):
        return await self.start()

    async def __aexit__(self, *exc):
        await self.close()

    async def start(self):
        if self._started or self._states:
            raise RuntimeError('Create a new Service after a completed shutdown or failed start')
        self.failures = FailureStore(self.failed_directory)
        self._started = True
        try:
            for route in self.routes.values():
                state = self._states[route.name] = _RouteState(route)
                for name, endpoint in route.destinations.items():
                    destination = _Destination(self, route, name, endpoint)
                    state.destinations[name] = destination
                    destination.task = asyncio.create_task(destination.run())
                state.worker = asyncio.create_task(self._route_worker(state))
            for route in self.routes.values():
                source = route.source
                if isinstance(source, MLLPSource):
                    async def incoming(message, context, name=route.name):
                        return await self.submit(name, message, source={'kind': 'mllp'},
                                                 require_completion=context.require_completion, _internal=True)
                    async def error(payload, result, name=route.name):
                        await self._failure(dict(route=name, destination=None, kind='input', source={'kind': 'mllp'}), payload, result)
                    server = MLLPServer(source.host, source.port, incoming, ack_mode=source.ack_mode,
                                        on_error=error, **source.options)
                    self.servers[route.name] = await server.start()
                elif isinstance(source, DirectorySource):
                    self._recover(source)
                    self._source_tasks.append(asyncio.create_task(self._watch(route)))
                elif isinstance(source, FileSource):
                    self._source_tasks.append(asyncio.create_task(self._file_source(route, Path(source.path))))
            return self
        except BaseException:
            for state in self._states.values():
                state.worker.cancel()
                for destination in state.destinations.values():
                    destination.task.cancel()
            for server in self.servers.values():
                await server.close()
            raise

    async def submit(self, route, message, *, source=None, require_completion=False, _group=None, _internal=False, _encoding=None):
        if not self._started or (self._closing and not _internal):
            raise AdmissionRejected('Service is not accepting work')
        state = self._states[route]
        if not state.route.allow_internal and not _internal:
            raise AdmissionRejected('This route does not allow internal admission')
        encoding = message.encoding if isinstance(message, Message) else _encoding
        payload = message.to_bytes() if isinstance(message, Message) else bytes(message)
        if source is not None:
            source = json.loads(json.dumps(source))
            if not isinstance(source, dict):
                raise ValueError('Source metadata must be a JSON object')
        parse(payload, encoding=encoding)
        if len(payload) > state.route.max_bytes:
            raise AdmissionRejected('Input exceeds route byte capacity')
        while True:
            async with state.condition:
                if self._paused:
                    raise AdmissionRejected('Failure storage is unavailable')
                if state.count < state.route.capacity and state.size + len(payload) <= state.route.max_bytes:
                    state.count += 1
                    state.size += len(payload)
                    break
            # Let already admitted work reach its destinations before flushing.
            # Never hold the credit condition while waiting on another worker.
            await state.queue.join()
            await self._flush_state(state, pressure=True)
            async with state.condition:
                await state.condition.wait_for(lambda: self._paused or
                    (state.count < state.route.capacity and state.size + len(payload) <= state.route.max_bytes))
        receipt = Receipt(_future(), _future(), uuid4().hex)
        work = _Work(payload, receipt, source or {'kind': 'internal'}, require_completion, _group, utcnow(), encoding)
        state.queue.put_nowait(work)
        return receipt

    async def _route_worker(self, state):
        while True:
            work = await state.queue.get()
            if work is None:
                state.queue.task_done()
                return
            deliveries = []
            try:
                message = parse(work.payload, encoding=work.encoding)
                result = state.route.transform(message) if state.route.transform else message
                if inspect.isawaitable(result):
                    result = await result
                outputs = () if result is None else (result,) if isinstance(result, Message) else result
                # Freeze and validate all outputs before any destination sees one.
                frozen, total = [], 0
                for output in outputs:
                    if not isinstance(output, Message):
                        raise TypeError('Transforms must yield Message instances')
                    payload = output.copy().to_bytes()
                    total += len(payload)
                    if len(frozen) >= state.route.max_output_messages or total > state.route.max_output_bytes:
                        raise ValueError('Transform output exceeds configured route limits')
                    frozen.append((payload, output.encoding))
                for index, (payload, encoding) in enumerate(frozen):
                    for name, destination in state.destinations.items():
                        future = _future()
                        metadata = dict(route=state.route.name, destination=name, kind='message',
                            input_id=work.receipt.identity, delivery_id=uuid4().hex, output_index=index,
                            source=work.source, created_at=work.created_at, encoding=encoding)
                        await destination.queue.put(_Delivery(payload, future, metadata, work.force, work.group))
                        deliveries.append(future)
                task = asyncio.create_task(self._complete(state, work, deliveries))
                state.pending.add(task)
                task.add_done_callback(state.pending.discard)
            except Exception as exc:
                failure = DeliveryResult('transform_error', detail=type(exc).__name__ + ': ' + str(exc))
                await self._failure(dict(route=state.route.name, destination=None, kind='transform',
                    input_id=work.receipt.identity, source=work.source, created_at=work.created_at, encoding=work.encoding), work.payload, failure)
                await self._release(state, work, failure)
            finally:
                if not work.receipt.dispatched.done():
                    work.receipt.dispatched.set_result(None)
                state.queue.task_done()

    async def _complete(self, state, work, deliveries):
        results = await asyncio.gather(*deliveries)
        await self._release(state, work, _aggregate(results) if deliveries else DeliveryResult('filtered'))

    async def _release(self, state, work, result):
        if not work.receipt.completed.done():
            work.receipt.completed.set_result(result)
        async with state.condition:
            state.count -= 1
            state.size -= len(work.payload)
            state.condition.notify_all()

    async def _flush_state(self, state, group=None, pressure=False):
        return _aggregate(await asyncio.gather(*(d.flush(group, pressure) for d in state.destinations.values())))

    async def flush(self):
        for state in self._states.values():
            await state.queue.join()
        return _aggregate(await asyncio.gather(*(self._flush_state(s) for s in self._states.values())))

    async def wait_sources(self):
        """Wait for finite inputs. Directory watchers run until close()."""
        if self._source_tasks:
            await asyncio.gather(*(asyncio.shield(t) for t in self._source_tasks))

    async def _file_source(self, route, path, *, original=None):
        group = uuid4().hex
        source = route.source
        original = original or path
        metadata = dict(kind='file', path=str(original), stem=original.stem, group=group)
        receipts = deque()
        success = True
        try:
            for message in iter_messages(path, encoding=source.encoding):
                receipt = await self.submit(route.name, message, source=metadata, _group=group, _internal=True)
                receipts.append(receipt)
                await asyncio.shield(receipt.dispatched)
                while receipts and receipts[0].completed.done():
                    success &= receipts.popleft().completed.result().success
                if len(receipts) >= route.capacity:
                    await self._flush_state(self._states[route.name], pressure=True)
                    success &= (await asyncio.shield(receipts.popleft().completed)).success
            success &= (await self._flush_state(self._states[route.name], group)).success
            # Batch mode is keyed by None, not the input group.
            success &= (await self._flush_state(self._states[route.name])).success
            for receipt in receipts:
                success &= (await asyncio.shield(receipt.completed)).success
        except Exception as exc:
            success = False
            await self._failure(dict(route=route.name, destination=None, kind='input', source=metadata),
                                path, DeliveryResult('input_error', detail=type(exc).__name__ + ': ' + str(exc)))
        if success:
            archive = Path(source.archive) if source.archive else original.parent / '.v2path-archive'
            archive.mkdir(parents=True, exist_ok=True)
            target = archive / (original.stem + '-' + group + original.suffix)
            try:
                # copy/link publication never overwrites an older archived input.
                fd, tmp = tempfile.mkstemp(prefix='.archive-', dir=archive)
                os.close(fd)
                try:
                    shutil.copyfile(path, tmp)
                    with open(tmp, 'r+b') as handle:
                        os.fsync(handle.fileno())
                    publish(tmp, target)
                    path.unlink()
                finally:
                    Path(tmp).unlink(missing_ok=True)
            except OSError as exc:
                self._error(exc)
                success = False
        if not success and isinstance(source, DirectorySource) and path.exists():
            quarantine = Path(source.path) / '.v2path-quarantine'
            quarantine.mkdir(exist_ok=True)
            os.rename(path, quarantine / (uuid4().hex + '-' + original.name))
        return success

    def _recover(self, source):
        root = Path(source.path)
        root.mkdir(parents=True, exist_ok=True)
        claimed = root / '.v2path-claimed'
        claimed.mkdir(exist_ok=True)
        quarantine = root / '.v2path-quarantine'
        quarantine.mkdir(exist_ok=True)
        for path in claimed.iterdir():
            if path.is_file():
                os.rename(path, quarantine / (uuid4().hex + '-' + path.name))

    async def _watch(self, route):
        source = route.source
        root = Path(source.path)
        observed = {}
        while not self._stop.is_set():
            for path in sorted(root.glob(source.pattern)):
                if self._stop.is_set():
                    break
                if not path.is_file() or path.is_symlink() or path.name.startswith('.') or path.name.endswith(('.tmp', '.part', source.marker_suffix)):
                    continue
                try:
                    marker = Path(str(path) + source.marker_suffix)
                    if source.readiness == 'marker' and not marker.is_file():
                        continue
                    if source.readiness == 'stable':
                        stat = path.stat()
                        signature = (stat.st_size, stat.st_mtime_ns)
                        previous = observed.get(path)
                        now = asyncio.get_running_loop().time()
                        if previous is None or previous[0] != signature:
                            observed[path] = (signature, now)
                            continue
                        if now - previous[1] < source.stability:
                            continue
                    claimed = root / '.v2path-claimed' / (uuid4().hex + '-' + path.name)
                    os.rename(path, claimed)
                    observed.pop(path, None)
                    if source.readiness == 'marker':
                        marker.unlink(missing_ok=True)
                    await self._file_source(route, claimed, original=path)
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    self._error(exc)
            observed = {p: v for p, v in observed.items() if p.exists()}
            try:
                await asyncio.wait_for(self._stop.wait(), source.poll_interval)
            except asyncio.TimeoutError:
                pass

    async def replay(self, identity):
        """Explicitly replay one failed output/artifact; never successful siblings."""
        async with self._replay_lock:
            return await self._replay(identity)

    async def _replay(self, identity):
        if Path(identity).name != identity or identity.startswith('.'):
            raise ValueError('Expected a failure record ID')
        record_path = self.failed_directory / identity
        record = json.loads((record_path / 'record.json').read_text(encoding='utf-8'))
        if (record_path / 'replayed').exists() or (record_path / 'superseded').exists():
            raise ValueError('This failure was already replayed or superseded')
        kind = record['kind']
        if kind == 'artifact':
            dest = self._states[record['route']].destinations[record['destination']]
            if not isinstance(dest.endpoint, (FileDestination, DirectoryDestination)):
                raise ValueError('Artifact destination is no longer a file endpoint')
            target = Path(record['target'])
            root = Path(dest.endpoint.path).parent if isinstance(dest.endpoint, FileDestination) else Path(dest.endpoint.path)
            if target.parent.resolve() != root.resolve():
                raise ValueError('Recorded output is outside its configured destination')
            fd, tmp = tempfile.mkstemp(prefix='.replay-', dir=root)
            os.close(fd)
            try:
                shutil.copyfile(record_path / 'payload.hl7', tmp)
                with open(tmp, 'r+b') as handle:
                    os.fsync(handle.fileno())
                publish(tmp, target)
                result = DeliveryResult('published')
            finally:
                Path(tmp).unlink(missing_ok=True)
        elif kind == 'http':
            destination = self._states[record['route']].destinations[record['destination']]
            target = record['http_request']
            if not isinstance(destination.client, HTTPClient) or (target['url'], target['method']) != (destination.client.url, destination.client.method):
                raise ValueError('HTTP replay target/method no longer matches configuration')
            payload = (record_path / 'payload.hl7').read_bytes()
            request = HTTPRequest(target['url'], target['method'], (record_path / 'request.body').read_bytes(), payload, target['encoding'])
            future = _future()
            await destination.queue.put(_Delivery(payload, future, dict(record, replay_of=identity), True, None, request))
            result = await asyncio.shield(future)
        elif kind == 'message':
            state = self._states[record['route']]
            future = _future()
            await state.destinations[record['destination']].queue.put(_Delivery(
                (record_path / 'payload.hl7').read_bytes(), future, dict(record, replay_of=identity), True, None))
            result = await asyncio.shield(future)
        elif kind == 'transform':
            receipt = await self.submit(record['route'], (record_path / 'payload.hl7').read_bytes(), require_completion=True, _encoding=record.get('encoding'))
            result = await asyncio.shield(receipt.completed)
            if not result.success:
                (record_path / 'superseded').write_text(utcnow(), encoding='utf-8')
        else:
            raise ValueError('Input failures require inspection and explicit source resubmission')
        if result.success:
            (record_path / 'replayed').write_text(utcnow(), encoding='utf-8')
        return result

    async def close(self):
        if not self._started:
            return
        self._closing = True
        self._stop.set()
        for server in self.servers.values():
            await server.stop_accepting()
        async def drain():
            await self.wait_sources()
            # Topological handoffs may enqueue after an earlier queue was joined.
            while True:
                await self.flush()
                if all(not s.count for s in self._states.values()):
                    break
                await asyncio.sleep(0)
        draining = asyncio.create_task(drain())
        try:
            await asyncio.wait_for(asyncio.shield(draining), self.drain_timeout)
        except asyncio.TimeoutError:
            # Do not discard admitted work. Operator can repair storage, resume,
            # and call close again; this object still owns the live tasks.
            draining.cancel()
            await asyncio.gather(draining, return_exceptions=True)
            raise TimeoutError('Service drain timed out; admitted work is retained') from None
        for server in self.servers.values():
            await server.close()
        for state in self._states.values():
            await state.queue.put(None)
            await state.worker
            for destination in state.destinations.values():
                await destination.queue.put(None)
                await destination.task
                if destination.client:
                    await destination.client.close()
        self._started = False
