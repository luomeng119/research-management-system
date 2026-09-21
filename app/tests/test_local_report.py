import json
import pytest
from app.ai.local_report import LocalReportAssistant


def make(content=None, finish='stop'):
    return {'choices': [{'finish_reason': finish, 'message': {'role': 'assistant', 'content': json.dumps({'body': content or '资料整合结果[来源S1]。待确认项：未提供实测结果。'}, ensure_ascii=False)}}], 'model': 'Qwen3.5-9B', 'usage': {'prompt_tokens': 12, 'completion_tokens': 24}}


def call(transport, **kw):
    return LocalReportAssistant(base_url='http://127.0.0.1:18081', model='Qwen3.5-9B', transport=transport).generate(title='科研报告', purpose='立项依据', current_body='', sources=[{'sourceId': 'S1', 'text': '仅计划，无实测。'}], **kw)


def test_local_report_keeps_sources_and_metadata_and_uses_no_proxy():
    calls=[]
    def transport(**kw):
        calls.append(kw)
        if kw['url'].endswith('/apply-template'): return {'prompt':'模板内容'}
        return {'tokens':[1,2]} if kw['url'].endswith('/tokenize') else make()
    result=call(transport)
    assert result['body'].endswith('未提供实测结果。')
    assert result['model']=='Qwen3.5-9B'
    assert result['promptVersion'] and len(result['inputHash'])==64
    assert all(item['no_proxy'] is True for item in calls)
    payload=calls[-1]['payload']
    assert payload['chat_template_kwargs']=={'enable_thinking':False}
    assert '仅计划，无实测。' in payload['messages'][1]['content']


@pytest.mark.parametrize('bad', [make(finish='length'), make('没有来源'), make('错误来源[来源S9]'), {'choices':[]}])
def test_bad_model_output_fails_without_fake_report(bad):
    def transport(**kw):
        if kw['url'].endswith('/apply-template'): return {'prompt':'模板内容'}
        return {'tokens':[1]} if kw['url'].endswith('/tokenize') else bad
    with pytest.raises(RuntimeError): call(transport)


def test_context_overflow_rejected_before_generation():
    calls=[]
    def transport(**kw):
        calls.append(kw)
        if kw['url'].endswith('/apply-template'): return {'prompt':'模板内容'}
        return {'tokens':[1]*5001}
    with pytest.raises(ValueError,match='资料过长'): call(transport)
    assert len(calls)==2


def test_remote_endpoint_rejected():
    with pytest.raises(ValueError): LocalReportAssistant(base_url='http://example.com:80',model='m')


def test_cancelled_input_never_calls_transport():
    calls=[]
    with pytest.raises(InterruptedError): call(lambda **kw: calls.append(kw),cancel_check=lambda:True)
    assert calls==[]


def test_shared_deadline_expires_before_next_request(monkeypatch):
    import app.ai.local_report as module
    ticks=iter([0,0,61])
    monkeypatch.setattr(module.time,'monotonic',lambda:next(ticks))
    calls=[]
    def transport(**kw):
        calls.append(kw)
        return {'prompt':'模板内容'}
    with pytest.raises(TimeoutError): call(transport)
    assert len(calls)==1


def test_revision_request_carries_complete_fact_and_evidence_update_instructions():
    """Prompt contract test only; fake transport cannot certify model semantics."""
    calls = []
    def transport(**kwargs):
        calls.append(kwargs)
        if kwargs['url'].endswith('/apply-template'):
            return {'prompt': '模板内容'}
        if kwargs['url'].endswith('/tokenize'):
            return {'tokens': [1, 2]}
        return make('当前决定已确认[来源S2]。')
    old_body = '两种方案尚未达成一致[来源S1]。'
    sources = [{'sourceId':'S1','text':'早期方案存在分歧。'},
               {'sourceId':'S2','text':'后续书面决定已解决该分歧。'}]
    result = LocalReportAssistant(base_url='http://127.0.0.1:18081',model='test',transport=transport).generate(
        title='阶段报告',purpose='整合研究进展',current_body=old_body,sources=sources)
    messages = calls[-1]['payload']['messages']
    system = messages[0]['content']
    for instruction in ('研究对象、研究目标和预期或已完成成果', '方案与方法', '原始数值及单位',
                        '每个事实句', '真正支持该事实', 'currentBody是较早版本',
                        '新来源明确记录较晚决定', '旧说法标为历史', '不能同时写成仍未解决',
                        '没有证据解决的分歧仍并列呈现'):
        assert instruction in system
    assert calls[0]['payload']['messages'] == messages
    assert json.loads(messages[1]['content']) == dict(title='阶段报告',purpose='整合研究进展',currentBody=old_body,sources=sources)
    assert result['body'] == '当前决定已确认[来源S2]。'
    assert result['promptVersion'] == 'research-report-sources-v3'


@pytest.mark.parametrize('body,accepted', [
    ('事实[来源 S1]', True), ('事实[来源  S1]', True), ('事实[来源S1]', True),
    ('事实[来源 S9]', False), ('事实无标签', False), ('事实[来源 S01]', False),
    ('事实[来源 S1,S2]', False), ('事实[来源 S1 ]', False),
    ('事实[来源\tS1]', False), ('事实[来源\u3000S1]', False),
    ('事实[来源S1]另一个[来源 S9]', False),
    ('事实[来源S1]另一个[来源 S1', False),
])
def test_citation_ascii_space_policy_preserves_body_and_membership(body, accepted):
    def transport(**kwargs):
        if kwargs['url'].endswith('/apply-template'): return {'prompt':'模板'}
        if kwargs['url'].endswith('/tokenize'): return {'tokens':[1]}
        return make(body)
    if accepted:
        assert call(transport)['body'] == body
    else:
        with pytest.raises(RuntimeError): call(transport)


def test_real_9b_revision_raw_response_offline_replay():
    """Replay optional local evidence; no model call or semantic certification."""
    from pathlib import Path
    folder = Path(__file__).parents[2] / 'build/local-offline-20260910/field-terminal/model-decision-9b/revision'
    if not (folder/'03-completions.response.raw').exists():
        pytest.skip('Local real-response evidence not present in this checkout')
    response = json.loads((folder/'03-completions.response.raw').read_bytes())
    request = json.loads((folder/'03-completions.request.json').read_text())['payload']
    data = json.loads(request['messages'][1]['content'])
    original_body = json.loads(response['choices'][0]['message']['content'])['body']
    assert '[来源 S1]' in original_body
    def transport(**kwargs):
        if kwargs['url'].endswith('/apply-template'): return {'prompt':'离线回放'}
        if kwargs['url'].endswith('/tokenize'): return {'tokens':[1]}
        return response
    result = LocalReportAssistant(base_url='http://127.0.0.1:18081',model=request['model'],transport=transport).generate(
        title=data['title'],purpose=data['purpose'],current_body=data['currentBody'],sources=data['sources'])
    assert result['body'] == original_body
    # Removing a cited source must still fail with the identical raw response.
    with pytest.raises(RuntimeError):
        LocalReportAssistant(base_url='http://127.0.0.1:18081',model=request['model'],transport=transport).generate(
            title=data['title'],purpose=data['purpose'],current_body=data['currentBody'],sources=[s for s in data['sources'] if s['sourceId']!='S1'])


@pytest.mark.parametrize('body,accepted', [
    ('事实（S1）', True), ('事实(S1)', True), ('事实（S1, S2，S3）', True),
    ('事实(S1, S2)[来源 S3]', True), ('正文普通S1不是引用', False),
    ('事实（S9）', False), ('事实（S1, S9）[来源S1]', False),
    ('事实（S1; S2）[来源S1]', False), ('事实（S1,未知）[来源S1]', False),
    ('事实（S1)[来源S1]', False), ('事实(S1）[来源S1]', False),
    ('事实（S1,）[来源S1]', False), ('事实（S01）[来源S1]', False),
    ('事实（S1[来源S1]', False),
    ('事实[来源S1]（\tS9）', False),
    ('事实[来源S1]（S1/S2）', False),
    ('正文普通S1，另有事实[来源S2]', True),
])
def test_parenthetical_citation_lists_are_complete_and_bounded(body,accepted):
    def transport(**kw):
        if kw['url'].endswith('/apply-template'): return {'prompt':'模板'}
        if kw['url'].endswith('/tokenize'): return {'tokens':[1]}
        return make(body)
    assistant=LocalReportAssistant(base_url='http://127.0.0.1:18081',model='test',transport=transport)
    def generate():
        return assistant.generate(title='报告',purpose='测试',current_body='',sources=[dict(sourceId=f'S{i}',text='资料') for i in range(1,4)])
    if accepted: assert generate()['body']==body
    else:
        with pytest.raises(RuntimeError): generate()


def test_real_stage_e_parenthetical_response_offline_replay():
    from pathlib import Path
    folder=Path(__file__).parents[2]/'build/local-offline-20260910/field-terminal/stage-e-ui-report-diagnostic'
    if not (folder/'03-response.json').exists(): pytest.skip('Local UI diagnostic evidence absent')
    response=json.loads((folder/'03-response.json').read_text())
    original=json.loads(response['choices'][0]['message']['content'])['body']
    assert '（S1, S2, S3）' in original
    def transport(**kw):
        if kw['url'].endswith('/apply-template'): return {'prompt':'离线模板'}
        if kw['url'].endswith('/tokenize'): return {'tokens':[1]}
        return response
    assistant=LocalReportAssistant(base_url='http://127.0.0.1:18081',model=response['model'],transport=transport)
    sources=[dict(sourceId=f'S{i}',text='回放仅验证编号归属') for i in range(1,4)]
    assert assistant.generate(title='报告',purpose='离线回放',current_body='',sources=sources)['body']==original
    with pytest.raises(RuntimeError):
        assistant.generate(title='报告',purpose='离线回放',current_body='',sources=sources[:2])


@pytest.mark.parametrize('body', [
    '事实（S1[来源S2]）', '事实(S1[来源S2])',
    '事实[来源（S2）S1]', '事实[来源S1（S2）]',
    '事实（S9[来源S2]）', '事实（S1[来源S2]',
    '事实[来源S1（S2）',
])
def test_nested_citations_cannot_create_valid_tags_during_validation(body):
    def transport(**kw):
        if kw['url'].endswith('/apply-template'): return {'prompt':'模板'}
        if kw['url'].endswith('/tokenize'): return {'tokens':[1]}
        return make(body)
    assistant=LocalReportAssistant(base_url='http://127.0.0.1:18081',model='test',transport=transport)
    with pytest.raises(RuntimeError):
        assistant.generate(title='报告',purpose='测试',current_body='',sources=[dict(sourceId='S2',text='仅S2可用')])


@pytest.mark.parametrize('body,source_ids,accepted', [
    ('事实（S1-S3）',['S1','S2','S3'],True),
    ('事实(S1 - S3)',['S1','S2','S3'],True),
    ('事实（S8-S8）',['S8'],True),
    ('事实（S1-S3）[来源S1]',['S1','S3'],False),
    ('事实（S3-S1）[来源S1]',['S1','S2','S3'],False),
    ('事实（S1-S9）[来源S1]',['S1'],False),
    ('事实（S0-S1）[来源S1]',['S1'],False),
    ('事实（S1-S3,S4-S5）[来源S1]',['S1'],False),
    ('事实（S1-S3[来源S2]）',['S2'],False),
    ('事实（S1-S3)[来源S1]',['S1'],False),
    ('普通正文S1-S3',['S1','S2','S3'],False),
])
def test_single_parenthetical_range_checks_every_source(body,source_ids,accepted):
    def transport(**kw):
        if kw['url'].endswith('/apply-template'): return {'prompt':'模板'}
        if kw['url'].endswith('/tokenize'): return {'tokens':[1]}
        return make(body)
    assistant=LocalReportAssistant(base_url='http://127.0.0.1:18081',model='test',transport=transport)
    def generate():
        return assistant.generate(title='报告',purpose='测试',current_body='',sources=[dict(sourceId=s,text='资料') for s in source_ids])
    if accepted: assert generate()['body']==body
    else:
        with pytest.raises(RuntimeError): generate()


def test_real_project_report_range_response_offline_replay():
    from pathlib import Path
    folder=Path(__file__).parents[2]/'build/local-offline-20260910/field-terminal/stage-e-project-report-diagnostic'
    if not (folder/'03-response.json').exists(): pytest.skip('Local project diagnostic evidence absent')
    response=json.loads((folder/'03-response.json').read_text())
    request=json.loads((folder/'03-request.json').read_text())['payload']
    data=json.loads(request['messages'][1]['content'])
    original=json.loads(response['choices'][0]['message']['content'])['body']
    assert '（S1-S3）' in original
    def transport(**kw):
        if kw['url'].endswith('/apply-template'): return {'prompt':'离线模板'}
        if kw['url'].endswith('/tokenize'): return {'tokens':[1]}
        return response
    assistant=LocalReportAssistant(base_url='http://127.0.0.1:18081',model=request['model'],transport=transport)
    def generate(sources):
        return assistant.generate(title=data['title'],purpose=data['purpose'],current_body=data['currentBody'],sources=sources)
    assert generate(data['sources'])['body']==original
    with pytest.raises(RuntimeError): generate([s for s in data['sources'] if s['sourceId']!='S2'])


def test_revision_rebinds_source_labels_and_distinguishes_unknown_from_failed():
    calls=[]
    def transport(**kw):
        calls.append(kw)
        if kw['url'].endswith('/apply-template'): return {'prompt':'模板'}
        if kw['url'].endswith('/tokenize'): return {'tokens':[1]}
        return make('方案来自本次资料[来源S2]；验证记录待补。')
    old='方案采用自动记录[来源S1]。'
    sources=[dict(sourceId='S1',text='后续决定已确认。'),dict(sourceId='S2',text='方案采用自动记录。尚未提供验证记录。')]
    result=LocalReportAssistant(base_url='http://127.0.0.1:18081',model='test',transport=transport).generate(title='修订报告',purpose='进展',current_body=old,sources=sources)
    system=calls[-1]['payload']['messages'][0]['content']
    assert '旧正文的来源编号不得未经核对直接沿用' in system
    assert '按本次来源核对后仍相同可保留' in system
    assert '所有事实必须按本次来源快照重新绑定' in system
    assert '缺少或未提供验证记录只能写未知、待补或不能判定' in system
    assert '不得写成未通过或验证失败' in system
    assert json.loads(calls[-1]['payload']['messages'][1]['content'])['sources']==sources
    assert json.loads(calls[-1]['payload']['messages'][1]['content'])['currentBody']==old
    assert result['promptVersion']=='research-report-sources-v3'
