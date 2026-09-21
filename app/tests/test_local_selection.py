import json
import pytest


def response(items):
    return {'choices':[{'finish_reason':'stop','message':{'role':'assistant','content':json.dumps({'suggestions':items},ensure_ascii=False)}}]}


def assistant(transport, **kwargs):
    from app.ai.local_selection import LocalSelectionAssistant
    return LocalSelectionAssistant(base_url='http://127.0.0.1:18081', model='local-test', transport=transport, **kwargs)


def test_single_local_call_and_processed_bounded_context():
    calls=[]
    service=assistant(lambda **kw: calls.append(kw) or response([{'text':'压力为 3 MPa，尚未完成。','reason':'措辞更清晰'}]), processor=lambda text:text.replace('敏感名称','单位甲'))
    result=service.suggest(selected_text='压力是 3 MPa，尚未完成。',prefix='敏感名称',suffix='尾文'*500)
    assert len(calls)==1 and calls[0]['no_proxy'] is True and calls[0]['timeout']<=60
    payload=json.dumps(calls[0]['payload'],ensure_ascii=False)
    assert '敏感名称' not in payload and '单位甲' in payload
    assert len(json.loads(calls[0]['payload']['messages'][1]['content'])['readOnlyContext']['after'])==300
    assert result['suggestions'][0]['text']=='压力为 3 MPa，尚未完成。'
    assert result['processorStatus']=='APPLIED_UNVERSIONED'


@pytest.mark.parametrize('candidate',['压力为 4 MPa，尚未完成。','压力为 3 kPa，尚未完成。','压力为 3 MPa，已完成。'])
def test_numeric_unit_or_explicit_state_change_rejected(candidate):
    service=assistant(lambda **kw:response([{'text':candidate,'reason':'改写'}]))
    with pytest.raises(ValueError):
        service.suggest(selected_text='压力是 3 MPa，尚未完成。',prefix='',suffix='')


@pytest.mark.parametrize('items',[[],[{'text':'','reason':'x'}],[{'text':'说明','reason':''}],[{'text':'x','reason':'y'}]*4])
def test_invalid_candidate_structure_rejected(items):
    with pytest.raises(ValueError):
        assistant(lambda **kw:response(items)).suggest(selected_text='原文',prefix='',suffix='')


def test_nonlocal_endpoint_rejected_before_transport():
    from app.ai.local_selection import LocalSelectionAssistant
    with pytest.raises(ValueError):
        LocalSelectionAssistant(base_url='https://example.com',model='x')


def test_empty_selection_never_calls_transport():
    calls=[]
    with pytest.raises(ValueError):
        assistant(lambda **kw:calls.append(kw)).suggest(selected_text='  ',prefix='',suffix='')
    assert not calls


def test_processor_also_filters_candidate_and_reason():
    service=assistant(lambda **kw:response([{'text':'敏感名称的研究表述','reason':'敏感名称相关措辞'}]),processor=lambda value:value.replace('敏感名称','单位甲'))
    result=service.suggest(selected_text='敏感名称的研究表达',prefix='',suffix='')
    assert '敏感名称' not in json.dumps(result,ensure_ascii=False)
    assert result['suggestions'][0]['text']=='单位甲的研究表述'


def test_authorized_numeric_model_alias_compares_against_processed_selection():
    service=assistant(lambda **kw:response([{'text':'DEV-03 的温度偏差是 0.6℃，未达标。','reason':'措辞调整'}]),processor=lambda value:value.replace('HX-3','DEV-03'))
    result=service.suggest(selected_text='HX-3 的温度偏差为 0.6℃，未达标。',prefix='',suffix='')
    assert result['suggestions'][0]['text'].startswith('DEV-03')


def test_reference_label_change_rejected():
    service=assistant(lambda **kw:response([{'text':'参考[文献S1]的结果','reason':'修改'}]))
    with pytest.raises(ValueError):
        service.suggest(selected_text='参考[来源S1]的结果',prefix='',suffix='')


def test_sensitive_term_crosses_selection_boundary_fails_before_model():
    calls=[]
    service=assistant(lambda **kw:calls.append(kw),processor=lambda text:text.replace('甲试验分队','[UNIT]'))
    with pytest.raises(ValueError):
        service.suggest(selected_text='试验分队',prefix='甲',suffix='')
    assert not calls


def test_context_is_processed_before_300_character_window():
    calls=[]
    service=assistant(lambda **kw:calls.append(kw) or response([{'text':'报告表述','reason':'措辞'}]),processor=lambda text:text.replace('甲试验分队','[UNIT]'))
    service.suggest(selected_text='报告表达',prefix='前文'*100+'甲试验分队'+'尾'*297,suffix='')
    context=json.loads(calls[0]['payload']['messages'][1]['content'])
    assert '试验分队' not in context['readOnlyContext']['before'] and '甲试验' not in context['readOnlyContext']['before']


def test_whitespace_context_is_allowed():
    result=assistant(lambda **kw:response([{'text':'更清晰表述','reason':'简洁'}])).suggest(selected_text='原表达',prefix='  ',suffix='\n')
    assert result['suggestions'][0]['text']=='更清晰表述'


def test_adopted_v2_payload_preserves_context_and_sampling_configuration():
    from app.ai.local_selection import PROMPT_VERSION
    calls=[]
    result=assistant(lambda **kw:calls.append(kw) or response([{'text':'这项工作主要整理记录。','reason':'删去冗余措辞'}])).suggest(selected_text='这项工作主要是对记录进行整理。',prefix='前文不输出。',suffix='后文不输出。')
    payload=calls[0]['payload']
    assert json.loads(payload['messages'][1]['content'])==dict(selectionToRewrite='这项工作主要是对记录进行整理。',readOnlyContext=dict(before='前文不输出。',after='后文不输出。'))
    assert payload['temperature']==1 and payload['top_p']==0.95
    assert result['promptVersion']==PROMPT_VERSION=='selection-readonly-context-v2'
    assert len(calls)==1


# Frozen replay of model-decision-9b/selection/01-completions.response.json.
# No inference is performed by this regression.
LIVE_9B_SELECTION = 'PRJ-A的DEV-03样机v1第三点偏差为0.6℃，超过允许误差0.5℃，不能判为全部通过。'
LIVE_9B_CANDIDATES = [
    {'reason': '提升专业性与表达严谨度，将口语化表述转化为标准科研用语',
     'text': 'PRJ-A的DEV-03样机v1第三点偏差达0.6℃，超出允许误差0.5℃，故该项未获通过。'},
    {'reason': '精简冗余词汇，增强句子紧凑度',
     'text': 'PRJ-A的DEV-03样机v1第三点偏差0.6℃，逾允许误差0.5℃，不能判定全部通过。'},
    {'reason': '使用更精准的动词描述偏差状态及结论',
     'text': 'PRJ-A的DEV-03样机v1第三点偏差为0.6℃，超允许误差0.5℃，不予通过。'},
]


def test_real_9b_replay_returns_only_second_candidate():
    calls=[]
    result=assistant(lambda **kw:calls.append(kw) or response(LIVE_9B_CANDIDATES)).suggest(
        selected_text=LIVE_9B_SELECTION, prefix='[UNIT-A]在[SITE-B]整理※。',
        suffix='尚无完整温区、续航、可靠性记录。')
    assert result['suggestions']==[LIVE_9B_CANDIDATES[1]]
    assert len(calls)==1


def test_real_9b_replay_all_unsafe_still_fails():
    with pytest.raises(ValueError):
        assistant(lambda **kw:response([LIVE_9B_CANDIDATES[0],LIVE_9B_CANDIDATES[2]])).suggest(
            selected_text=LIVE_9B_SELECTION)


@pytest.mark.parametrize('unsafe',[
    '压力为 4 MPa，尚未完成。[来源S1]',
    '压力为 3 kPa，尚未完成。[来源S1]',
    '压力为 3 MPa，已完成。[来源S1]',
    '压力为 3 MPa，尚未完成。[来源S2]',
])
def test_unsafe_and_duplicate_candidates_are_filtered_individually(unsafe):
    safe={'text':'压力为 3 MPa，尚未完成。[来源S1]','reason':'简洁措辞'}
    result=assistant(lambda **kw:response([{'text':unsafe,'reason':'改写'},safe,dict(safe)])).suggest(
        selected_text='压力是 3 MPa，尚未完成。[来源S1]')
    assert result['suggestions']==[safe]
