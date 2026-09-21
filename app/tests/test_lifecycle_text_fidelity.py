import pytest

from app.services.projects import ProjectService, ProjectServiceError
from app.tests.test_project_lifecycle import lifecycle


TEXT = '演练：x² 与 x₂，面积10 m²；剂量5 µg，μ不替换。\n保留Ａ１①㎏与\t制表符\u200d连接符'
PADDED = '\x00\u200b\u3000' + TEXT + '\u3000\ufeff\x00'


@pytest.mark.parametrize('kind,fields', [
    ('progress', ['summary', 'issues', 'nextActions']),
    ('change', ['beforeSummary', 'afterSummary', 'basis']),
    ('output', ['title', 'description', 'contributors']),
    ('closure', ['summary', 'remainingIssues', 'noOutputReason']),
])
def test_process_save_readback_preserves_free_text(lifecycle, kind, fields):
    service, _, _, registry_id = lifecycle
    payloads = {
        'progress': {'status': 'NORMAL', 'recordedAt': '2026-09-10T09:00:00+08:00'},
        'change': {'changeType': ' ＰＥＲＩＯＤ ', 'decision': 'AGREED', 'decisionDate': '2026-09-10'},
        'output': {'outputType': ' ＲＥＰＯＲＴ ', 'formedDate': '2026-09-10'},
        'closure': {'closedAt': '2026-09-10T09:00:00+08:00', 'conclusion': 'PASS'},
    }
    version = 1
    if kind == 'closure':
        for target in ['ACTIVE', 'CLOSING']:
            service.transition_status(registry_id, {'toStatus': target, 'reason': PADDED, 'version': version}, actor_user_id=7, request_id='fidelity-transition')
            version += 1
        assert service.list_changes(registry_id)[0]['basis'] == TEXT
    payload = {**payloads[kind], **dict.fromkeys(fields, PADDED), 'version': version}
    write = service.close_project if kind == 'closure' else getattr(service, 'add_' + kind)
    created = write(registry_id, payload, actor_user_id=7, request_id='fidelity-write')
    readers = {'progress': service.list_progress, 'change': service.list_changes, 'output': service.list_outputs}
    readback = service.get_closure(registry_id) if kind == 'closure' else readers[kind](registry_id)[0]
    for field in fields:
        assert created[field] == TEXT
        assert readback[field] == TEXT
    if kind == 'change':
        assert readback['changeType'] == 'PERIOD'
    if kind == 'output':
        assert readback['outputType'] == 'REPORT'


@pytest.mark.parametrize('value', ['\x00\u200b', '\ufe0f', None, 123, 'x' * 5001])
def test_required_process_text_validation_remains(value):
    with pytest.raises(ProjectServiceError) as error:
        ProjectService._required_text({'summary': value}, 'summary')
    assert error.value.code == 'VALIDATION_ERROR'


def test_optional_process_text_keeps_empty_and_length_rules():
    assert ProjectService._optional_text({'issues': '\u200b\x00 '}, 'issues') is None
    with pytest.raises(ProjectServiceError):
        ProjectService._optional_text({'issues': 'x' * 5001}, 'issues')
