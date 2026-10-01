"""Pure ACK construction, MSH request interpretation, and MSA correlation."""
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from .core import Message, NULL, parse
from .errors import ParseError

QUALIFIERS = {'AL', 'NE', 'ER', 'SU'}
CODES = {'AA', 'AE', 'AR', 'CA', 'CE', 'CR'}


def value(msg, ref):
    item = msg[ref]
    return '' if item is NULL else str(item)


@dataclass(frozen=True)
class AckRequest:
    original: bool
    accept: str
    application: str

    @classmethod
    def from_message(cls, msg):
        accept, application = value(msg, 'MSH-15'), value(msg, 'MSH-16')
        if not accept and not application:
            return cls(True, 'NE', 'AL')
        if (accept and accept not in QUALIFIERS) or (application and application not in QUALIFIERS):
            raise ParseError('MSH-15/16 must be AL, NE, ER, SU, or empty')
        return cls(False, accept or 'NE', application or 'NE')

    @staticmethod
    def sends(qualifier, success):
        return qualifier == 'AL' or qualifier == ('SU' if success else 'ER')


def make_ack(original: Message, code: str, *, detail='', control_id=None, timestamp=None) -> Message:
    if code not in CODES:
        raise ValueError('Invalid acknowledgement code')
    enc = original.delimiters
    ack = parse('MSH' + enc.field + enc.declaration + '\r', encoding=original.encoding)
    for dest, source in [(3, 5), (4, 6), (5, 3), (6, 4)]:
        ack[f'MSH-{dest}'] = original.get_raw(f'MSH-{source}') or ''
    ack['MSH-7'] = timestamp or datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%z')
    event = original.get_raw('MSH-9.2') or ''
    ack['MSH-9'] = enc.component.join(('ACK', event, 'ACK')) if event else 'ACK'
    ack['MSH-10'] = control_id or uuid4().hex
    ack['MSH-11'] = original.get_raw('MSH-11') or 'P'
    ack['MSH-12'] = original.get_raw('MSH-12') or '2.5.1'
    ack['MSH-15'] = ack['MSH-16'] = 'NE'
    charset = original.get_raw('MSH-18')
    if charset:
        ack['MSH-18'] = charset
    elif original.encoding != 'utf-8':
        from .core import _CHARSET_NAMES
        ack['MSH-18'] = _CHARSET_NAMES[original.encoding]
    ack.append('MSA' + enc.field + code)
    ack.set_text('MSA-2', value(original, 'MSH-10'))
    if detail:
        detail = detail.encode(original.encoding, errors='replace').decode(original.encoding)
        ack.set_text('MSA-3', detail[:256])
        ack.append('ERR')
        ack['ERR-4'] = 'E'
        ack.set_text('ERR-8', detail[:256])
    return ack


def read_ack(payload: bytes, expected_id: str):
    ack = parse(payload)
    if value(ack, 'MSH-9.1') != 'ACK' or ack.segment('MSA') is None:
        raise ParseError('Response is not an ACK with MSA')
    if value(ack, 'MSA-2') != expected_id:
        raise ParseError('ACK control ID does not match the sent message')
    code = value(ack, 'MSA-1')
    if code not in CODES:
        raise ParseError('Invalid MSA-1 acknowledgement code')
    return code
