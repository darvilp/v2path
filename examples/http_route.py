"""A local HTTP destination with JSON encoding and an application-owned header."""
import asyncio
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread

from v2path import parse
from v2path.service import HTTPDestination, Route, Service


class Receiver(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def do_POST(self):
        body = self.rfile.read(int(self.headers['Content-Length']))
        message = parse(json.loads(body)['message'])
        assert message['PID-3'] == 'TRANSFORMED'
        assert self.headers['Authorization'] == 'Bearer demo-token'
        self.send_response(202)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def log_message(self, *args):
        pass


def transform(message):
    message['PID-3'] = 'TRANSFORMED'
    return message


async def deliver(url, root):
    destination = HTTPDestination(
        url,
        headers={'Content-Type': 'application/json'},
        encoder=lambda message: json.dumps({'message': message.to_text()}).encode('utf-8'),
        header_provider=lambda message, body, attempt: {'Authorization': 'Bearer demo-token'},
    )
    async with Service([Route('adt', {'api': destination}, transform=transform)],
                       failed_directory=root / 'failed') as service:
        message = parse('MSH|^~\\&|SRC|FAC|DST|FAC|20260101||ADT^A01|http-demo|P|2.5.1\rPID|||123\r')
        receipt = await service.submit('adt', message, require_completion=True)
        result = await receipt.completed
        assert result.success
        assert not service.failures.records()


if __name__ == '__main__':
    with ThreadingHTTPServer(('127.0.0.1', 0), Receiver) as receiver:
        thread = Thread(target=receiver.serve_forever)
        thread.start()
        try:
            with TemporaryDirectory() as directory:
                asyncio.run(deliver(f'http://127.0.0.1:{receiver.server_port}/messages', Path(directory)))
        finally:
            receiver.shutdown()
            thread.join()
    print('HTTP route, frozen JSON body, dynamic headers, and 202 acceptance passed.')
