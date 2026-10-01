"""Incremental ER7/MLLP records and transactional path-to-path conversion."""
from __future__ import annotations

import os
import re
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .core import Message, encoding_characters, normalize_encoding, parse, transcode_hex
from .errors import ParseError
from .framing import START, END


@dataclass(frozen=True)
class FrameStart:
    pass


@dataclass(frozen=True)
class FrameEnd:
    pass


@dataclass(frozen=True)
class Envelope:
    raw: bytes

    @property
    def name(self):
        return self.raw[:3].decode('ascii')


def _chunks(source, encoding=None, chunk_size=65536):
    if isinstance(source, bytes):
        yield source
    elif isinstance(source, str) and (source.startswith(('MSH', 'BHS', 'FHS', '\x0b'))):
        yield source
    elif isinstance(source, (str, os.PathLike)):
        with open(source, 'rb') as handle:
            while chunk := handle.read(chunk_size):
                yield chunk
    elif hasattr(source, 'read'):
        while chunk := source.read(chunk_size):
            yield chunk
    else:
        for chunk in source:
            if not isinstance(chunk, (bytes, str)):
                raise TypeError('Input iterables must yield byte or text chunks')
            yield chunk


def _tokens(source, encoding=None):
    # Buffer at most one physical segment plus a read chunk.
    pending = bytearray()
    text_mode = None
    def output(value):
        raw = bytes(value)
        return raw.decode('utf-8') if text_mode else raw
    for chunk in _chunks(source, encoding):
        if not chunk:
            continue
        is_text = isinstance(chunk, str)
        if text_mode is not None and is_text != text_mode:
            raise TypeError('Input chunks must consistently be text or bytes')
        text_mode = is_text
        pending.extend(chunk.encode('utf-8') if is_text else chunk)
        while True:
            positions = [p for marker in (b'\r', b'\n', b'\x0b', b'\x1c') if (p := pending.find(marker)) >= 0]
            if not positions:
                break
            pos = min(positions)
            marker = pending[pos]
            if marker in (13, 28) and pos + 1 == len(pending):
                break
            if marker in (11, 28):
                if pos:
                    yield output(pending[:pos])
                    del pending[:pos]
                if marker == 11:
                    yield FrameStart()
                    del pending[:1]
                else:
                    if pending[1] != 13:
                        raise ParseError('MLLP end marker must be followed by CR')
                    yield FrameEnd()
                    del pending[:2]
            else:
                end = pos + 1 + (marker == 13 and pending[pos + 1:pos + 2] == b'\n')
                yield output(pending[:end])
                del pending[:end]
    if pending:
        if pending[-1:] == b'\x1c':
            raise ParseError('Incomplete MLLP end marker')
        yield output(pending)


def iter_records(source, *, encoding=None):
    """Yield Message, Envelope, FrameStart, and FrameEnd records incrementally."""
    rows = []
    framed = False
    file_open = batch_open = False
    metadata = []
    def message():
        joined = ''.join(rows) if isinstance(rows[0], str) else b''.join(rows)
        result = parse(joined, encoding=encoding)
        result._framing = framed
        result._envelopes = tuple(metadata)
        rows.clear()
        return result
    for token in _tokens(source, encoding):
        if isinstance(token, FrameStart):
            if framed:
                raise ParseError('Nested MLLP frames are not supported')
            if rows:
                yield message()
            framed = True
            yield token
        elif isinstance(token, FrameEnd):
            if not framed:
                raise ParseError('MLLP end marker without a start')
            if rows:
                yield message()
            yield token
            framed = False
        else:
            name = token[:3].encode('ascii') if isinstance(token, str) else token[:3]
            if name == b'MSH' and rows:
                yield message()
            if name in (b'FHS', b'BHS', b'BTS', b'FTS'):
                if isinstance(token, str):
                    token = token.encode(encoding or 'ascii')
                if rows:
                    yield message()
                if name in (b'FHS', b'BHS'):
                    encoding_characters(token.rstrip(b'\r\n').decode('latin1'))
                if name == b'FHS':
                    if file_open or batch_open:
                        raise ParseError('Unexpected nested file envelope')
                    file_open = True
                    metadata.append(token)
                elif name == b'BHS':
                    if batch_open:
                        raise ParseError('Unexpected nested batch envelope')
                    batch_open = True
                    metadata.append(token)
                elif name == b'BTS':
                    if not batch_open:
                        raise ParseError('BTS without BHS')
                    batch_open = False
                    metadata.pop()
                else:
                    if not file_open or batch_open:
                        raise ParseError('FTS without a complete file envelope')
                    file_open = False
                    metadata.clear()
                yield Envelope(token)
            elif name == b'MSH' or rows:
                rows.append(token)
            else:
                raise ParseError('A segment outside a message or envelope cannot be interpreted')
    if rows:
        yield message()
    if framed:
        raise ParseError('Incomplete MLLP frame')
    if batch_open or file_open:
        raise ParseError('Incomplete batch/file envelope')


def iter_messages(source, *, encoding=None):
    """Message-only iteration; use iter_records/transform_stream for whole files."""
    for record in iter_records(source, encoding=encoding):
        if isinstance(record, Message):
            yield record


@contextmanager
def _destination(destination):
    if isinstance(destination, (str, os.PathLike)):
        path = Path(destination)
        fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', suffix='.tmp', dir=path.parent)
        try:
            with os.fdopen(fd, 'wb') as handle:
                yield handle
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    else:
        yield destination


class MessageWriter:
    """Context-managed binary writer. Paths are replaced only on success."""
    def __init__(self, destination, *, terminator=None, framing='preserve', encoding=None, delimiters=None):
        if framing not in ('preserve', 'bare', 'mllp'):
            raise ValueError('framing must be preserve, bare, or mllp')
        if terminator not in (None, '\r', '\n', '\r\n'):
            raise ValueError('terminator must be None, CR, LF, or CRLF')
        self.destination, self.terminator, self.framing = destination, terminator, framing
        self.encoding, self.delimiters = encoding, delimiters
        self._handle = None
        self._frame = False
        self._needs_separator = False
        self._separator = b'\r'
        self._file_delimiters = self._batch_delimiters = None

    def __enter__(self):
        self._context = _destination(self.destination)
        self._handle = self._context.__enter__()
        return self

    def __exit__(self, *exc):
        try:
            return self._context.__exit__(*exc)
        finally:
            self._handle = None

    def _write(self, data):
        if self._handle is None:
            raise RuntimeError('Use MessageWriter as a context manager')
        remaining = memoryview(data)
        while remaining:
            count = self._handle.write(remaining)
            if count is None or count <= 0:
                raise OSError('Output stream made no progress')
            remaining = remaining[count:]

    def _payload(self, data):
        if self._needs_separator and data:
            self._write(self._separator)
        self._write(data)
        endings = list(re.finditer(rb'\r\n|\r|\n', data))
        if endings:
            self._separator = endings[-1].group()
        if data:
            self._needs_separator = not data.endswith((b'\r', b'\n'))

    def write(self, msg: Message):
        if not isinstance(msg, Message):
            raise TypeError('write expects a Message')
        payload = msg.to_bytes(encoding=self.encoding, terminator=self.terminator, delimiters=self.delimiters)
        framed = self.framing == 'mllp' or self.framing == 'preserve' and msg._framing and not self._frame
        if framed:
            self._write(START)
            self._needs_separator = False
        self._payload(payload)
        if framed:
            self._write(END)
            self._needs_separator = False

    def write_record(self, record, *, source_encoding=None):
        if isinstance(record, Message):
            self.write(record)
        elif isinstance(record, FrameStart):
            if self.framing == 'preserve':
                self._write(START)
                self._frame = True
                self._needs_separator = False
        elif isinstance(record, FrameEnd):
            if self.framing == 'preserve':
                self._write(END)
                self._frame = False
                self._needs_separator = False
        elif isinstance(record, Envelope):
            raw = record.raw
            name = record.name
            if name in ('FHS', 'BHS'):
                original = encoding_characters(raw.rstrip(b'\r\n').decode('latin1'))
                if name == 'FHS':
                    self._file_delimiters = original
                else:
                    self._batch_delimiters = original
            else:
                original = self._file_delimiters if name == 'FTS' else self._batch_delimiters
            if self.encoding:
                # Envelope headers have no reliable per-record charset declaration.
                source = source_encoding or ('ascii' if raw.isascii() else None)
                if source is None:
                    raise ParseError('Explicit source encoding required to transcode non-ASCII envelopes')
                target = normalize_encoding(self.encoding)
                text = raw.decode(source)
                if original is not None:
                    text = transcode_hex(text, original.escape, source, target)
                raw = text.encode(target)
            if self.delimiters:
                if original is None:
                    raise ValueError('Envelope trailers require their corresponding header for delimiter conversion')
                # Latin-1 supplies a reversible byte mapping; the source envelope
                # need not declare a charset for separator-only conversion.
                text = raw.decode('latin1')
                if name in ('FHS', 'BHS'):
                    converted = parse('MSH' + text[3:], encoding='iso8859-1').to_text(delimiters=self.delimiters)
                    raw = (name + converted[3:]).encode('latin1')
                else:
                    header = 'MSH' + original.field + original.declaration + '\r'
                    converted = parse(header + text, encoding='iso8859-1').to_text(delimiters=self.delimiters)
                    tail = parse(converted, encoding='iso8859-1').segment(name)
                    raw = (tail.get_raw() + tail.terminator).encode('latin1')
            if self.terminator is not None:
                raw = raw.rstrip(b'\r\n') + self.terminator.encode('ascii')
            self._payload(raw)
        else:
            raise TypeError('Unknown record type')


def _count_trailer(record, count, separator):
    raw = record.raw
    end = len(raw.rstrip(b'\r\n'))
    pieces = raw[:end].split(separator)
    if len(pieces) < 2:
        pieces.append(b'')
    pieces[1] = str(count).encode('ascii')
    return Envelope(separator.join(pieces) + raw[end:])


def transform_stream(source, destination, transform, *, encoding=None, output_encoding=None,
                     terminator=None, framing='preserve', envelopes='preserve', delimiters=None,
                     batch_header=None, file_header=None):
    """Transform messages while preserving full record order and envelope metadata.

    Callbacks return a Message, an iterable of Messages, or None. Exceptions leave
    path destinations untouched; caller-owned streams may contain partial output.
    """
    if envelopes not in ('preserve', 'omit', 'generate'):
        raise ValueError('envelopes must be preserve, omit, or generate')
    if envelopes == 'generate' and batch_header is None:
        raise ValueError('Generating envelopes requires a caller-supplied BHS header')
    if envelopes != 'generate' and (batch_header is not None or file_header is not None):
        raise ValueError('Header metadata requires envelopes=generate')
    def header(value, name):
        raw = value.encode(encoding or 'utf-8') if isinstance(value, str) else value
        if not isinstance(raw, bytes) or raw[:3] != name:
            raise ValueError(f'Expected {name.decode()} header')
        body = raw.rstrip(b'\r\n')
        if b'\r' in body or b'\n' in body:
            raise ValueError('Envelope header must contain exactly one segment')
        encoding_characters(body.decode('latin1'))
        return Envelope(body + (terminator or '\r').encode())
    batch = header(batch_header, b'BHS') if envelopes == 'generate' else None
    file = header(file_header, b'FHS') if file_header is not None else None
    with MessageWriter(destination, terminator=terminator, framing=framing, encoding=output_encoding, delimiters=delimiters) as writer:
        if file:
            writer.write_record(file, source_encoding=encoding)
        if batch:
            writer.write_record(batch, source_encoding=encoding)
        in_batch = False
        batch_original = batch_written = written = 0
        separator = b'|'
        for record in iter_records(source, encoding=encoding):
            if isinstance(record, Message):
                if in_batch:
                    batch_original += 1
                result = transform(record)
                results = () if result is None else (result,) if isinstance(result, Message) else result
                for msg in results:
                    if not isinstance(msg, Message):
                        raise TypeError('Transforms must yield Message instances')
                    # Preserve framing provenance for newly generated replacements.
                    if framing == 'preserve' and record._framing and not writer._frame:
                        msg = msg.copy()
                        msg._framing = True
                    writer.write(msg)
                    written += 1
                    if in_batch:
                        batch_written += 1
            elif isinstance(record, Envelope):
                if record.name == 'BHS':
                    in_batch = True
                    batch_original = batch_written = 0
                    separator = record.raw[3:4]
                elif record.name == 'BTS':
                    if batch_original != batch_written:
                        record = _count_trailer(record, batch_written, separator)
                    in_batch = False
                if envelopes == 'preserve':
                    writer.write_record(record, source_encoding=encoding)
            elif framing == 'preserve':
                writer.write_record(record)
        if batch:
            sep = batch.raw[3:4]
            writer.write_record(Envelope(b'BTS' + sep + str(written).encode() + (terminator or '\r').encode()))
        if file:
            writer.write_record(Envelope(b'FTS' + file.raw[3:4] + b'1' + (terminator or '\r').encode()))
    return written
