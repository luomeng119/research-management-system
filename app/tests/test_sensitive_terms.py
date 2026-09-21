"""KW-04/05/06/10 pure replacement behavior; no stored vocabulary implied."""
import copy
import pytest

from app.services.sensitive_terms import replace_sensitive_terms, compile_rules, replace_sensitive_fields, SensitiveTermError

RULES=[
    dict(id='project',source='青岚-07',replacement='PRJ-A'),
    dict(id='short',source='青岚',replacement='QL'),
    dict(id='unit',source='甲试验分队',replacement='[UNIT-A]'),
    dict(id='site',source='北区二号试验点',replacement='[SITE-B]'),
    dict(id='device',source='HX-3',replacement='DEV-03'),
    dict(id='internal',source='内部试验资料',replacement='※'),
]


def test_longest_first_and_exact_hits():
    result=replace_sensitive_terms('青岚-07、青岚、青岚-07，甲试验分队在北区二号试验点使用HX-3。内部试验资料',RULES)
    assert result['text']=='PRJ-A、QL、PRJ-A，[UNIT-A]在[SITE-B]使用DEV-03。※'
    assert result['hits']==dict(project=2,short=1,unit=1,site=1,device=1,internal=1)


def test_ascii_boundary_and_case():
    result=replace_sensitive_terms('HX-3 AHX-30 XHX-3 HX-3A HX-3_ _HX-3 hx-3 （HX-3）中文HX-3中文',RULES)
    assert result['text']=='DEV-03 AHX-30 XHX-3 HX-3A HX-3_ _HX-3 hx-3 （DEV-03）中文DEV-03中文'
    assert result['hits']=={'device':3}


def test_idempotence_and_existing_aliases():
    original='PRJ-A QL [UNIT-A] [SITE-B] DEV-03 ※ 青岚-07 青岚 HX-3'
    first=replace_sensitive_terms(original,RULES)
    second=replace_sensitive_terms(first['text'],RULES)
    assert first['text']==second['text']
    assert second['hits']=={}


def test_nonmatches_preserved_without_normalization():
    text='  Ａ²㎏\r\n\n甲\t青岚\u200b。  '
    assert replace_sensitive_terms(text,RULES)['text']=='  Ａ²㎏\r\n\n甲\tQL\u200b。  '
    assert replace_sensitive_terms('ordinary text',RULES)==dict(text='ordinary text',hits={})


@pytest.mark.parametrize('rules',[
    [dict(id='1',source='甲',replacement='A'),dict(id='2',source='甲',replacement='B')],
    [dict(id='1',source='甲',replacement='乙'),dict(id='2',source='乙',replacement='丙')],
    [dict(id='1',source='甲',replacement='甲')],
    [dict(id='1',source='HX-3',replacement='(HX-3)')],
    [dict(id='1',source='',replacement='A')],
    [dict(id='1',source='甲',replacement='')],
    [dict(id='1',source='甲',replacement='A'),dict(id='1',source='乙',replacement='B')],
])
def test_invalid_rules_error_does_not_expose_originals(rules):
    with pytest.raises(SensitiveTermError) as exc:
        compile_rules(rules)
    assert all(rule['source'] not in str(exc.value) for rule in rules if rule['source'])


def test_many_to_one_alias_is_explicitly_supported():
    rules=[dict(id='1',source='甲组',replacement='[UNIT]'),dict(id='2',source='乙组',replacement='[UNIT]')]
    assert replace_sensitive_terms('甲组乙组',rules)==dict(text='[UNIT][UNIT]',hits={'1':1,'2':1})


def test_recursive_fields_no_mutation_and_aggregate_counts():
    original={'title':'青岚', 'sources':[{'text':'青岚-07 HX-3'}], 'count':3,'ready':True,'optional':None}
    before=copy.deepcopy(original)
    result=replace_sensitive_fields(original,RULES)
    assert original==before
    assert result['value']==dict(title='QL',sources=[dict(text='PRJ-A DEV-03')],count=3,ready=True,optional=None)
    assert result['hits']==dict(short=1,project=1,device=1)
    with pytest.raises(SensitiveTermError):
        replace_sensitive_fields({'unsupported':object()},RULES)


def test_compiled_rules_do_not_follow_input_mutation():
    rules=copy.deepcopy(RULES)
    compiled=compile_rules(rules)
    rules[0]['replacement']='changed'
    assert replace_sensitive_terms('青岚-07',compiled)['text']=='PRJ-A'


def test_new_boundary_collision_is_rejected_without_recursive_rewrite():
    rules=[dict(id='mixed',source='甲X',replacement='※'),dict(id='ascii',source='A',replacement='Z')]
    with pytest.raises(SensitiveTermError,match='边界'):
        replace_sensitive_terms('甲XA',rules)


def test_recursive_cycle_and_nonfinite_numbers_rejected():
    cycle=[]
    cycle.append(cycle)
    for value in [cycle,float('nan'),{1:'青岚'},('青岚',)]:
        with pytest.raises(SensitiveTermError):
            replace_sensitive_fields(value,RULES)


def test_empty_rules_and_same_source_duplicate_are_explicit():
    assert replace_sensitive_terms('青岚',[])==dict(text='青岚',hits={})
    rule=dict(id='1',source='甲',replacement='A')
    with pytest.raises(SensitiveTermError):
        compile_rules([rule,dict(id='2',source='甲',replacement='A')])


def test_partial_alias_source_overlap_rejected_instead_of_silent_miss():
    rules=[dict(id='one',source='甲乙',replacement='[A]'),dict(id='two',source='戊',replacement='乙丙')]
    with pytest.raises(SensitiveTermError,match='交叠'):
        replace_sensitive_terms('甲乙丙',rules)


@pytest.mark.parametrize('field',['id','source','replacement'])
@pytest.mark.parametrize('bad',['\ud800','\x01','\ufffe'])
def test_rules_reject_invalid_unicode_and_xml_controls(field,bad):
    rule=dict(id='one',source='青岚',replacement='QL')
    rule[field]=bad
    with pytest.raises(SensitiveTermError):compile_rules([rule])
