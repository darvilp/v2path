"""ER7 values and physical messages, preserving source spelling until edited."""
from __future__ import annotations

import codecs
import re
from dataclasses import dataclass
from enum import Enum

from .errors import AssignmentError, Diagnostic, ParseError


class ValueState(Enum):
    ABSENT = "absent"
    EMPTY = "empty"
    NULL = "null"
    VALUE = "value"


class _Null:
    def __repr__(self):
        return "NULL"

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        return self


NULL = _Null()


@dataclass(frozen=True)
class EncodingCharacters:
    field: str = "|"
    component: str = "^"
    repetition: str = "~"
    escape: str = "\\"
    subcomponent: str = "&"
    truncation: str = ""

    @property
    def declaration(self):
        return self.component + self.repetition + self.escape + self.subcomponent + self.truncation


@dataclass(frozen=True)
class Reference:
    segment: str | None
    occurrence: int
    field: int
    repetition: int | None
    component: int | None
    subcomponent: int | None


_REFERENCE = re.compile(r"(?:(?P<seg>[A-Z0-9]{3})(?:\[(?P<occ>\d+)\])?-)?(?P<field>[1-9]\d*)(?:\[(?P<rep>[1-9]\d*)\])?(?:\.(?P<comp>[1-9]\d*))?(?:\.(?P<sub>[1-9]\d*))?\Z")


def reference(value: str) -> Reference:
    m = _REFERENCE.fullmatch(value)
    if not m:
        raise ValueError(f"Invalid field reference: {value!r}")
    return Reference(m['seg'], int(m['occ'] or 0), int(m['field']),
                     int(m['rep']) if m['rep'] else None,
                     int(m['comp']) if m['comp'] else None,
                     int(m['sub']) if m['sub'] else None)


def split_er7(value: str, delimiter: str, escape: str) -> list[str]:
    """Split outside complete escape sequences; retain every character."""
    parts, start, pos = [], 0, 0
    while pos < len(value):
        if value[pos] == escape:
            end = value.find(escape, pos + 1)
            if end != -1:
                pos = end + 1
                continue
        if value[pos] == delimiter:
            parts.append(value[start:pos])
            start = pos + 1
        pos += 1
    return parts + [value[start:]]


_CHARSETS = {
    '': 'utf-8', 'ASCII': 'ascii', 'US-ASCII': 'ascii',
    'UNICODE UTF-8': 'utf-8', 'UTF-8': 'utf-8', 'UTF8': 'utf-8',
    '8859/1': 'iso8859-1', 'ISO-8859-1': 'iso8859-1', 'LATIN1': 'iso8859-1',
    'WINDOWS-1252': 'cp1252', 'CP1252': 'cp1252',
}
_CHARSET_NAMES = {'utf-8': 'UNICODE UTF-8', 'ascii': 'ASCII', 'iso8859-1': '8859/1', 'cp1252': 'WINDOWS-1252'}


def normalize_encoding(value: str) -> str:
    try:
        name = codecs.lookup(value).name
    except LookupError as exc:
        raise ParseError(f"Unknown encoding {value!r}") from exc
    if name not in _CHARSET_NAMES:
        raise ParseError(f"Unsupported encoding {value!r}; use an ASCII-compatible supported encoding")
    return name


def encoding_characters(header: str) -> EncodingCharacters:
    if len(header) < 8 or header[:3] not in ('MSH', 'BHS', 'FHS'):
        raise ParseError('A usable MSH, BHS, or FHS delimiter declaration is required')
    field = header[3]
    end = header.find(field, 4)
    declaration = header[4:] if end < 0 else header[4:end]
    chars = field + declaration
    if len(declaration) not in (4, 5) or len(set(chars)) != len(chars):
        raise ParseError('Encoding delimiters must be distinct: four encoding characters and optional truncation')
    if any(ord(c) < 33 or ord(c) > 126 or c.isalnum() for c in chars):
        raise ParseError('Encoding delimiters must be printable ASCII punctuation')
    return EncodingCharacters(field, *declaration[:4], declaration[4:])


def _decode(value: str, enc: EncodingCharacters, charset: str) -> str:
    codes = dict(zip('FSRET', (enc.field, enc.component, enc.repetition, enc.escape, enc.subcomponent)))
    def replace(m):
        token = m.group(1)
        if token in codes:
            return codes[token]
        if token.startswith('X') and re.fullmatch(r'(?:[0-9A-Fa-f]{2})+', token[1:]):
            return bytes.fromhex(token[1:]).decode(charset, errors='surrogateescape')
        return m.group(0)
    return re.sub(re.escape(enc.escape) + '(.*?)' + re.escape(enc.escape), replace, value)


def _escape(value: str, enc: EncodingCharacters) -> str:
    codes = dict(zip((enc.field, enc.component, enc.repetition, enc.escape, enc.subcomponent), 'FSRET'))
    return ''.join(enc.escape + codes[c] + enc.escape if c in codes else
                   enc.escape + f'X{ord(c):02X}' + enc.escape if ord(c) < 32 or ord(c) == 127 else c
                   for c in value)


def transcode_hex(value, escape, source, target):
    pattern = re.escape(escape) + r'X((?:[0-9A-Fa-f]{2})+)' + re.escape(escape)
    def replace(match):
        text = bytes.fromhex(match[1]).decode(source, errors='strict')
        return escape + 'X' + text.encode(target).hex().upper() + escape
    return re.sub(pattern, replace, value)


class Segment:
    """A physical segment. Use message mutators to change topology."""
    def __init__(self, raw: str, message: Message, terminator: str = ''):
        if len(raw) < 3 or not re.fullmatch('[A-Z0-9]{3}', raw[:3]):
            raise ParseError(f'Invalid segment identifier: {raw[:3]!r}')
        if len(raw) > 3 and raw[3] != message.delimiters.field:
            raise ParseError('Segment fields must use the message field delimiter')
        self._raw, self._message, self._terminator = raw, message, terminator

    @property
    def name(self):
        return self._raw[:3]

    @property
    def message(self):
        return self._message

    @property
    def terminator(self):
        return self._terminator

    def __repr__(self):
        return f'Segment({self.name!r})'

    def _fields(self):
        enc = self.message.delimiters
        if self.name == 'MSH':
            # MSH-2 declares the escape character, so never tokenize it as data.
            rest = self._raw[4:].split(enc.field, 1)
            return [self._raw[:3], enc.field, rest[0]] + (split_er7(rest[1], enc.field, enc.escape) if len(rest) > 1 else [])
        return split_er7(self._raw, enc.field, enc.escape)

    def _locate(self, ref: str, *, whole_field=False):
        r = reference(ref)
        if r.segment and (r.segment != self.name or r.occurrence):
            raise ValueError('A segment-relative reference cannot select another segment')
        enc = self.message.delimiters
        fields = self._fields()
        if r.field >= len(fields):
            return None
        value = fields[r.field]
        if self.name == 'MSH' and r.field in (1, 2):
            if r.repetition or r.component or r.subcomponent:
                raise ValueError('MSH delimiter declarations are indivisible')
            return value
        levels = []
        if not whole_field or r.repetition or r.component:
            levels.append((enc.repetition, (r.repetition or 1) - 1))
        if r.component:
            levels.append((enc.component, r.component - 1))
        if r.subcomponent:
            levels.append((enc.subcomponent, r.subcomponent - 1))
        for sep, index in levels:
            parts = split_er7(value, sep, enc.escape)
            if index >= len(parts):
                return None
            value = parts[index]
        return value

    def get_raw(self, ref: str | None = None):
        return self._raw if ref is None else self._locate(ref, whole_field=True)

    def __getitem__(self, ref: str):
        value = self._locate(ref)
        if value is None:
            return ''
        if value == '""':
            return NULL
        r = reference(ref)
        if self.name == 'MSH' and r.field in (1, 2):
            return value
        return _decode(value, self.message.delimiters, self.message.encoding)

    def value_state(self, ref: str):
        value = self._locate(ref)
        return ValueState.ABSENT if value is None else ValueState.EMPTY if value == '' else ValueState.NULL if value == '""' else ValueState.VALUE

    def set_text(self, ref: str, value: str):
        if not isinstance(value, str):
            raise TypeError('Literal text must be a string')
        encoded = _escape(value, self.message.delimiters)
        # Literal double quotes must not become the explicit null token.
        if encoded == '""':
            e = self.message.delimiters.escape
            encoded = e + 'X2222' + e
        self[ref] = encoded

    def __setitem__(self, ref: str, value):
        r = reference(ref)
        if r.segment and (r.segment != self.name or r.occurrence):
            raise AssignmentError('Cannot select another segment through a segment reference')
        if self.name == 'MSH' and r.field in (1, 2):
            raise AssignmentError('Use explicit output conversion to change delimiters')
        if value is NULL:
            value = '""'
        if not isinstance(value, str):
            raise TypeError('Values must be ER7 strings or NULL')
        enc = self.message.delimiters
        forbidden = enc.field
        if r.repetition or r.component:
            forbidden += enc.repetition
        if r.component:
            forbidden += enc.component
        if r.subcomponent:
            forbidden += enc.subcomponent
        self._validate_assignment(value, forbidden)
        if self.name == 'MSH' and r.field == 18:
            if r.repetition or r.component or r.subcomponent:
                raise AssignmentError('MSH-18 is a scalar charset declaration in this release')
            declared = _CHARSETS.get(value.upper())
            if declared != self.message.encoding:
                raise AssignmentError('Use to_bytes(encoding=...) to change charset metadata')
        fields = self._fields()
        while len(fields) <= r.field:
            fields.append('')
        levels = []
        if r.repetition or r.component:
            levels.append((enc.repetition, (r.repetition or 1) - 1))
        if r.component:
            levels.append((enc.component, r.component - 1))
        if r.subcomponent:
            levels.append((enc.subcomponent, r.subcomponent - 1))
        def update(old, remaining):
            if not remaining:
                return value
            (sep, index), *tail = remaining
            pieces = split_er7(old, sep, enc.escape)
            while len(pieces) <= index:
                pieces.append('')
            pieces[index] = update(pieces[index], tail)
            return sep.join(pieces)
        fields[r.field] = update(fields[r.field], levels)
        if self.name == 'MSH':
            raw = 'MSH' + enc.field + enc.field.join(fields[2:])
        else:
            raw = enc.field.join(fields)
        self._raw = raw

    def _validate_assignment(self, value, forbidden):
        if any(ord(c) < 32 or ord(c) == 127 or 0xDC80 <= ord(c) <= 0xDCFF for c in value):
            raise AssignmentError('Use set_text for control characters; new undecodable bytes are not accepted')
        enc = self.message.delimiters
        if value.count(enc.escape) % 2:
            raise AssignmentError('New assignments must contain complete escape sequences')
        if any(len(split_er7(value, sep, enc.escape)) > 1 for sep in forbidden):
            raise AssignmentError('Assignment contains a higher-level separator; use set_text for literal text')
        try:
            value.encode(self.message.encoding)
        except UnicodeEncodeError as exc:
            raise AssignmentError('Value cannot be encoded in the message charset') from exc

    def set_raw(self, ref: str | None, value: str):
        if ref is not None:
            self[ref] = value
            return
        replacement = Segment(value, self.message, self.terminator)
        body = self.message.delimiters.field.join(replacement._fields()[3:]) if replacement.name == 'MSH' else value
        self._validate_assignment(body, '')
        if self.name == 'MSH' or replacement.name == 'MSH':
            if self.name != replacement.name or encoding_characters(value) != self.message.delimiters:
                raise AssignmentError('Cannot replace the message delimiter declaration')
            fields = replacement._fields()
            declared = fields[18] if len(fields) > 18 else ''
            old_fields = self._fields()
            old_declared = old_fields[18] if len(old_fields) > 18 else ''
            if declared != old_declared and _CHARSETS.get(declared.upper()) != self.message.encoding:
                raise AssignmentError('Use to_bytes(encoding=...) to change charset metadata')
        changed = replacement.name != self.name
        self._raw = value
        if changed:
            self.message._revision += 1

    def remove(self):
        self.message.remove(self)

    def next(self, name=None):
        segments = self.message.segments()
        return next((s for s in segments[segments.index(self) + 1:] if name is None or s.name == name), None)

    def previous(self, name=None):
        segments = self.message.segments()
        return next((s for s in reversed(segments[:segments.index(self)]) if name is None or s.name == name), None)

    def until(self, name):
        result = []
        item = self.next()
        while item is not None and item.name != name:
            result.append(item)
            item = item.next()
        return result


class Message:
    def __init__(self, delimiters=None, encoding='utf-8'):
        self._delimiters = delimiters or EncodingCharacters()
        self._encoding = normalize_encoding(encoding)
        self._segments: list[Segment] = []
        self._revision = 0
        self.diagnostics: list[Diagnostic] = []
        self._framing = False
        self._had_bytes = False
        self._envelopes = ()

    @property
    def delimiters(self):
        return self._delimiters

    @property
    def encoding(self):
        return self._encoding

    @classmethod
    def new(cls):
        return parse('MSH|^~\\&\r')

    def segments(self, name=None):
        return [s for s in self._segments if name is None or s.name == name]

    def segment(self, name, occurrence=0):
        if occurrence < 0:
            raise ValueError('Segment occurrences are zero-based and nonnegative')
        matches = self.segments(name)
        return matches[occurrence] if occurrence < len(matches) else None

    def _target(self, path):
        r = reference(path)
        if r.segment is None:
            raise ValueError('Message references must include a segment identifier')
        seg = self.segment(r.segment, r.occurrence)
        if seg is None:
            raise LookupError(f'Missing segment: {r.segment}[{r.occurrence}]')
        return seg, path.split('-', 1)[1]

    def __getitem__(self, path):
        seg, ref = self._target(path)
        return seg[ref]

    def __setitem__(self, path, value):
        seg, ref = self._target(path)
        seg[ref] = value

    def get_raw(self, path):
        seg, ref = self._target(path)
        return seg.get_raw(ref)

    def set_raw(self, path, value):
        seg, ref = self._target(path)
        seg.set_raw(ref, value)

    def set_text(self, path, value):
        seg, ref = self._target(path)
        seg.set_text(ref, value)

    def value_state(self, path):
        seg, ref = self._target(path)
        return seg.value_state(ref)

    def insert(self, index, raw):
        if not 0 <= index <= len(self._segments):
            raise IndexError(index)
        if self._segments and index == 0:
            raise AssignmentError('MSH must remain the first segment')
        if isinstance(raw, Segment):
            raise AssignmentError('Use copy or raw text when inserting another segment')
        candidate = Segment(raw, self)
        candidate._validate_assignment(raw, '')
        if self._segments and candidate.name == 'MSH':
            raise AssignmentError('A message has one MSH segment')
        terminator = next((s.terminator for s in reversed(self._segments[:index]) if s.terminator), '')
        terminator = terminator or next((s.terminator for s in self._segments[index:] if s.terminator), '') or '\r'
        candidate._terminator = terminator
        if index and not self._segments[index - 1].terminator:
            self._segments[index - 1]._terminator = terminator
        self._segments.insert(index, candidate)
        self._revision += 1
        return candidate

    def append(self, raw):
        return self.insert(len(self._segments), raw)

    def remove(self, segment):
        if segment.message is not self or segment not in self._segments:
            raise ValueError('Segment is not in this message')
        if segment.name == 'MSH':
            raise AssignmentError('Cannot remove MSH')
        self._segments.remove(segment)
        self._revision += 1

    def move(self, segment, index):
        self._move_range([segment], index)

    def _move_range(self, segments, index):
        if not 1 <= index <= len(self._segments):
            raise IndexError('Move target must follow MSH')
        if not segments or any(s.message is not self or s not in self._segments or s.name == 'MSH' for s in segments):
            raise AssignmentError('Only non-MSH segments in this message can be moved')
        positions = [self._segments.index(s) for s in segments]
        if positions != list(range(positions[0], positions[0] + len(positions))):
            raise AssignmentError('Moved group must be contiguous')
        if positions[0] <= index <= positions[-1] + 1:
            return
        dest = index - sum(p < index for p in positions)
        remaining = [s for s in self._segments if s not in segments]
        remaining[dest:dest] = segments
        for i, seg in enumerate(remaining[:-1]):
            if not seg.terminator:
                seg._terminator = next((s.terminator for s in reversed(remaining[:i]) if s.terminator), '\r')
        self._segments = remaining
        self._revision += 1

    def copy(self):
        import copy
        return copy.deepcopy(self)

    def resolve(self, grammar=None):
        from .structure import ResolvedMessage
        return ResolvedMessage(self, grammar)

    def to_text(self, *, terminator=None, delimiters=None):
        if terminator not in (None, '\r', '\n', '\r\n'):
            raise ValueError('Terminator must be CR, LF, CRLF, or None')
        if delimiters is not None and delimiters != self.delimiters:
            return self._convert_delimiters(delimiters).to_text(terminator=terminator)
        return ''.join(s.get_raw() + (s.terminator if terminator is None else terminator) for s in self._segments)

    def _convert_delimiters(self, target):
        encoding_characters('MSH' + target.field + target.declaration + target.field)
        old = self.delimiters
        original = (old.field, old.repetition, old.component, old.subcomponent)
        replacement = (target.field, target.repetition, target.component, target.subcomponent)
        def convert(value, level=0):
            if level < len(original):
                return replacement[level].join(convert(part, level + 1) for part in split_er7(value, original[level], old.escape))
            if value == '""':
                return value
            if value.count(old.escape) % 2:
                raise AssignmentError('Cannot convert malformed escape sequences')
            tokens = re.findall(re.escape(old.escape) + '(.*?)' + re.escape(old.escape), value)
            for token in tokens:
                if not (len(token) == 1 and token in 'FSRET' or token.startswith('X') and re.fullmatch(r'(?:[0-9A-Fa-f]{2})+', token[1:])):
                    raise AssignmentError('Cannot convert delimiters with unsupported escape sequences')
            result = _escape(_decode(value, old, self.encoding), target)
            return target.escape + 'X2222' + target.escape if result == '""' else result
        rows = []
        for s in self._segments:
            if s.name == 'MSH':
                fields = s._fields()
                row = 'MSH' + target.field + target.declaration
                if len(fields) > 3:
                    row += target.field + target.field.join(convert(f, 1) for f in fields[3:])
            else:
                row = convert(s.get_raw())
            rows.append(row + s.terminator)
        return parse(''.join(rows), encoding=self.encoding)

    def to_bytes(self, *, encoding=None, terminator=None, delimiters=None):
        target = normalize_encoding(encoding) if encoding else self.encoding
        msg = self
        if target != self.encoding:
            msg = self.copy()
            for segment in msg._segments:
                segment._raw = transcode_hex(segment._raw, self.delimiters.escape, self.encoding, target)
            msg._encoding = target
            msg.segment('MSH')['18'] = _CHARSET_NAMES[target]
            # A charset conversion cannot safely reinterpret undecodable source bytes.
            errors = 'strict'
        else:
            errors = 'surrogateescape'
        return msg.to_text(terminator=terminator, delimiters=delimiters).encode(target, errors=errors)


def parse(data: str | bytes, *, encoding: str | None = None) -> Message:
    if not isinstance(data, (str, bytes)):
        raise TypeError('parse accepts str or bytes; use iter_messages for files')
    was_bytes = isinstance(data, bytes)
    probe = data.decode('latin1') if was_bytes else data
    first = re.split(r'\r\n|\r|\n', probe, maxsplit=1)[0]
    if not first.startswith('MSH'):
        raise ParseError('A bare message must begin with MSH')
    enc = encoding_characters(first)
    declared_parts = split_er7(first[4:].split(enc.field, 1)[1], enc.field, enc.escape) if enc.field in first[4:] else []
    declared = declared_parts[15] if len(declared_parts) > 15 else ''  # MSH-3 onward
    if encoding:
        charset = normalize_encoding(encoding)
    elif declared.upper() in _CHARSETS:
        charset = _CHARSETS[declared.upper()]
    else:
        raise ParseError(f'Unsupported MSH-18 charset {declared!r}; supply encoding explicitly')
    text = data.decode(charset, errors='surrogateescape') if was_bytes else data
    msg = Message(enc, charset)
    msg._had_bytes = was_bytes
    rows = re.split(r'(\r\n|\r|\n)', text)
    for i in range(0, len(rows), 2):
        raw = rows[i]
        term = rows[i + 1] if i + 1 < len(rows) else ''
        if not raw:
            if not term and i == len(rows) - 1:
                continue
            # Preserve blank physical records, but do not turn them into segments.
            if msg._segments:
                msg._segments[-1]._terminator += term
                continue
            raise ParseError('Empty message')
        if '\x0b' in raw or '\x1c' in raw:
            raise ParseError('Use the I/O reader for MLLP framing')
        segment = Segment(raw, msg, term)
        if segment.name == 'MSH' and msg._segments:
            raise ParseError('Use iter_messages for multiple messages')
        msg._segments.append(segment)
        body = enc.field.join(segment._fields()[3:]) if segment.name == 'MSH' else raw[3:]
        if body.count(enc.escape) % 2:
            msg.diagnostics.append(Diagnostic('malformed_escape', 'Unclosed escape sequence preserved', len(msg._segments) - 1))
    if not msg._segments:
        raise ParseError('Empty message')
    return msg
