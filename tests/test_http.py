import asyncio
import inspect
import json
import shutil
import ssl
import subprocess

import pytest

from v2path.ack import make_ack
from v2path.delivery import DeliveryResult, Retry, RetryPolicy
from v2path.http import HTTPClient, HTTPDestination
from v2path.service import Service, Route, DirectoryDestination
from test_net import msg


class Server:
    def __init__(self, reply=None, ssl=None):
        self.reply = reply or (lambda request, count: (200, {}, b'OK'))
        self.ssl, self.requests, self.peers, self.tasks = ssl, [], [], set()

    async def __aenter__(self):
        self.server = await asyncio.start_server(self.handle, '127.0.0.1', 0, ssl=self.ssl)
        self.url = ('https' if self.ssl else 'http') + '://127.0.0.1:' + str(self.server.sockets[0].getsockname()[1]) + '/messages?source=test'
        return self

    async def __aexit__(self, *exc):
        self.server.close()
        for task in tuple(self.tasks):
            task.cancel()
        await asyncio.gather(*tuple(self.tasks), return_exceptions=True)
        await self.server.wait_closed()

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        self.peers.append(writer.get_extra_info('peername'))
        try:
            while True:
                raw = await reader.readuntil(b'\r\n\r\n')
                lines = raw.decode('latin1').split('\r\n')
                headers = dict((k.lower(), v.strip()) for k,v in (line.split(':',1) for line in lines[1:] if line))
                body = await reader.readexactly(int(headers.get('content-length',0)))
                request = dict(line=lines[0], headers=headers, body=body)
                self.requests.append(request)
                response = self.reply(request, len(self.requests))
                if inspect.isawaitable(response):
                    response = await response
                if response is None:
                    return
                if not isinstance(response, bytes):
                    status, output_headers, body = response
                    output_headers = {'Content-Length': str(len(body)), **output_headers}
                    response = (f'HTTP/1.1 {status} Test\r\n' + ''.join(f'{k}: {v}\r\n' for k,v in output_headers.items()) + '\r\n').encode('latin1') + body
                writer.write(response)
                await writer.drain()
                if b'Connection: close' in response:
                    return
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            try:
                await asyncio.shield(writer.wait_closed())
            except (ConnectionError, OSError):
                pass
            self.tasks.discard(task)


def run(coro):
    async def bounded():
        return await asyncio.wait_for(coro, 10)
    asyncio.run(bounded())


def test_persistent_raw_and_statuses():
    async def scenario():
        async with Server(lambda r,n: ([200,202,204][n-1], {}, b'')) as server:
            async with HTTPClient(server.url) as client:
                for _ in range(3):
                    result = await client.send(msg())
                    assert result.success and result.http_response.status in {200,202,204}
            assert len(server.peers) == 1
            assert all(r['body'] == msg().to_bytes() for r in server.requests)
            assert server.requests[0]['headers']['content-type'] == 'application/hl7-v2'
    run(scenario())


def test_encoder_frozen_and_credentials_refresh():
    async def scenario():
        encoded = 0
        async def encode(message):
            nonlocal encoded
            encoded += 1
            return json.dumps({'hl7':message.to_text()}).encode()
        async def headers(message, body, attempt):
            assert json.loads(body)['hl7'] == message.to_text()
            return {'authorization':f'Bearer fresh-{attempt}', 'CONTENT-TYPE':'application/json'}
        async with Server(lambda r,n: (503 if n == 1 else 200, {}, b'OK')) as server:
            async with HTTPClient(server.url, encoder=encode, headers={'Authorization':'old'}, header_provider=headers,
                                  retry=RetryPolicy({'http:503':Retry(2)})) as client:
                result = await client.send(msg())
                assert result.success and result.attempts == 2
            assert encoded == 1
            assert server.requests[0]['body'] == server.requests[1]['body']
            assert [r['headers']['authorization'] for r in server.requests] == ['Bearer fresh-1','Bearer fresh-2']
    run(scenario())


@pytest.mark.parametrize('status', [302,400,401,429,500,503])
def test_no_implicit_redirect_or_retry(status):
    async def scenario():
        async with Server(lambda r,n: (status, {'Location':'/elsewhere','Retry-After':'0'}, b'failure')) as server:
            async with HTTPClient(server.url) as client:
                result = await client.send(msg())
                assert result.outcome == 'http_error' and result.attempts == 1
            assert len(server.requests) == 1
    run(scenario())


@pytest.mark.parametrize('code', ['AA','CA','AE','AR','CE','CR','mismatch','invalid'])
def test_hl7_ack_policy(code):
    async def scenario():
        ack = make_ack(msg(), code if code not in {'mismatch','invalid'} else 'AA')
        if code == 'mismatch':
            ack['MSA-2'] = 'wrong'
        body = b'not an ack' if code == 'invalid' else ack.to_bytes()
        async with Server(lambda r,n:(200,{},body)) as server:
            async with HTTPClient(server.url,response='hl7_ack') as client:
                result = await client.send(msg())
                assert result.success == (code in {'AA','CA'})
                assert result.outcome == ('response_error' if code in {'invalid','mismatch'} else 'confirmed' if code in {'AA','CA'} else 'negative_ack')
    run(scenario())


def test_custom_status_and_evaluator():
    async def scenario():
        async def evaluate(response, message):
            assert response.status == 409 and message['MSH-10']
            return DeliveryResult('confirmed' if json.loads(response.body)['duplicate'] else 'failed')
        async with Server(lambda r,n:(409,{},b'{"duplicate":true}')) as server:
            async with HTTPClient(server.url,accepted_statuses={409}) as client:
                assert (await client.send(msg())).success
            async with HTTPClient(server.url,response=evaluate) as client:
                assert (await client.send(msg())).success
    run(scenario())


@pytest.mark.parametrize('headers', [{'Host':'other'}, {'Content-Length':'3'}, {'Transfer-Encoding':'chunked'},
                                    {'X\r\nBad':'yes'}, {'Authorization':'bad\r\nInjected: yes'}])
def test_header_validation(headers):
    with pytest.raises(ValueError):
        HTTPClient('http://localhost:1234',headers=headers)
    async def scenario():
        async with Server() as server:
            async with HTTPClient(server.url,header_provider=lambda *args:headers) as client:
                assert (await client.send(msg())).outcome == 'request_error'
            assert not server.requests
    run(scenario())


@pytest.mark.parametrize('response,outcome', [
    (b'HTTP/1.1 200 OK\r\nContent-Length: 10\r\nConnection: close\r\n\r\nabc','uncertain'),
    (b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Length: 3\r\n\r\nabc','protocol_error'),
    (b'HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n'+b'x'*100,'protocol_error'),
    (b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n2\r\nOK\r\n0\r\n\r\n','confirmed'),
    (b'HTTP/1.1 200 OK\r\nContent-Length: \xb2\r\nConnection: close\r\n\r\n','protocol_error'),
    (None,'uncertain'),
])
def test_response_limits_and_framing(response,outcome):
    async def scenario():
        async with Server(lambda r,n: response) as server:
            async with HTTPClient(server.url,max_response_bytes=20) as client:
                assert (await client.send(msg())).outcome == outcome
    run(scenario())


def test_timeouts_cancellation_and_reuse():
    async def scenario():
        entered = asyncio.Event()
        async def reply(request,count):
            if count in {1,2}:
                entered.set()
                await asyncio.sleep(1)
            return 200,{},b'OK'
        async with Server(reply) as server:
            async with HTTPClient(server.url,response_timeout=.1) as client:
                assert (await client.send(msg())).outcome == 'uncertain'
                entered.clear()
                sending = asyncio.create_task(client.send(msg()))
                await entered.wait()
                sending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(sending,1)
                assert (await client.send(msg())).success
            assert len(server.peers) == 3
    run(scenario())


def test_fanout_failure_replay_exact_request(tmp_path):
    async def scenario():
        calls = 0
        def encode(m):
            nonlocal calls
            calls += 1
            return b'WRAPPED:' + m.to_bytes()
        reject = True
        async with Server(lambda r,n:(503 if reject else 200, {'Set-Cookie':'secret','X-Request-ID':'diagnostic'}, b'response')) as server:
            routes = [Route('r', {'http':HTTPDestination(server.url,encoder=encode,headers={'Authorization':'SECRET'}),
                                  'copy':DirectoryDestination(tmp_path/'out',mode='message')})]
            async with Service(routes,failed_directory=tmp_path/'failed') as service:
                receipt = await service.submit('r',msg(),require_completion=True)
                assert (await receipt.completed).outcome == 'http_error'
                record = service.failures.records()[0]
                metadata = json.loads((record/'record.json').read_text())
                assert metadata['kind'] == 'http' and metadata['http_response']['status'] == 503
                assert 'SECRET' not in (record/'record.json').read_text() and 'Set-Cookie' not in (record/'record.json').read_text()
                assert (record/'request.body').read_bytes() == server.requests[0]['body']
                assert not (await service.replay(record.name)).success
                assert len(service.failures.records()) == 1
                reject = False
                assert (await service.replay(record.name)).success
                assert calls == 1 and all(r['body'] == server.requests[0]['body'] for r in server.requests)
                assert len(list((tmp_path/'out').glob('*.hl7'))) == 1
                with pytest.raises(ValueError):
                    await service.replay(record.name)
    run(scenario())


@pytest.fixture(scope='session')
def certificates(tmp_path_factory):
    if not shutil.which('openssl'):
        pytest.skip('openssl is needed only to generate TLS test certificates')
    root = tmp_path_factory.mktemp('http-tls')
    cert, key = root/'cert.pem', root/'key.pem'
    subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(cert),
                    '-days','1','-subj','/CN=localhost','-addext','subjectAltName=IP:127.0.0.1'],
                    check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    return cert,key


def test_https_certificate_verification(certificates):
    cert,key = certificates
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert,key)
    async def scenario():
        async with Server(ssl=server_context) as server:
            async with HTTPClient(server.url) as untrusted:
                assert (await untrusted.send(msg())).outcome == 'connection_error'
            async with HTTPClient(server.url,ssl=ssl.create_default_context(cafile=str(cert))) as trusted:
                assert (await trusted.send(msg())).success
                assert (await trusted.send(msg())).success
    run(scenario())


def test_informational_responses_do_not_acknowledge_next_request():
    async def scenario():
        async with Server(lambda r,n: b'HTTP/1.1 103 Early Hints\r\nLink: </x>\r\n\r\nHTTP/1.1 102 Processing\r\n\r\nHTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK' if n == 1 else (500,{},b'failed')) as server:
            async with HTTPClient(server.url) as client:
                assert (await client.send(msg())).http_response.status == 200
                second = await client.send(msg())
                assert second.outcome == 'http_error' and second.http_response.status == 500
    run(scenario())


def test_cancel_stalled_tls_handshake():
    async def scenario():
        entered = asyncio.Event()
        tasks = set()
        async def stall(reader, writer):
            task = asyncio.current_task(); tasks.add(task)
            try:
                await reader.read(4096)
                entered.set()
                await reader.read()
            finally:
                writer.close()
                await writer.wait_closed()
                tasks.discard(task)
        server = await asyncio.start_server(stall,'127.0.0.1',0)
        async with server:
            url = 'https://127.0.0.1:' + str(server.sockets[0].getsockname()[1])
            async with HTTPClient(url,connect_timeout=10) as client:
                task = asyncio.create_task(client.send(msg()))
                await entered.wait()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task,1)
            if tasks:
                await asyncio.gather(*tasks)
    run(scenario())


def test_callback_failures_limits_and_retry_precedence():
    async def scenario():
        def broken(*args):
            raise ValueError('private error text')
        async with Server(lambda r,n:(200,{},make_ack(msg(),'AE').to_bytes())) as server:
            async with HTTPClient(server.url,encoder=broken) as client:
                assert (await client.send(msg())).outcome == 'request_error'
            async with HTTPClient(server.url,max_request_bytes=2) as client:
                assert (await client.send(msg())).outcome == 'request_error'
            assert not server.requests
            async with HTTPClient(server.url,response=broken) as client:
                result = await client.send(msg())
                assert result.outcome == 'response_error' and result.detail == 'ValueError'
            async with HTTPClient(server.url,response='hl7_ack',retry=RetryPolicy({'AE':Retry(2),'http:200':Retry(3),'negative_ack':Retry(4)})) as client:
                assert (await client.send(msg())).attempts == 2
    run(scenario())


def test_close_interrupts_response_and_retry_delay():
    async def scenario():
        entered = asyncio.Event()
        async def hang(r,n):
            entered.set()
            await asyncio.sleep(5)
            return 200,{},b'OK'
        async with Server(hang) as server:
            client = HTTPClient(server.url)
            sending = asyncio.create_task(client.send(msg()))
            await entered.wait()
            await asyncio.wait_for(client.close(),1)
            assert (await sending).outcome == 'uncertain'
        async with Server(lambda r,n:(503,{},b'')) as server:
            client = HTTPClient(server.url,retry=RetryPolicy({'http:503':Retry(3,30)}))
            sending = asyncio.create_task(client.send(msg()))
            while not server.requests:
                await asyncio.sleep(.001)
            await asyncio.sleep(.02)
            await asyncio.wait_for(client.close(),1)
            assert (await sending).attempts == 1
            assert len(server.requests) == 1
    run(scenario())


def test_service_retry_order_independence_and_storage_recovery(tmp_path, monkeypatch):
    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()
        async def fail(r,n):
            if n == 1:
                entered.set()
                await release.wait()
            return 503,{},b'failed'
        async with Server(fail) as server:
            route = Route('r', {'http':HTTPDestination(server.url), 'file':DirectoryDestination(tmp_path/'out',mode='message')},
                          retry=RetryPolicy({'http:503':Retry(2)}))
            async with Service([route],failed_directory=tmp_path/'failed') as service:
                a = msg(); a['MSH-10'] = 'first'
                b = msg(); b['MSH-10'] = 'second'
                first = await service.submit('r',a,require_completion=True)
                await entered.wait()
                second = await service.submit('r',b,require_completion=True)
                await second.dispatched
                for _ in range(100):
                    if len(list((tmp_path/'out').glob('*.hl7'))) == 2:
                        break
                    await asyncio.sleep(.005)
                assert len(list((tmp_path/'out').glob('*.hl7'))) == 2
                write = service.failures.write
                def unavailable(*args, **kwargs):
                    raise OSError('storage unavailable')
                monkeypatch.setattr(service.failures,'write',unavailable)
                release.set()
                while not service._paused:
                    await asyncio.sleep(.001)
                assert not first.completed.done()
                monkeypatch.setattr(service.failures,'write',write)
                service.resume()
                assert not (await first.completed).success
                assert not (await second.completed).success
                assert [r['body'] for r in server.requests] == [a.to_bytes(),a.to_bytes(),b.to_bytes(),b.to_bytes()]
                assert len(service.failures.records()) == 2
    run(scenario())


def test_replay_after_restart_and_target_mismatch(tmp_path):
    async def scenario():
        reject = True
        async with Server(lambda r,n:(500 if reject else 200,{},b'OK')) as server:
            async with Service([Route('r', {'http':HTTPDestination(server.url,encoder=lambda m:b'FROZEN',headers={'Authorization':'old'})})],
                               failed_directory=tmp_path/'failed') as first:
                receipt = await first.submit('r',msg())
                assert not (await receipt.completed).success
                identity = first.failures.records()[0].name
            async with Service([Route('r', {'http':HTTPDestination(server.url+'/different')})],failed_directory=tmp_path/'failed') as changed:
                with pytest.raises(ValueError,match='target/method'):
                    await changed.replay(identity)
            def never_encode(m):
                raise AssertionError('Replay must not encode')
            reject = False
            async with Service([Route('r', {'http':HTTPDestination(server.url,encoder=never_encode,header_provider=lambda *a:{'Authorization':'fresh'})})],
                               failed_directory=tmp_path/'failed') as restarted:
                assert (await restarted.replay(identity)).success
            assert server.requests[-1]['body'] == b'FROZEN'
            assert server.requests[-1]['headers']['authorization'] == 'fresh'
    run(scenario())


def test_slow_response_has_whole_phase_deadline():
    async def scenario():
        tasks = set()
        async def trickle(reader,writer):
            task = asyncio.current_task(); tasks.add(task)
            try:
                await reader.read(4096)
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\n'); await writer.drain()
                for _ in range(10):
                    writer.write(b'x'); await writer.drain()
                    await asyncio.sleep(.04)
            except ConnectionError:
                pass
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except ConnectionError:
                    pass
                tasks.discard(task)
        server = await asyncio.start_server(trickle,'127.0.0.1',0)
        async with server:
            async with HTTPClient('http://127.0.0.1:'+str(server.sockets[0].getsockname()[1]),response_timeout=.1) as client:
                result = await client.send(msg())
                assert result.outcome == 'uncertain'
            if tasks:
                await asyncio.gather(*tasks)
    run(scenario())


def test_http_failure_diagnostics_bound_ack_body(tmp_path):
    import base64
    async def scenario():
        ack = make_ack(msg(),'AE')
        ack.append('NTE|||'+ 'x'*70000)
        body = ack.to_bytes()
        async with Server(lambda r,n:(200,{'Set-Cookie':'private'},body)) as server:
            async with Service([Route('r', {'http':HTTPDestination(server.url,response='hl7_ack')})],failed_directory=tmp_path/'failed') as service:
                receipt = await service.submit('r',msg())
                assert (await receipt.completed).code == 'AE'
                data = json.loads((service.failures.records()[0]/'record.json').read_text())
                assert data['acknowledgements'] == []
                assert data['http_response']['truncated']
                assert len(base64.b64decode(data['http_response']['body_base64'])) == 65536
    run(scenario())


def test_prepared_request_ack_policy_and_url_validation():
    with pytest.raises(ValueError):
        HTTPClient('http://localhost:0')
    async def scenario():
        async with Server() as server:
            message = msg(); message['MSH-10'] = ''
            async with HTTPClient(server.url) as preparer:
                request = await preparer.prepare(message)
            async with HTTPClient(server.url,response='hl7_ack') as sender:
                assert (await sender.send_prepared(request)).outcome == 'request_error'
            assert not server.requests
    run(scenario())
