import asyncio
import json
from pathlib import Path

import pytest

from v2path import iter_messages
from v2path.delivery import DeliveryResult, Retry, RetryPolicy
from v2path.files import DirectoryDestination, DirectorySource, FileDestination, FileSource, filename
from v2path.net import MLLPClient, MLLPServer
from v2path.service import MLLPDestination, MLLPSource, Route, RouteDestination, Service
from test_net import msg


def run(coro):
    async def bounded():
        return await asyncio.wait_for(coro, 5)
    return asyncio.run(bounded())


def test_internal_fanout_and_route_handoff(tmp_path):
    async def scenario():
        count = 0
        def transform(m):
            nonlocal count
            count += 1
            m['PID-3'] = 'changed'
            return [m, m.copy()]
        async with Service([
            Route('first', {'next': RouteDestination('second', 'completion'),
                            'copy': DirectoryDestination(tmp_path/'copy', mode='message')}, transform=transform),
            Route('second', {'out': DirectoryDestination(tmp_path/'out')})
        ], failed_directory=tmp_path/'failed') as service:
            receipt = await service.submit('first', msg(), require_completion=True)
            assert (await receipt.completed).success
        assert count == 1
        assert len(list((tmp_path/'copy').glob('*.hl7'))) == 2
        assert len(list((tmp_path/'out').glob('*.hl7'))) == 2
        assert not list((tmp_path/'failed').iterdir())
    run(scenario())


def test_batch_pressure_timer_and_shutdown(tmp_path):
    async def scenario():
        async with Service([Route('r', {'out': DirectoryDestination(tmp_path/'out', max_seconds=.04)}, capacity=2)],
                           failed_directory=tmp_path/'failed') as service:
            receipts = [await service.submit('r', msg()) for _ in range(5)]
            await asyncio.gather(*(r.completed for r in receipts))
        paths = list((tmp_path/'out').glob('*.hl7'))
        assert sorted(len(list(iter_messages(p))) for p in paths) == [1,2,2]
    run(scenario())


@pytest.mark.parametrize('kind', ['fixed', 'preserve', 'batch', 'message'])
def test_file_source_output_modes(tmp_path, kind):
    source = tmp_path/'input.hl7'
    source.write_bytes(msg().to_bytes()*5)
    endpoint = FileDestination(tmp_path/'out.hl7') if kind == 'fixed' else DirectoryDestination(tmp_path/'out', mode=kind)
    async def scenario():
        async with Service([Route('r', {'out': endpoint}, source=FileSource(source), capacity=2)],
                           failed_directory=tmp_path/'failed') as service:
            await service.wait_sources()
        assert not source.exists()
        assert len(list((tmp_path/'.v2path-archive').glob('*.hl7'))) == 1
        paths = [tmp_path/'out.hl7'] if kind == 'fixed' else list((tmp_path/'out').glob('*.hl7'))
        assert sum(len(list(iter_messages(p))) for p in paths) == 5
        if kind in {'fixed', 'preserve'}:
            assert len(paths) == 1
        assert len(paths) == 5 if kind == 'message' else True
    run(scenario())


def test_network_to_file_application_ack_waits_for_publication(tmp_path):
    async def scenario():
        async with Service([Route('r', {'out': DirectoryDestination(tmp_path/'out')},
                    source=MLLPSource(port=0, ack_mode='application'))], failed_directory=tmp_path/'failed') as service:
            async with MLLPClient(*service.servers['r'].address[:2]) as client:
                result = await client.send(msg())
                assert result.success
                assert len(list((tmp_path/'out').glob('*.hl7'))) == 1
    run(scenario())


def test_network_to_network_and_file_to_network(tmp_path):
    async def scenario():
        received = []
        async with MLLPServer('127.0.0.1', 0, lambda m,c: (received.append(m), DeliveryResult('completed'))[1]) as sink:
            endpoint = MLLPDestination(*sink.address[:2])
            source = tmp_path/'in.hl7'
            source.write_bytes(msg().to_bytes()*2)
            async with Service([
                Route('file', {'out': endpoint}, source=FileSource(source)),
                Route('net', {'out': endpoint}, source=MLLPSource(port=0, ack_mode='application'))
            ], failed_directory=tmp_path/'failed') as service:
                await service.wait_sources()
                async with MLLPClient(*service.servers['net'].address[:2]) as client:
                    assert (await client.send(msg())).success
            assert len(received) == 3
    run(scenario())


def test_failure_fanout_retry_and_replay_only_failed_destination(tmp_path):
    async def scenario():
        calls = 0
        reject = True
        def handler(m,c):
            nonlocal calls
            calls += 1
            return DeliveryResult('failed' if reject else 'completed')
        async with MLLPServer('127.0.0.1',0,handler,ack_mode='application') as sink:
            async with Service([Route('r', {
                'good': DirectoryDestination(tmp_path/'good', mode='message'),
                'bad': MLLPDestination(*sink.address[:2])}, retry=RetryPolicy({'AE': Retry(2)}))],
                failed_directory=tmp_path/'failed') as service:
                receipt = await service.submit('r', msg())
                assert (await receipt.completed).code == 'AE'
                assert calls == 2
                records = service.failures.records()
                assert len(records) == 1
                data = json.loads((records[0]/'record.json').read_text())
                assert data['destination'] == 'bad' and data['attempts'] == 2
                assert (records[0]/'payload.hl7').read_bytes() == msg().to_bytes()
                reject = False
                assert (await service.replay(records[0].name)).success
                with pytest.raises(ValueError):
                    await service.replay(records[0].name)
                assert calls == 3
                assert len(list((tmp_path/'good').glob('*.hl7'))) == 1
    run(scenario())


def test_no_clobber_and_shared_batch_failure(tmp_path):
    (tmp_path/'out').mkdir()
    existing = tmp_path/'out'/'batch.hl7'
    existing.write_bytes(b'keep')
    async def scenario():
        async with Service([Route('r', {'out': DirectoryDestination(tmp_path/'out', mode='batch', template='batch.hl7')}, capacity=2)],
                           failed_directory=tmp_path/'failed') as service:
            first = await service.submit('r', msg())
            second = await service.submit('r', msg())
            assert not (await first.completed).success
            assert not (await second.completed).success
            assert existing.read_bytes() == b'keep'
            records = service.failures.records()
            assert len(records) == 1
            assert len((records[0]/'members.jsonl').read_text().splitlines()) == 2
            existing.unlink()
            assert (await service.replay(records[0].name)).success
            assert len(list(iter_messages(existing))) == 2
    run(scenario())


@pytest.mark.parametrize('readiness', ['atomic','marker','stable'])
def test_directory_claim_archive_and_recovery(tmp_path, readiness):
    root = tmp_path/'in'
    root.mkdir()
    claimed = root/'.v2path-claimed'
    claimed.mkdir()
    (claimed/'abandoned.hl7').write_bytes(b'unfinished')
    (root/'ignore.tmp').write_bytes(b'temporary')
    (root/'input.hl7').write_bytes(msg().to_bytes())
    if readiness == 'marker':
        (root/'input.hl7.ready').touch()
    async def scenario():
        async with Service([Route('r', {'out': DirectoryDestination(tmp_path/'out')},
            source=DirectorySource(root, readiness=readiness, poll_interval=.01, stability=.02))],
            failed_directory=tmp_path/'failed') as service:
            for _ in range(100):
                if list((root/'.v2path-archive').glob('*.hl7')):
                    break
                await asyncio.sleep(.01)
            else:
                pytest.fail('Directory source did not archive input')
            assert len(list((root/'.v2path-quarantine').iterdir())) == 1
        assert (root/'ignore.tmp').exists()
        assert len(list((tmp_path/'out').glob('*.hl7'))) == 1
    run(scenario())


def test_directory_chain_and_config_rejection(tmp_path):
    async def scenario():
        async with Service([
            Route('first', {'out': DirectoryDestination(tmp_path/'middle')}),
            Route('second', {'out': DirectoryDestination(tmp_path/'final')},
                  source=DirectorySource(tmp_path/'middle', poll_interval=.01))
        ], failed_directory=tmp_path/'failed') as service:
            receipt = await service.submit('first', msg())
            assert (await receipt.completed).success
            for _ in range(100):
                if list((tmp_path/'final').glob('*.hl7')):
                    break
                await asyncio.sleep(.01)
            else:
                pytest.fail('Chained directory did not deliver')
    run(scenario())
    with pytest.raises(ValueError, match='cycle'):
        Service([Route('a', {'next': RouteDestination('b')}), Route('b', {'next': RouteDestination('a')})], failed_directory=tmp_path/'f')
    with pytest.raises(ValueError, match='atomic'):
        Service([Route('a', {'next': DirectoryDestination(tmp_path/'b')}),
                 Route('b', {'out': DirectoryDestination(tmp_path/'c')}, source=DirectorySource(tmp_path/'b', readiness='stable'))], failed_directory=tmp_path/'f')
    with pytest.raises(ValueError):
        filename('{source_stem}.hl7', source_stem='../bad')


def test_transform_failure_retains_original_and_filter_success(tmp_path):
    def broken(m):
        m['PID-3'] = 'modified'
        raise ValueError('bad mapping')
    async def scenario():
        async with Service([Route('r', {'out': DirectoryDestination(tmp_path/'out')}, transform=broken)],
                           failed_directory=tmp_path/'failed') as service:
            receipt = await service.submit('r', msg())
            assert (await receipt.completed).outcome == 'transform_error'
            record = service.failures.records()[0]
            assert (record/'payload.hl7').read_bytes() == msg().to_bytes()
            service.routes['r'].transform = lambda m: None
            assert (await service.replay(record.name)).success
            assert not (tmp_path/'out').exists()
    run(scenario())


def test_failure_store_pause_and_resume_retains_work(tmp_path, monkeypatch):
    async def scenario():
        def broken(m):
            raise ValueError('failure')
        async with Service([Route('r', {'out': DirectoryDestination(tmp_path/'out')}, transform=broken)],
                           failed_directory=tmp_path/'failed') as service:
            write = service.failures.write
            monkeypatch.setattr(service.failures, 'write', lambda *a, **k: (_ for _ in ()).throw(OSError('disk full')))
            receipt = await service.submit('r', msg())
            while not service._paused:
                await asyncio.sleep(0)
            assert not receipt.completed.done() and service._states['r'].count == 1
            from v2path.net import AdmissionRejected
            with pytest.raises(AdmissionRejected):
                await service.submit('r', msg())
            monkeypatch.setattr(service.failures, 'write', write)
            service.resume()
            assert (await receipt.completed).outcome == 'transform_error'
            assert len(service.failures.records()) == 1
    run(scenario())


def test_shutdown_timeout_is_resumable(tmp_path):
    async def scenario():
        from v2path.net import Acceptance
        completed = asyncio.get_running_loop().create_future()
        entered = asyncio.Event()
        def hold(m,c):
            entered.set()
            return Acceptance(completed)
        source = tmp_path/'in.hl7'; source.write_bytes(msg().to_bytes())
        async with MLLPServer('127.0.0.1',0,hold,ack_mode='application') as sink:
            service = await Service([Route('r', {'out': MLLPDestination(*sink.address[:2])}, source=FileSource(source))],
                                    failed_directory=tmp_path/'failed',drain_timeout=.02).start()
            await entered.wait()
            with pytest.raises(TimeoutError):
                await service.close()
            assert not service._source_tasks[0].cancelled()
            completed.set_result(DeliveryResult('completed'))
            service.drain_timeout = 1
            await service.close()
            assert not source.exists()
    run(scenario())


def test_bad_archive_and_incomplete_batch_replay(tmp_path):
    with pytest.raises(ValueError, match='Archive'):
        Service([Route('r', {'out': DirectoryDestination(tmp_path/'out')},
                       source=DirectorySource(tmp_path/'in', archive=tmp_path/'in'))], failed_directory=tmp_path/'f')
    async def scenario():
        async with Service([Route('r', {'out': DirectoryDestination(tmp_path/'out', mode='batch', framing='mllp', encoding='ascii')})],
                           failed_directory=tmp_path/'failed') as service:
            first = await service.submit('r', msg())
            bad = msg(); bad.set_text('PID-5','é')
            second = await service.submit('r', bad)
            assert not (await first.completed).success
            assert not (await second.completed).success
            records = service.failures.records()
            incomplete = next(p for p in records if json.loads((p/'record.json').read_text())['kind'] == 'artifact_incomplete')
            with pytest.raises(ValueError):
                await service.replay(incomplete.name)
            assert not list((tmp_path/'out').glob('*.hl7'))
            failed_message = next(p for p in records if json.loads((p/'record.json').read_text())['kind'] == 'message')
        async with Service([Route('r', {'out': DirectoryDestination(tmp_path/'out', encoding='utf-8')})],
                           failed_directory=tmp_path/'failed') as repaired:
            assert (await repaired.replay(failed_message.name)).success
            assert len(list((tmp_path/'out').glob('*.hl7'))) == 1
    run(scenario())


def test_preserve_group_with_slow_fanout_stays_one_file(tmp_path):
    async def scenario():
        async def sink(m,c):
            await asyncio.sleep(.005)
            return DeliveryResult('completed')
        source = tmp_path/'in.hl7'; source.write_bytes(msg().to_bytes()*6)
        async with MLLPServer('127.0.0.1',0,sink) as server:
            async with Service([Route('r', {'file': DirectoryDestination(tmp_path/'out'),
                    'net': MLLPDestination(*server.address[:2])}, source=FileSource(source), capacity=2)],
                    failed_directory=tmp_path/'failed') as service:
                await service.wait_sources()
            paths = list((tmp_path/'out').glob('*.hl7'))
            assert len(paths) == 1 and len(list(iter_messages(paths[0]))) == 6
    run(scenario())


def test_source_encoding_override_survives_handoff(tmp_path):
    m = msg(); m.set_text('PID-5', 'André')
    payload = m.to_bytes(encoding='latin1').replace(b'8859/1', b'UNKNOWN')
    source = tmp_path/'in.hl7'; source.write_bytes(payload)
    async def scenario():
        async with Service([
            Route('r', {'out': RouteDestination('next','completion')}, source=FileSource(source,encoding='latin1')),
            Route('next', {'out': DirectoryDestination(tmp_path/'out', mode='message')})
        ], failed_directory=tmp_path/'failed') as service:
            await service.wait_sources()
        assert next((tmp_path/'out').glob('*.hl7')).read_bytes() == payload
    run(scenario())


def test_completion_handoff_does_not_retry_successful_downstream_siblings(tmp_path):
    async def scenario():
        calls = 0
        def reject(m,c):
            nonlocal calls
            calls += 1
            return DeliveryResult('failed')
        async with MLLPServer('127.0.0.1',0,reject,ack_mode='application') as sink:
            async with Service([
                Route('first', {'next': RouteDestination('second','completion')}, retry=RetryPolicy({'AE': Retry(3)})),
                Route('second', {'good': DirectoryDestination(tmp_path/'good', mode='message'),
                                 'bad': MLLPDestination(*sink.address[:2])})
            ], failed_directory=tmp_path/'failed') as service:
                receipt = await service.submit('first', msg(), require_completion=True)
                assert (await receipt.completed).outcome == 'downstream_failure'
                assert calls == 1
                records = service.failures.records()
                assert len(records) == 1
                assert not (await service.replay(records[0].name)).success
                assert len(service.failures.records()) == 1
                assert len(list((tmp_path/'good').glob('*.hl7'))) == 1
    run(scenario())


def test_application_fanout_does_not_wait_for_slow_sibling(tmp_path):
    async def scenario():
        from v2path.net import Acceptance
        gate = asyncio.get_running_loop().create_future()
        entered = asyncio.Event()
        def hold(m,c):
            entered.set()
            return Acceptance(gate)
        async with MLLPServer('127.0.0.1',0,hold,ack_mode='application') as sink:
            async with Service([Route('r', {'slow': MLLPDestination(*sink.address[:2]),
                    'fast': DirectoryDestination(tmp_path/'out', mode='message')})], failed_directory=tmp_path/'failed') as service:
                a = await service.submit('r', msg(), require_completion=True)
                await entered.wait()
                b = await service.submit('r', msg(), require_completion=True)
                await b.dispatched
                for _ in range(50):
                    if len(list((tmp_path/'out').glob('*.hl7'))) == 2:
                        break
                    await asyncio.sleep(.005)
                assert len(list((tmp_path/'out').glob('*.hl7'))) == 2
                assert not a.completed.done() and not b.completed.done()
                gate.set_result(DeliveryResult('completed'))
                assert (await a.completed).success and (await b.completed).success
    run(scenario())


def test_async_transform_and_generated_batch_counts(tmp_path):
    source = tmp_path/'in.hl7'; source.write_bytes(msg().to_bytes()*3)
    async def scenario():
        calls = 0
        async def transform(message):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0)
            return None if calls == 2 else [message, message.copy()]
        async with Service([Route('r', {'out': DirectoryDestination(tmp_path/'out', batch_header='BHS|^~\\&|SRC', file_header='FHS|^~\\&|SRC')},
                                  source=FileSource(source),transform=transform)], failed_directory=tmp_path/'failed') as service:
            await service.wait_sources()
        data = next((tmp_path/'out').glob('*.hl7')).read_bytes()
        assert data.startswith(b'FHS') and data.endswith(b'BTS|4\rFTS|1\r')
        assert len(list(iter_messages([data]))) == 4
    run(scenario())


def test_file_and_directory_ownership_overlap(tmp_path):
    with pytest.raises(ValueError, match='owned'):
        Service([
            Route('file', {'out': DirectoryDestination(tmp_path/'a')}, source=FileSource(tmp_path/'in'/'m.hl7')),
            Route('directory', {'out': DirectoryDestination(tmp_path/'b')}, source=DirectorySource(tmp_path/'in')),
        ], failed_directory=tmp_path/'failed')
    with pytest.raises(ValueError, match='overlaps'):
        Service([
            Route('file', {'out': FileDestination(tmp_path/'out'/'m.hl7')}, source=FileSource(tmp_path/'input.hl7')),
            Route('directory', {'out': DirectoryDestination(tmp_path/'out')}),
        ], failed_directory=tmp_path/'failed')
