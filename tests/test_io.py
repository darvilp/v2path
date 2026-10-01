from io import BytesIO
import pytest
from v2path import MessageWriter, ParseError, iter_messages, iter_records, parse, transform_stream
from test_core import BASE

MSG = (BASE + 'PID|||123||Smith^Jane\r').encode()
BATCH = b'FHS|^~\\&|SOURCE\rBHS|^~\\&|SOURCE\r' + MSG + MSG + b'BTS|2|comment\rFTS|1\r'


@pytest.mark.parametrize('data', [MSG, MSG + MSG, b'\x0b' + MSG + b'\x1c\r', BATCH, b'\x0b' + BATCH + b'\x1c\r'])
@pytest.mark.parametrize('chunk', [1, 2, 7, 65536])
def test_chunked_roundtrip(data, chunk):
    out = BytesIO()
    chunks = [data[i:i+chunk] for i in range(0, len(data), chunk)]
    transform_stream(chunks, out, lambda m: m)
    assert out.getvalue() == data


def test_filter_split_and_counts():
    out = BytesIO()
    number = 0
    def convert(msg):
        nonlocal number
        number += 1
        return None if number == 1 else [msg, msg.copy(), msg.copy()]
    assert transform_stream(BATCH, out, convert) == 3
    assert out.getvalue().endswith(b'BTS|3|comment\rFTS|1\r')
    assert len(list(iter_messages(out.getvalue()))) == 3
    out = BytesIO()
    transform_stream(BATCH, out, lambda _: None)
    assert b'BTS|0|' in out.getvalue()


def test_options_and_generated_envelopes():
    out = BytesIO()
    transform_stream(MSG, out, lambda m: m, terminator='\r\n', framing='mllp', envelopes='generate', batch_header='BHS|^~\\&|S', file_header='FHS|^~\\&|S')
    result = out.getvalue()
    assert b'\x0bMSH' in result and b'\x1c\r' in result
    assert result.endswith(b'BTS|1\r\nFTS|1\r\n')
    assert list(iter_messages(result))[0]['PID-3'] == '123'
    bare = BytesIO()
    transform_stream(result, bare, lambda m: m, framing='bare', envelopes='omit', terminator='\n')
    assert bare.getvalue() == MSG.replace(b'\r', b'\n')


def test_atomic_path_failure(tmp_path):
    destination = tmp_path / 'output.hl7'
    destination.write_bytes(b'original')
    with pytest.raises(ParseError):
        transform_stream(MSG + b'\x0b' + MSG, destination, lambda m: m)
    assert destination.read_bytes() == b'original'
    assert list(tmp_path.iterdir()) == [destination]
    source = tmp_path / 'input.hl7'
    source.write_bytes(MSG)
    transform_stream(source, source, lambda m: m)
    assert source.read_bytes() == MSG


def test_writer_and_iteration():
    assert len(list(iter_messages(MSG + MSG))) == 2
    out = BytesIO()
    with MessageWriter(out, framing='mllp') as writer:
        writer.write(parse(MSG))
    assert out.getvalue() == b'\x0b' + MSG + b'\x1c\r'


@pytest.mark.parametrize('source', [b'\x0b' + MSG, b'\x1c\r', b'BHS|^~\\&\r'+MSG, MSG+b'\x1cX', b'BTS|1\r'])
def test_incomplete_records(source):
    with pytest.raises(ParseError):
        list(iter_records(source))


def test_text_input_uses_characters_not_utf8_bytes():
    from io import StringIO
    msg = parse(BASE + 'PID|||||André\r', encoding='iso8859-1')
    source = msg.to_bytes(encoding='iso8859-1').decode('iso8859-1')
    # Make the declaration explicit without changing the message's codec.
    msg['MSH-18'] = '8859/1'
    source = msg.to_text()
    for incoming in [source, StringIO(source), iter(source)]:
        out = BytesIO()
        transform_stream(incoming, out, lambda m: m)
        assert list(iter_messages(out.getvalue()))[0]['PID-5'] == 'André'
        assert out.getvalue() == source.encode('iso8859-1')


def test_unterminated_message_only_gains_separator_when_needed():
    source = MSG.rstrip(b'\r')
    out = BytesIO()
    transform_stream(source, out, lambda m: m)
    assert out.getvalue() == source
    out = BytesIO()
    transform_stream(source, out, lambda m: [m, m.copy()])
    assert len(list(iter_messages(out.getvalue()))) == 2
    assert out.getvalue() == source + b'\r' + source
    out = BytesIO()
    transform_stream(source, out, lambda m: m, envelopes='generate', batch_header='BHS|^~\\&|S')
    assert len(list(iter_messages(out.getvalue()))) == 1
    assert b'PID|||123||Smith^Jane\rBTS|1\r' in out.getvalue()


def test_streaming_does_not_read_whole_input():
    consumed = 0
    def source():
        nonlocal consumed
        for _ in range(1000):
            consumed += 1
            yield MSG
    messages = iter_messages(source())
    assert next(messages)['PID-3'] == '123'
    assert consumed <= 2
    assert sum(1 for _ in messages) == 999


def test_short_output_writes():
    class ShortWriter(BytesIO):
        def write(self, value):
            return super().write(value[:3])
    out = ShortWriter()
    transform_stream(MSG, out, lambda m: m)
    assert out.getvalue() == MSG


def test_non_ascii_envelope_conversion_requires_source_encoding():
    data = b'BHS|^~\\&|Andr\xe9\r' + MSG + b'BTS|1\r'
    out = BytesIO()
    with pytest.raises(ParseError):
        transform_stream(data, out, lambda m: m, output_encoding='utf-8')
    out = BytesIO()
    transform_stream(data, out, lambda m: m, encoding='iso8859-1', output_encoding='utf-8')
    assert b'Andr\xc3\xa9' in out.getvalue()
    assert list(iter_messages(out.getvalue()))[0]['MSH-18'] == 'UNICODE UTF-8'


def test_envelope_delimiter_conversion_preserves_data_and_counts():
    from v2path import EncodingCharacters, Envelope
    target = EncodingCharacters('*', '$', '%', '!', '?')
    data = b'FHS|^~\\&|FILE\rBHS|^~\\&|A\\F\\B\r' + MSG + b'BTS|1|comment\rFTS|1\r'
    out = BytesIO()
    transform_stream(data, out, lambda m: [m, m.copy()], delimiters=target)
    value = out.getvalue()
    assert value.startswith(b'FHS*$%!?*FILE\rBHS*$%!?*A|B\r')
    assert value.endswith(b'BTS*2*comment\rFTS*1\r')
    assert len(list(iter_messages(value))) == 2
    out = BytesIO()
    records = list(iter_records(b'BHS|^~\\&|SRC\rBTS|0\r'))
    with MessageWriter(out, delimiters=target) as writer:
        for record in records:
            writer.write_record(record)
    assert out.getvalue() == b'BHS*$%!?*SRC\rBTS*0\r'


def test_envelope_encoding_conversion_updates_hex_escapes():
    data = b'BHS|^~\\&|Andr\\XE9\\\r' + MSG + b'BTS|1\r'
    out = BytesIO()
    transform_stream(data, out, lambda m: m, encoding='iso8859-1', output_encoding='utf-8')
    assert b'Andr\\XC3A9\\' in out.getvalue()
