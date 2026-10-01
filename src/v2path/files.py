"""Atomic file endpoints and failure artifacts used by the service runtime."""
from __future__ import annotations

import json
import os
import shutil
import string
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .io import MessageWriter, Envelope


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def publish(temporary, final):
    """Publish a closed file without replacing an existing name, on one filesystem."""
    os.link(temporary, final)
    try:
        os.unlink(temporary)
    except OSError:
        # Publication already succeeded; a leftover internal temp is not a failed delivery.
        pass


def filename(template, **values):
    allowed = {'route', 'destination', 'timestamp', 'id', 'batch_id', 'sequence', 'source_stem'}
    for _, key, spec, conversion in string.Formatter().parse(template):
        if key is not None and (key not in allowed or spec or conversion):
            raise ValueError('Filename templates accept only the documented plain tokens')
    for value in values.values():
        if any(c in str(value) for c in '/\\\x00'):
            raise ValueError('Filename substitutions cannot contain path separators')
    name = template.format(**values)
    if not name or name in {'.', '..'} or any(c in name for c in '/\\\x00') or name.startswith('.'):
        raise ValueError('Output filename must be a visible, single path component')
    return name


@dataclass(frozen=True)
class FileSource:
    path: str | Path
    encoding: str | None = None
    archive: str | Path | None = None


@dataclass(frozen=True)
class DirectorySource:
    path: str | Path
    pattern: str = '*.hl7'
    readiness: str = 'atomic'
    marker_suffix: str = '.ready'
    poll_interval: float = 1
    stability: float = 5
    encoding: str | None = None
    archive: str | Path | None = None

    def __post_init__(self):
        if self.readiness not in {'atomic', 'marker', 'stable'}:
            raise ValueError('Unknown directory readiness mode')
        if self.poll_interval <= 0 or self.stability <= 0:
            raise ValueError('Directory intervals must be positive')
        if '/' in self.pattern or '\\' in self.pattern:
            raise ValueError('Directory input is nonrecursive')
        if not self.marker_suffix or '/' in self.marker_suffix or '\\' in self.marker_suffix:
            raise ValueError('Invalid completion marker suffix')


@dataclass(frozen=True)
class FileDestination:
    path: str | Path
    framing: str = 'bare'
    terminator: str | None = None
    encoding: str | None = None
    batch_header: bytes | str | None = None
    file_header: bytes | str | None = None


@dataclass(frozen=True)
class DirectoryDestination:
    path: str | Path
    mode: str = 'auto'
    template: str | None = None
    max_messages: int = 1000
    max_bytes: int = 16 * 1024 * 1024
    max_seconds: float = 60
    framing: str = 'bare'
    terminator: str | None = None
    encoding: str | None = None
    batch_header: bytes | str | None = None
    file_header: bytes | str | None = None

    def __post_init__(self):
        if self.mode not in {'auto', 'message', 'batch', 'preserve'}:
            raise ValueError('Invalid directory output mode')
        if min(self.max_messages, self.max_bytes, self.max_seconds) <= 0:
            raise ValueError('Batch rotation limits must be positive')


class FailureStore:
    """One atomic directory per failure; payload bytes are never embedded in logs."""
    def __init__(self, path):
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)

    def write(self, metadata, payload, *, members=None, identity=None, request_body=None):
        identity = identity or uuid4().hex
        final = self.path / identity
        if final.exists():
            # Identity is allocated by the runtime and reused after a failed write.
            return final
        temporary = Path(tempfile.mkdtemp(prefix='.failure-', dir=self.path))
        try:
            data = temporary / 'payload.hl7'
            if isinstance(payload, Path):
                shutil.copyfile(payload, data)
            else:
                data.write_bytes(payload)
            if request_body is not None:
                (temporary / 'request.body').write_bytes(request_body)
            if members:
                shutil.copyfile(members, temporary / 'members.jsonl')
            record = dict(metadata, id=identity, recorded_at=utcnow())
            (temporary / 'record.json').write_text(json.dumps(record, indent=2), encoding='utf-8')
            for path in temporary.iterdir():
                with path.open('r+b') as handle:
                    os.fsync(handle.fileno())
            os.rename(temporary, final)
            return final
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)

    def records(self):
        return sorted(p for p in self.path.iterdir() if p.is_dir() and not p.name.startswith('.'))


class Spool:
    """One destination artifact. Member metadata streams alongside its payload."""
    def __init__(self, endpoint, route, destination, mode, source_stem='', sequence=1):
        self.endpoint, self.mode = endpoint, mode
        self.identity = uuid4().hex
        self.route, self.destination = route, destination
        self.count = self.size = 0
        self.created_at = utcnow()
        root = Path(endpoint.path).parent if isinstance(endpoint, FileDestination) else Path(endpoint.path)
        root.mkdir(parents=True, exist_ok=True)
        if isinstance(endpoint, FileDestination):
            self.final = Path(endpoint.path)
        else:
            template = endpoint.template or ('{source_stem}-{id}.hl7' if mode == 'preserve' else
                '{route}-{timestamp}-{id}.hl7' if mode == 'message' else '{route}-{timestamp}-{batch_id}.hl7')
            self.final = root / filename(template, route=route, destination=destination,
                timestamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'),
                id=self.identity, batch_id=self.identity, sequence=sequence, source_stem=source_stem)
        self.batch = self._header(endpoint.batch_header, 'BHS')
        self.file = self._header(endpoint.file_header, 'FHS')
        if self.file and not self.batch:
            raise ValueError('FHS requires a BHS header')
        if self.batch and endpoint.framing == 'mllp':
            raise ValueError('Batch envelopes require bare file framing')
        fd, temporary = tempfile.mkstemp(prefix='.v2path-', suffix='.tmp', dir=root)
        self.path = Path(temporary)
        self.handle = os.fdopen(fd, 'wb')
        self.members = Path(str(temporary) + '.members')
        try:
            self.manifest = self.members.open('w', encoding='utf-8')
        except OSError:
            self.handle.close()
            self.path.unlink(missing_ok=True)
            raise
        self.writer = MessageWriter(self.handle, framing=endpoint.framing,
                                    terminator=endpoint.terminator, encoding=endpoint.encoding)
        self.writer.__enter__()
        self.closed = False
        self.complete = False
        try:
            for header in (self.file, self.batch):
                if header:
                    self.writer.write_record(header, source_encoding=endpoint.encoding)
        except Exception:
            self.discard()
            raise

    def _header(self, raw, name):
        if raw is None:
            return None
        raw = raw.encode(self.endpoint.encoding or 'utf-8') if isinstance(raw, str) else raw
        body = raw.rstrip(b'\r\n')
        if body[:3] != name.encode() or b'\r' in body or b'\n' in body:
            raise ValueError(f'Expected one {name} segment')
        from .core import encoding_characters
        encoding_characters(body.decode('latin1'))
        return Envelope(body + (self.endpoint.terminator or '\r').encode())

    def add(self, message, metadata):
        self.manifest.write(json.dumps(metadata) + '\n')
        before = self.handle.tell()
        self.writer.write(message)
        self.size += self.handle.tell() - before
        self.count += 1

    def finish(self):
        if self.closed:
            return
        try:
            ending = (self.endpoint.terminator or '\r').encode()
            if self.batch:
                self.writer.write_record(Envelope(b'BTS' + self.batch.raw[3:4] + str(self.count).encode() + ending))
            if self.file:
                self.writer.write_record(Envelope(b'FTS' + self.file.raw[3:4] + b'1' + ending))
            self.writer.__exit__(None, None, None)
            self.handle.flush()
            os.fsync(self.handle.fileno())
            self.manifest.flush()
            os.fsync(self.manifest.fileno())
            self.complete = True
        finally:
            self.closed = True
            self.handle.close()
            self.manifest.close()

    def discard(self):
        self.handle.close()
        self.manifest.close()
        self.path.unlink(missing_ok=True)
        self.members.unlink(missing_ok=True)
