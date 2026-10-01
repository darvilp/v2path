"""Run an application-ACK route against a loopback sender, with no external server."""
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

from v2path import parse
from v2path.net import MLLPClient
from v2path.service import DirectoryDestination, MLLPSource, Route, Service


def transform(message):
    message['PID-3'] = 'TRANSFORMED'
    return message


async def main(root):
    async with Service([
        Route('adt', source=MLLPSource(port=0, ack_mode='application'),
              transform=transform,
              destinations={'files': DirectoryDestination(root / 'out')})
    ], failed_directory=root / 'failed') as service:
        message = parse('MSH|^~\\&|SRC|FAC|DST|FAC|20260101||ADT^A01|demo-1|P|2.5.1\rPID|||123\r')
        async with MLLPClient(*service.servers['adt'].address[:2]) as client:
            result = await client.send(message)
            assert result.success and result.code == 'AA'
        output = next((root / 'out').glob('*.hl7'))
        assert parse(output.read_bytes())['PID-3'] == 'TRANSFORMED'
        assert not service.failures.records()


if __name__ == '__main__':
    with TemporaryDirectory() as directory:
        asyncio.run(main(Path(directory)))
    print('MLLP route, transformation, atomic file publication, and application ACK passed.')
