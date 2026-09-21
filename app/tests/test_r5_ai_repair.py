import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import MagicMock

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from app.ai.contract import CONTENT_FIELDS, FIELD_LIMITS, parse_assistant_content
from app.ai.deepseek import DeepSeekProposalAssistant
from app.tests.test_assistant import engine, service, VALID, _assistant_service
from app.services.assistant import AssistantService, AssistantServiceError
from app.services.audit import AuditService
from app.repositories.audit import AuditRepository


def response(text='{}', **changes):
    value = {
        'status': 'completed', 'model': 'configured-model',
        'output': [{'type': 'message', 'status': 'completed', 'role': 'assistant',
                    'content': [{'type': 'output_text', 'text': text}]}],
        'usage': {'input_tokens': 12, 'output_tokens': 34},
    }
    value.update(changes)
    return value


def test_responses_contract_preserves_limits_model_cancel_and_usage():
    calls = []
    content = {field: '' if field in CONTENT_FIELDS[:2] else [] for field in CONTENT_FIELDS}
    content['missingInformation'] = ['研究对象']
    raw = json.dumps(content)
    def transport(**kwargs):
        calls.append(kwargs)
        return response(raw)
    provider = DeepSeekProposalAssistant(api_key='test', model='configured-model', transport=transport)
    cancel = lambda: False
    assert parse_assistant_content(provider.generate('整理材料', deadline_seconds=90, cancel_check=cancel)) == content
    assert len(calls) == 1
    request = calls[0]
    assert request['url'] == 'https://api.deepseek.com/responses'
    assert request['timeout'] == 60
    assert request['cancel_check'] is cancel
    payload = request['payload']
    assert payload['model'] == 'configured-model'
    assert payload['reasoning'] == {'effort': 'none'}
    assert payload['max_output_tokens'] == 2000
    assert payload['stream'] is False
    assert payload['temperature'] == 0
    assert not {'tools', 'messages', 'response_format', 'max_tokens'} & payload.keys()
    schema = payload['text']['format']['schema']
    assert payload['text']['format']['type'] == 'json_schema'
    assert set(schema['required']) == set(CONTENT_FIELDS)
    assert set(schema['properties']) == set(CONTENT_FIELDS)
    assert schema['additionalProperties'] is False
    for field in CONTENT_FIELDS:
        prop = schema['properties'][field]
        if field in CONTENT_FIELDS[:2]:
            assert prop == {'type': 'string', 'maxLength': FIELD_LIMITS[field]}
        else:
            assert prop['maxItems'] == (6 if field == 'missingInformation' else 20)
            assert prop['items']['maxLength'] == FIELD_LIMITS[field]
    assert provider.last_metadata == {'model': 'configured-model', 'inputTokens': 12, 'outputTokens': 34}


@pytest.mark.parametrize('bad', [
    None, [], {}, response(status='incomplete'), response(status='failed'),
    response(error={'code': 'failed'}), response(incomplete_details={'reason': 'max_output_tokens'}),
    response(output=[]), response(output=[None]),
    response(output=[{'type': 'function_call'}]),
    response(output=[{'type': 'message', 'status': 'incomplete', 'role': 'assistant', 'content': []}]),
    response(output=[{'type': 'message', 'status': 'completed', 'role': 'assistant', 'content': [{'type': 'refusal', 'text': '拒绝'}]}]),
    response(text=123), response(text=''),
])
def test_invalid_or_incomplete_response_fails_once(bad):
    calls = []
    def transport(**kwargs):
        calls.append(kwargs)
        return bad
    provider = DeepSeekProposalAssistant(api_key='test', model='configured-model', transport=transport)
    with pytest.raises(RuntimeError):
        provider.generate('整理材料', deadline_seconds=2, cancel_check=lambda: False)
    assert len(calls) == 1


def test_metadata_is_cleared_before_failed_next_call():
    values = iter([response(), TimeoutError('timeout')])
    def transport(**kwargs):
        value = next(values)
        if isinstance(value, Exception):
            raise value
        return value
    provider = DeepSeekProposalAssistant(api_key='test', model='configured-model', transport=transport)
    provider.generate('整理材料', deadline_seconds=2, cancel_check=lambda: False)
    with pytest.raises(TimeoutError):
        provider.generate('整理材料', deadline_seconds=2, cancel_check=lambda: False)
    assert provider.last_metadata == {}


def test_concurrent_calls_have_independent_usage():
    barrier = Barrier(2)
    def transport(**kwargs):
        count = int(kwargs['payload']['input'][1]['content'])
        return response(usage={'input_tokens': count, 'output_tokens': count + 1})
    provider = DeepSeekProposalAssistant(api_key='test', model='configured-model', transport=transport)
    def call(count):
        provider.generate(str(count), deadline_seconds=2, cancel_check=lambda: False)
        barrier.wait(timeout=5)
        return provider.last_metadata['inputTokens']
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(call, [10, 20])) == [10, 20]
    assert provider.last_metadata == {}


@pytest.mark.parametrize('usage', [None, {}, [], {'input_tokens': True, 'output_tokens': -1},
                                   {'input_tokens': '12', 'output_tokens': 3.5}])
def test_missing_or_invalid_usage_stays_unknown(usage):
    provider = DeepSeekProposalAssistant(api_key='test', model='configured-model',
                                        transport=lambda **kwargs: response(usage=usage))
    provider.generate('整理材料', deadline_seconds=2, cancel_check=lambda: False)
    assert provider.last_metadata['inputTokens'] is None
    assert provider.last_metadata['outputTokens'] is None


@pytest.mark.parametrize('kind, usage, expected', [
    ('DEEPSEEK', {'input_tokens': 12, 'output_tokens': 34}, (12, 34)),
    ('DEEPSEEK', None, (None, None)),
    ('DEEPSEEK', {'input_tokens': True, 'output_tokens': '34'}, (None, None)),
    ('LOCAL', {'input_tokens': 99, 'output_tokens': 88}, (99, 88)),
])
def test_success_audit_records_only_known_tokens(engine, service, kind, usage, expected):
    created = service.create({'title': '测试', 'sourceType': 'IDEA', 'sourceSummary': '材料',
                             'researchProblem': '问题', 'objectives': '目标',
                             'researchContent': '内容', 'expectedOutcomes': '成果'},
                             actor_user_id=7, request_id='create')
    provider = DeepSeekProposalAssistant(api_key='test', model='configured-model',
        transport=lambda **kwargs: response(json.dumps(VALID), usage=usage))
    provider.provider_kind = kind
    assistant = _assistant_service(engine, service.audit_service, provider)
    assistant.generate(created['businessId'], source_text='脱敏材料', selected_file_ids=[],
        proposal_version=1, run_id='usage', actor_user_id=7, request_id='usage',
        remote_input_confirmed=True)
    properties = service.audit_service.events[-1]['properties']
    assert (properties['input_token_count'], properties['output_token_count']) == expected
    # A subsequent provider failure must not reuse the preceding success usage.
    def failing(**kwargs):
        raise TimeoutError('timeout')
    provider._transport = failing
    with pytest.raises(AssistantServiceError):
        assistant.generate(created['businessId'], source_text='脱敏材料', selected_file_ids=[],
            proposal_version=1, run_id='failed', actor_user_id=7, request_id='failed',
            remote_input_confirmed=True)
    failed = service.audit_service.events[-1]['properties']
    assert failed['input_token_count'] is None
    assert failed['output_token_count'] is None
    assert provider.last_metadata == {}


def _mock_failure_service(provider):
    repository = MagicMock()
    repository.get_proposal.return_value = {'id': 'proposal', 'status': 'DRAFT', 'version': 1}
    return AssistantService(repository, MagicMock(), provider)


def _generate_failure(assistant, run_id='failure', source='脱敏材料'):
    with pytest.raises(AssistantServiceError) as error:
        assistant.generate('proposal', source_text=source, selected_file_ids=[],
                           proposal_version=1, run_id=run_id, actor_user_id=7,
                           request_id=run_id, remote_input_confirmed=True)
    return error.value.code


@pytest.mark.parametrize('failure', ['schema', 'cancel', 'version', 'state', 'write'])
@pytest.mark.parametrize('kind, usage, expected', [
    ('DEEPSEEK', {'input_tokens': 1345, 'output_tokens': 38}, (1345, 38)),
    ('DEEPSEEK', None, (None, None)),
    ('DEEPSEEK', {'input_tokens': 0, 'output_tokens': -1}, (0, None)),
    ('LOCAL', {'input_tokens': 99, 'output_tokens': 88}, (99, 88)),
])
def test_failure_audit_keeps_only_this_completed_provider_usage(failure, kind, usage, expected):
    provider = DeepSeekProposalAssistant(api_key='test', model='configured-model',
        transport=lambda **kwargs: response('{}' if failure == 'schema' else json.dumps(VALID), usage=usage))
    provider.provider_kind = kind
    assistant = _mock_failure_service(provider)
    original_generate = provider.generate
    def generate(*args, **kwargs):
        raw = original_generate(*args, **kwargs)
        if failure == 'cancel':
            assistant.cancel('failure', actor_user_id=7, request_id='cancel')
        elif failure in ('version', 'state'):
            assistant.repository.get_proposal.return_value = {
                'id': 'proposal', 'status': 'SUBMITTED' if failure == 'state' else 'DRAFT',
                'version': 2 if failure == 'version' else 1}
        return raw
    provider.generate = generate
    if failure == 'write':
        assistant.repository.insert_draft.side_effect = RuntimeError('write failed')
    code = _generate_failure(assistant)
    assert code == {'schema': 'AI_OUTPUT_INVALID', 'cancel': 'AI_CANCELLED',
                    'version': 'VERSION_CONFLICT', 'state': 'STATE_CONFLICT',
                    'write': 'AI_UNAVAILABLE'}[failure]
    props = assistant.audit_service.record.call_args.kwargs['properties']
    if failure == 'cancel':
        assert assistant.audit_service.record.call_count == 1
        assert assistant.audit_service.record.call_args.kwargs['result'] == 'CANCELLED'
        expected = (None, None)
    assert (props['input_token_count'], props['output_token_count']) == expected
    # A provider exception after this request must keep both counts unknown.
    assistant.repository.get_proposal.return_value = {'id': 'proposal', 'status': 'DRAFT', 'version': 1}
    provider.generate = MagicMock(side_effect=TimeoutError('provider failed before returning'))
    assert _generate_failure(assistant, run_id='next') == 'AI_TIMEOUT'
    props = assistant.audit_service.record.call_args.kwargs['properties']
    assert (props['input_token_count'], props['output_token_count']) == (None, None)


def test_concurrent_failure_audits_keep_request_usage():
    barrier = Barrier(2)
    def transport(**kwargs):
        count = int(kwargs['payload']['input'][1]['content'])
        return response('{}', usage={'input_tokens': count, 'output_tokens': count + 1})
    provider = DeepSeekProposalAssistant(api_key='test', model='configured-model', transport=transport)
    original_generate = provider.generate
    def generate(*args, **kwargs):
        raw = original_generate(*args, **kwargs)
        barrier.wait(timeout=5)
        return raw
    provider.generate = generate
    assistant = _mock_failure_service(provider)
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(lambda count: _generate_failure(assistant, str(count), str(count)),
                             [10, 20])) == ['AI_OUTPUT_INVALID', 'AI_OUTPUT_INVALID']
    recorded = {call.kwargs['request_id']: call.kwargs['properties']
                for call in assistant.audit_service.record.call_args_list}
    for count in [10, 20]:
        assert recorded[str(count)]['input_token_count'] == count
        assert recorded[str(count)]['output_token_count'] == count + 1


@pytest.mark.parametrize('error, code', [
    (TimeoutError('timeout'), 'AI_TIMEOUT'),
    (InterruptedError('interrupted'), 'AI_TIMEOUT'),
    (RuntimeError('provider failed'), 'AI_UNAVAILABLE'),
])
def test_provider_exception_never_reads_stale_usage(error, code):
    provider = MagicMock(provider_kind='DEEPSEEK', model_version='configured-model')
    provider.last_metadata = {'inputTokens': 1345, 'outputTokens': 38}
    provider.generate.side_effect = error
    assistant = _mock_failure_service(provider)
    assert _generate_failure(assistant) == code
    props = assistant.audit_service.record.call_args.kwargs['properties']
    assert (props['input_token_count'], props['output_token_count']) == (None, None)


def test_failure_audit_outage_preserves_original_parser_error():
    provider = DeepSeekProposalAssistant(api_key='test', model='configured-model',
                                        transport=lambda **kwargs: response('{}'))
    assistant = _mock_failure_service(provider)
    assistant.audit_service.record.side_effect = RuntimeError('audit unavailable')
    assert _generate_failure(assistant) == 'AI_OUTPUT_INVALID'
    props = assistant.audit_service.record.call_args.kwargs['properties']
    assert (props['input_token_count'], props['output_token_count']) == (12, 34)


@pytest.mark.parametrize('result', ['SUCCESS', 'FAILURE', 'CANCELLED'])
@pytest.mark.parametrize('value', [0, 38, None])
def test_real_audit_preserves_safe_usage_and_filters_secrets(result, value):
    repository = MagicMock()
    audit = AuditService(repository, 'test')
    audit.record(object(), event_name='assistant_generation_completed', user_id=7,
                 object_type='ASSISTANT_DRAFT', result=result, request_id='usage',
                 duration_ms=0, error_code='AI_OUTPUT_INVALID' if result == 'FAILURE' else None,
                 properties={'input_token_count': value, 'output_token_count': value,
                             'token': 'secret', 'api_key': 'secret', 'content': 'private body',
                             'model_version': 'token=secret'})
    metadata = repository.insert.call_args.args[1]['metadata']
    assert metadata['input_token_count'] == value
    assert metadata['output_token_count'] == value
    assert not {'token', 'api_key', 'content', 'model_version'} & metadata.keys()


@pytest.mark.parametrize('value', [True, -1, 1.5, '38', 'token=secret', {'token': 'secret'}])
def test_real_audit_rejects_invalid_usage(value):
    repository = MagicMock()
    AuditService(repository, 'test').record(
        object(), event_name='assistant_generation_completed', user_id=7,
        object_type='ASSISTANT_DRAFT', result='SUCCESS', request_id='usage', duration_ms=0,
        properties={'input_token_count': value, 'output_token_count': value})
    metadata = repository.insert.call_args.args[1]['metadata']
    assert 'input_token_count' not in metadata
    assert 'output_token_count' not in metadata


def test_parser_failure_usage_reaches_real_audit_repository():
    provider = DeepSeekProposalAssistant(api_key='test', model='configured-model',
        transport=lambda **kwargs: response('{}', usage={'input_tokens': 1345, 'output_tokens': 38}))
    assistant = _mock_failure_service(provider)
    # Exercise real insert construction and PostgreSQL JSON serialization without
    # opening a connection or writing any database.
    audit_repository = AuditRepository.__new__(AuditRepository)
    audit_repository.table = sa.Table('audit_events', sa.MetaData(),
        sa.Column('id', sa.Uuid()), sa.Column('created_at', sa.DateTime(timezone=True)),
        sa.Column('actor_user_id', sa.Integer()), sa.Column('action', sa.String()),
        sa.Column('object_type', sa.String()), sa.Column('object_id', sa.String()),
        sa.Column('result', sa.String()), sa.Column('request_id', sa.String()),
        sa.Column('metadata', sa.JSON()))
    connection = assistant.repository.engine.begin.return_value.__enter__.return_value
    connection.dialect = postgresql.dialect()
    assistant.audit_service = AuditService(audit_repository, 'test')
    assert _generate_failure(assistant) == 'AI_OUTPUT_INVALID'
    statement = connection.execute.call_args.args[0]
    values = statement.compile(dialect=connection.dialect).params
    encode = audit_repository.table.c.metadata.type.bind_processor(connection.dialect)
    metadata = json.loads(encode(values['metadata']))
    assert metadata['input_token_count'] == 1345
    assert metadata['output_token_count'] == 38
