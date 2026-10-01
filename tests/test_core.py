import pytest
from hypothesis import given, strategies as st
from v2path import AssignmentError, EncodingCharacters, NULL, ParseError, ValueState, parse

BASE = 'MSH|^~\\&|SRC|FAC|DST|FAC|20260101||ADT^A01|1|P|2.5.1\r'


def test_targeted_edit_and_states():
    source = BASE + 'PID|||123||Smith^Jane~Jones^Jo||""||||\nZXX|\\Zunknown\\|\r\n'
    msg = parse(source)
    assert msg.to_text() == source
    msg['PID-5.2'] = 'John'
    assert msg.to_text() == source.replace('Smith^Jane', 'Smith^John')
    assert msg['PID-5[2].1'] == 'Jones'
    assert msg['PID-7'] is NULL
    assert msg.value_state('PID-7') is ValueState.NULL
    assert msg.value_state('PID-6') is ValueState.EMPTY
    assert msg.value_state('PID-99') is ValueState.ABSENT
    assert msg['PID-99'] == ''
    assert msg.get_raw('PID-99') is None
    assert msg.segment('OBX') is None
    with pytest.raises(LookupError):
        msg['OBX-5']


def test_assignment_levels_and_atomicity():
    msg = parse(BASE + 'PID|||||Smith^Jane~Jones^Jo\r')
    msg['PID-5'] = 'Doe^Jan~Roe^Joe'
    assert msg['PID-5[2].2'] == 'Joe'
    for ref, value in [('PID-5', 'Doe|Jan'), ('PID-5.1', 'Doe^Jan'), ('PID-5.1.1', 'Doe&Jan'), ('PID-5[1]', 'Doe~Jan')]:
        before = msg.to_text()
        with pytest.raises(AssignmentError):
            msg[ref] = value
        assert msg.to_text() == before
    msg.set_text('PID-5.1', 'A|B^C~D&E\\F\n')
    assert msg['PID-5.1'] == 'A|B^C~D&E\\F\n'
    msg.set_text('PID-5.1', '""')
    assert msg['PID-5.1'] == '""'
    msg['PID-5.1'] = NULL
    assert msg['PID-5.1'] is NULL


def test_msh_delimiters_and_independence():
    first = parse(BASE)
    second = parse('MSH*$%!?*S\nPID***123**Smith$Jane\n')
    assert first['MSH-1'] == '|'
    assert first['MSH-2'] == '^~\\&'
    assert second['PID-5.2'] == 'Jane'
    first['MSH-3'] = 'NEW'
    assert first.to_text().startswith('MSH|^~\\&|NEW|')
    with pytest.raises(AssignmentError):
        first['MSH-2'] = '!@#$'
    assert second['PID-5.2'] == 'Jane'


def test_bytes_preserve_and_encoding_conversion():
    source = BASE.encode() + b'PID|||||A\xffB\r'
    msg = parse(source)
    assert msg.to_bytes() == source
    msg['PID-3'] = '123'
    assert msg.to_bytes() == source.replace(b'PID|||', b'PID|||123')
    with pytest.raises(UnicodeEncodeError):
        msg.to_bytes(encoding='ascii')
    msg = parse(BASE + 'PID|||||André\r', encoding='iso8859-1')
    converted = msg.to_bytes(encoding='utf-8')
    assert b'Andr\xc3\xa9' in converted
    assert parse(converted)['MSH-18'] == 'UNICODE UTF-8'


def test_custom_delimiter_conversion():
    msg = parse(BASE + 'PID|||123||A\\S\\B^C&X~D\r')
    target = EncodingCharacters('*', '$', '%', '!', '?')
    result = parse(msg.to_text(delimiters=target))
    assert result['PID-5.1'] == 'A^B'
    assert result['PID-5.2.2'] == 'X'
    assert result['PID-5[2].1'] == 'D'


def test_topology_and_copy():
    msg = parse(BASE + 'PID|||123')
    copy = msg.copy()
    msg.append('OBX|1')
    assert msg.to_text().endswith('PID|||123\rOBX|1\r')
    assert copy.to_text().endswith('PID|||123')
    obx = msg.segment('OBX')
    msg.move(obx, 1)
    assert msg.segments()[1] is obx
    with pytest.raises(AssignmentError):
        msg.remove(msg.segment('MSH'))
    msg.segments().clear()
    assert msg.segment('PID') is not None


@given(st.text(alphabet=st.characters(min_codepoint=32, max_codepoint=126, blacklist_characters='|^~\\&'), max_size=80), st.sampled_from(['\r', '\n', '\r\n', '']))
def test_roundtrip(value, end):
    source = BASE + 'PID|||||' + value + '^^|||'+ end
    msg = parse(source)
    assert msg.to_text() == source
    assert parse(source.encode()).to_bytes() == source.encode()


def test_invalid_input():
    for value in ['PID|1', 'MSH|^^^^|', 'MSH|^~\\&|\rMSH|^~\\&|']:
        with pytest.raises(ParseError):
            parse(value)


def test_conversion_preserves_escaped_values_and_rejects_bad_edits():
    msg = parse(BASE + 'PID|||||Andr\\XE9\\\r', encoding='iso8859-1')
    assert parse(msg.to_bytes(encoding='utf-8'))['PID-5'] == 'André'
    for ref, value in [('PID-5', 'A\\'), ('MSH-18', 'ASCII')]:
        before = msg.to_text()
        with pytest.raises(AssignmentError):
            msg[ref] = value
        assert msg.to_text() == before
    original = msg.segment('MSH').get_raw()
    msg.segment('MSH').set_raw(None, original)
    assert msg.segment('MSH').get_raw() == original


def test_new_message_does_not_invent_business_metadata():
    from v2path import Message
    msg = Message.new()
    assert msg['MSH-9'] == ''
    assert msg['MSH-12'] == ''
    assert msg.to_bytes() == b'MSH|^~\\&\r'
    msg['MSH-9'] = 'ORU^R01'
    assert msg['MSH-9.1'] == 'ORU'


@given(st.text(alphabet=st.characters(min_codepoint=32, max_codepoint=126), max_size=80))
def test_literal_text_survives_delimiter_conversion(value):
    msg = parse(BASE + 'PID|||||\r')
    msg.set_text('PID-5.1', value)
    target = EncodingCharacters('*', '$', '%', '!', '?')
    converted = parse(msg.to_text(delimiters=target))
    assert converted['PID-5.1'] == value
    assert msg['PID-5.1'] == value


def test_literal_null_spelling_and_charset_subpaths():
    msg = parse(BASE + 'PID|||||\r')
    msg['PID-5'] = 'name^value'
    msg.set_text('PID-5.2', '""')
    target = EncodingCharacters('*', '$', '%', '!', '?')
    converted = parse(msg.to_text(delimiters=target))
    assert converted['PID-5.2'] == '""'
    assert converted.value_state('PID-5.2') is ValueState.VALUE
    before = msg.to_text()
    with pytest.raises(AssignmentError):
        msg['MSH-18.2'] = 'UTF-8'
    assert msg.to_text() == before
