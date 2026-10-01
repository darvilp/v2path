"""MLLP framing with bounded incremental decoding; no socket dependencies."""
from .errors import ParseError

START = b'\x0b'
END = b'\x1c\r'


def frame(payload: bytes) -> bytes:
    if START in payload or END[:1] in payload:
        raise ParseError('An MLLP payload cannot contain unescaped framing markers')
    return START + payload + END


class MLLPDecoder:
    def __init__(self, max_frame_bytes=16 * 1024 * 1024):
        if max_frame_bytes < 1:
            raise ValueError('Frame limit must be positive')
        self.limit = max_frame_bytes
        self._buffer = bytearray()
        self._active = False
        self._ending = False

    @property
    def pending(self):
        return self._active or self._ending

    def feed(self, data):
        return list(self.iter_feed(data))

    def iter_feed(self, data):
        for byte in data:
            if self._ending:
                if byte != 13:
                    raise ParseError('MLLP end marker must be followed by CR')
                payload = bytes(self._buffer)
                self._buffer.clear()
                self._active = self._ending = False
                yield payload
            elif not self._active:
                if byte != 11:
                    raise ParseError('Expected MLLP start marker')
                self._active = True
            elif byte == 11:
                raise ParseError('Nested MLLP frame')
            elif byte == 28:
                self._ending = True
            else:
                self._buffer.append(byte)
                if len(self._buffer) > self.limit:
                    raise ParseError('MLLP frame exceeds configured limit')

    def finish(self):
        if self.pending:
            raise ParseError('Incomplete MLLP frame')
