import pytest
from v2path import Grammar, GroupRule, StaleViewError, parse, presets
from test_core import BASE


def message(*segments):
    return parse(BASE + '\r'.join(segments) + '\r')


@pytest.mark.parametrize('rows,parents', [
    (['OBR', 'OBX', 'ZXX', 'NTE'], ['OBX']),
    (['OBR', 'OBX', 'ORC', 'NTE', 'OBR'], ['ORC']),
    (['OBR', 'OBX', 'NTE'], ['OBX']),
    (['OBX', 'NTE', 'NTE'], ['OBX', 'OBX']),
])
def test_default_attachments(rows, parents):
    view = message(*rows).resolve()
    assert [s.parent().name for s in view.segments('NTE')] == parents
    assert view.diagnostics == []


def test_views_share_values_not_interpretation():
    msg = message('OBX|1', 'ZXX|2', 'NTE|1')
    first = msg.resolve()
    second = msg.resolve(Grammar(attachments={'NTE': ('ZXX',)}))
    assert first.segment('NTE').parent().name == 'OBX'
    assert second.segment('NTE').parent().name == 'ZXX'
    second.segment('ZXX')['1'] = '3'
    assert first.segment('ZXX')['1'] == '3'
    first.segment('OBX').add_child('NTE', '2')
    with pytest.raises(StaleViewError):
        second.segment('NTE')
    assert msg.resolve().segment('OBX').children('NTE')[0]['1'] == '1'


def test_scope_and_paths():
    grammar = Grammar((GroupRule('ORDER', 'ORC', children=(GroupRule('OBSERVATION', 'OBX'),)),))
    msg = message('ORC|1', 'OBX|1||||10', 'NTE|1', 'ORC|2', 'NTE|2')
    view = msg.resolve(grammar)
    assert len(view.groups('ORDER')) == 2
    assert view.groups('OBSERVATION') == []
    assert len(view.groups('OBSERVATION', recursive=True)) == 1
    assert view['ORDER[0]/OBSERVATION[0]/OBX-5'] == '10'
    view['ORDER[0]/OBSERVATION[0]/OBX-5'] = '20'
    assert msg['OBX-5'] == '20'
    assert view.group('ORDER', 1).segment('NTE').parent().name == 'ORC'
    assert view.group('MISSING') is None
    with pytest.raises(LookupError):
        view['ORDER[9]/ORC-1']


def test_no_anchor_in_new_scope():
    grammar = Grammar((GroupRule('NOTES', 'NTE'),))
    view = message('OBX', 'NTE').resolve(grammar)
    note = view.group('NOTES').segment('NTE')
    assert note.parent() is None
    assert [d.code for d in view.diagnostics] == ['unattached']


def test_group_move_and_append_preserve_unknowns():
    grammar = Grammar((GroupRule('ORDER', 'ORC'),))
    msg = message('ORC|1', 'ZXX|a', 'OBR|1', 'ORC|2', 'OBR|2')
    first = msg.resolve(grammar).group('ORDER')
    first.move(len(msg.segments()))
    assert [s.get_raw() for s in msg.segments()[1:]] == ['ORC|2', 'OBR|2', 'ORC|1', 'ZXX|a', 'OBR|1']
    with pytest.raises(StaleViewError):
        first.segments()


def test_conflicting_rules():
    with pytest.raises(ValueError):
        Grammar((GroupRule('A', 'OBX'), GroupRule('B', 'OBX')))
    view = message('OBX').resolve(Grammar((GroupRule('A', lambda c: c.segment.name == 'OBX'), GroupRule('B', lambda c: c.segment.name == 'OBX'))))
    assert [d.code for d in view.diagnostics] == ['ambiguous_boundary']
    assert view.segment('OBX') is not None


@pytest.mark.parametrize('grammar,rows,group', [
    (presets.ADT_A01, ['PID|1', 'PV1|1', 'OBX|1', 'NTE|1', 'IN1|1'], 'PATIENT'),
    (presets.SIU_S12, ['SCH|1', 'PID|1', 'RGS|1', 'AIS|1', 'NTE|1', 'RGS|2', 'AIP|1'], 'APPOINTMENT'),
    (presets.ORM_O01, ['PID|1', 'ORC|1', 'OBR|1', 'OBX|1', 'ORC|2', 'OBR|2'], 'ORDER'),
    (presets.ORU_R01, ['PID|1', 'ORC|1', 'OBR|1', 'OBX|1', 'NTE|1', 'SPM|1', 'OBX|2'], 'PATIENT_RESULT'),
    (presets.DFT_P03, ['PID|1', 'PV1|1', 'FT1|1', 'NTE|1', 'ORC|1', 'OBR|1', 'FT1|2', 'NTE|2'], 'PATIENT'),
])
def test_presets(grammar, rows, group):
    msg = message(*rows)
    raw = msg.to_text()
    view = msg.resolve(grammar)
    assert view.groups(group)
    assert msg.to_text() == raw
    assert not view.diagnostics


def test_optional_orc_pairing():
    for preset, name in [(presets.ORM_O01, 'ORDER'), (presets.ORU_R01, 'ORDER_OBSERVATION')]:
        msg = message('ORC|1', 'OBR|1', 'OBX|1', 'OBR|2', 'ORC|3', 'NTE|1', 'OBR|3')
        view = msg.resolve(preset)
        assert len(view.groups(name, recursive=True)) == 3


def test_nested_presets_keep_physical_ranges():
    msg = message('PID|1', 'OBR|1', 'SPM|1', 'OBX|1', 'NTE|1')
    view = msg.resolve(presets.ORU_R01)
    specimen = view.group('PATIENT_RESULT').group('ORDER_OBSERVATION').group('SPECIMEN')
    assert specimen.group('OBSERVATION').segment('NTE').parent().name == 'OBX'
    specimen.remove()
    assert [s.name for s in msg.segments()] == ['MSH', 'PID', 'OBR']
    msg = message('PID|1', 'FT1|1', 'ORC|1', 'OBR|1', 'OBX|1', 'FT1|2')
    view = msg.resolve(presets.DFT_P03)
    financial = view.group('PATIENT').group('FINANCIAL')
    assert financial.group('COMMON_ORDER').group('OBSERVATION').segment('OBX') is not None
    financial.remove()
    assert [s.get_raw() for s in msg.segments()[1:]] == ['PID|1', 'FT1|2']


def test_parent_scope_does_not_reuse_closed_child_anchor():
    grammar = Grammar((GroupRule('ORDER', 'ORC', children=(GroupRule('OBSERVATION', 'OBX', end_before=('CTI',)),)),))
    view = message('ORC', 'OBX', 'NTE', 'CTI', 'NTE').resolve(grammar)
    notes = view.segments('NTE', recursive=True)
    assert [n.parent().name for n in notes] == ['OBX', 'CTI']
