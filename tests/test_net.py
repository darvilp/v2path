import asyncio
import pytest
from v2path import parse, ParseError
from v2path.ack import AckRequest, make_ack, read_ack
from v2path.framing import MLLPDecoder, frame
from v2path.net import MLLPClient, MLLPServer, Acceptance
from v2path.delivery import DeliveryResult, Retry, RetryPolicy
from test_core import BASE


def msg(enhanced=False):
    m = parse(BASE + 'PID|||123\r')
    if enhanced:
        m['MSH-15'] = m['MSH-16'] = 'AL'
    return m


def test_framing():
    d = MLLPDecoder(100)
    payloads = []
    for b in frame(b'abc') + frame(b'def'):
        payloads += d.feed(bytes([b]))
    assert payloads == [b'abc', b'def']
    d.finish()
    with pytest.raises(ParseError):
        MLLPDecoder(2).feed(frame(b'abc'))
    with pytest.raises(ParseError):
        MLLPDecoder().feed(b'abc')


@pytest.mark.parametrize('a', ['AL','NE','ER','SU'])
@pytest.mark.parametrize('b', ['AL','NE','ER','SU'])
def test_ack_policy(a,b):
    m=msg();m['MSH-15']=a;m['MSH-16']=b
    request=AckRequest.from_message(m)
    assert not request.original and request.accept==a and request.application==b
    ack=make_ack(m,'CA',control_id='ACK1')
    assert ack['MSH-3']==m['MSH-5']
    assert ack['MSH-15']=='NE' and ack['MSH-16']=='NE'
    assert read_ack(ack.to_bytes(),m['MSH-10'])=='CA'


@pytest.mark.parametrize('enhanced', [False,True])
def test_persistent_roundtrip(enhanced):
    async def run():
        peers=[]
        async def accept(m,context):
            peers.append(context.peer)
            f=asyncio.get_running_loop().create_future();f.set_result(DeliveryResult('completed'))
            return Acceptance(f)
        async with MLLPServer('127.0.0.1',0,accept) as server:
            async with MLLPClient(*server.address[:2],ack_timeout=.5) as client:
                for _ in range(3):
                    result=await client.send(msg(enhanced))
                    assert result.success
                    assert len(result.acknowledgements)==(2 if enhanced else 1)
        assert len(set(peers))==1
    asyncio.run(run())


def test_negative_ack_retry_opt_in():
    async def run():
        calls=0
        async def reject(m, context):
            nonlocal calls
            calls+=1
            return DeliveryResult('failed')
        async with MLLPServer('127.0.0.1',0,reject,ack_mode='application') as server:
            async with MLLPClient(*server.address[:2]) as client:
                r=await client.send(msg()); assert r.code=='AE' and r.attempts==1
            async with MLLPClient(*server.address[:2],retry=RetryPolicy({'AE':Retry(3,.001)})) as client:
                r=await client.send(msg());assert r.code=='AE' and r.attempts==3
        assert calls==4
    asyncio.run(run())


def test_conditional_silence_and_correlation():
    async def run():
        async with MLLPServer('127.0.0.1',0,lambda m,c:DeliveryResult('completed')) as server:
            async with MLLPClient(*server.address[:2],ack_timeout=.03) as client:
                m=msg();m['MSH-15']='ER';m['MSH-16']='NE'
                assert (await client.send(m)).outcome=='unconfirmed'
    asyncio.run(run())


@pytest.mark.parametrize('accept', ['AL','NE','ER','SU'])
@pytest.mark.parametrize('application', ['AL','NE','ER','SU'])
def test_live_enhanced_qualifiers(accept, application):
    async def run():
        async with MLLPServer('127.0.0.1', 0, lambda m,c: DeliveryResult('completed')) as server:
            async with MLLPClient(*server.address[:2], ack_timeout=.15) as client:
                m = msg(True)
                m['MSH-15'], m['MSH-16'] = accept, application
                result = await client.send(m)
                assert result.outcome == ('unconfirmed' if 'ER' in (accept, application) else
                                          'sent' if (accept, application) == ('NE','NE') else 'confirmed')
    asyncio.run(run())


def test_first_byte_deadline_and_malformed_following_frame():
    async def run():
        received = []
        async with MLLPServer('127.0.0.1',0,lambda m,c: (received.append(m), DeliveryResult('completed'))[1],
                              frame_timeout=.03,idle_timeout=.5) as server:
            reader, writer = await asyncio.open_connection(*server.address[:2])
            await asyncio.sleep(.05)
            wire = frame(msg().to_bytes())
            writer.write(wire[:5]); await writer.drain()
            await asyncio.sleep(.005)
            writer.write(wire[5:]); await writer.drain()
            assert await asyncio.wait_for(reader.read(4096),.5)
            writer.write(wire + b'bad'); await writer.drain()
            await asyncio.sleep(.02)
            assert len(received) == 2
            assert len(server.errors) == 1
            writer.close(); await writer.wait_closed()
    asyncio.run(run())


def test_ack_literal_event_and_ascii_rejection():
    from v2path.net import AdmissionRejected
    m = msg()
    m.set_text('MSH-9.2', 'A|01')
    assert make_ack(m, 'AA')['MSH-9.2'] == 'A|01'
    async def run():
        def reject(m,c):
            raise AdmissionRejected('café unavailable')
        async with MLLPServer('127.0.0.1',0,reject) as server:
            async with MLLPClient(*server.address[:2]) as client:
                assert (await client.send(msg().to_bytes(encoding='ascii'))).code == 'AR'
    asyncio.run(run())


@pytest.mark.parametrize('response', ['wrong_id', 'wrong_stage', 'missing', 'disconnect'])
def test_unresolved_ack_closes_connection(response):
    async def run():
        peers = []
        handlers = set()
        async def handle(reader, writer):
            task = asyncio.current_task()
            handlers.add(task)
            peers.append(writer.get_extra_info('peername'))
            try:
                await reader.read(4096)
                if response in {'wrong_id', 'wrong_stage'}:
                    ack = make_ack(msg(), 'CA' if response == 'wrong_stage' else 'AA')
                    if response == 'wrong_id':
                        ack['MSA-2'] = 'WRONG'
                    writer.write(frame(ack.to_bytes()))
                    await writer.drain()
                elif response == 'missing':
                    await reader.read(4096)
            finally:
                writer.close()
                await writer.wait_closed()
                handlers.discard(task)
        server = await asyncio.start_server(handle, '127.0.0.1',0)
        async with server:
            async with MLLPClient(*server.sockets[0].getsockname()[:2],ack_timeout=.15) as client:
                for _ in range(2):
                    result = await client.send(msg())
                    assert result.outcome == ('protocol_error' if response.startswith('wrong') else 'uncertain')
                    assert client._writer is None
        if handlers:
            await asyncio.gather(*handlers)
        assert len(set(peers)) == 2
    asyncio.run(run())


def test_server_shutdown_during_accept_and_with_persistent_peer():
    import gc
    import warnings
    async def no_ack():
        async with MLLPServer('127.0.0.1',0,lambda m,c: DeliveryResult('completed')) as server:
            async with MLLPClient(*server.address[:2]) as client:
                m = msg(True); m['MSH-15'] = m['MSH-16'] = 'NE'
                assert (await client.send(m)).success
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always', ResourceWarning)
        for _ in range(20):
            asyncio.run(no_ack())
            gc.collect()
    assert not [w for w in caught if issubclass(w.category, ResourceWarning)]
    async def persistent():
        server = await MLLPServer('127.0.0.1',0,lambda m,c: DeliveryResult('completed')).start()
        reader, writer = await asyncio.open_connection(*server.address[:2])
        try:
            await asyncio.wait_for(server.close(), .5)
            try:
                assert await reader.read() == b''
            except ConnectionResetError:
                pass  # Closing a socket still being admitted may reset the peer.
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionResetError:
                pass
    asyncio.run(persistent())


def test_no_ack_does_not_require_or_invent_control_id():
    async def run():
        received = asyncio.Event()
        def accept(message, context):
            assert message['MSH-10'] == ''
            received.set()
            return DeliveryResult('completed')
        async with MLLPServer('127.0.0.1',0,accept) as server:
            async with MLLPClient(*server.address[:2]) as client:
                message = msg(True)
                message['MSH-10'] = ''
                message['MSH-15'] = message['MSH-16'] = 'NE'
                assert (await client.send(message)).outcome == 'sent'
                await asyncio.wait_for(received.wait(), .5)
    asyncio.run(run())


def test_frame_deadline_excludes_prior_message_processing():
    async def run():
        count = 0
        entered = asyncio.Event()
        async def accept(message, context):
            nonlocal count
            count += 1
            if count == 1:
                entered.set()
                await asyncio.sleep(.1)
            return DeliveryResult('completed')
        async with MLLPServer('127.0.0.1',0,accept,frame_timeout=.05) as server:
            reader, writer = await asyncio.open_connection(*server.address[:2])
            try:
                wire = frame(msg().to_bytes())
                writer.write(wire + wire[:5]); await writer.drain()
                await entered.wait()
                writer.write(wire[5:]); await writer.drain()
                decoder = MLLPDecoder()
                responses = []
                while len(responses) < 2:
                    data = await asyncio.wait_for(reader.read(4096), .5)
                    assert data
                    responses.extend(decoder.feed(data))
                assert count == 2 and not server.errors
            finally:
                writer.close(); await writer.wait_closed()
    asyncio.run(run())
