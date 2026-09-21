"""Progress editing through the existing lifecycle service and authenticated API."""
import pytest
import sqlalchemy as sa

from app.tests.test_project_lifecycle import lifecycle  # noqa: F401
from app.services.projects import ProjectServiceError


def payload(version=1, **changes):
    return {"recordedAt": "2026-09-10T09:00:00+08:00", "status": "RISK",
            "summary": "原记录：0.6℃，未通过。", "riskLevel": "HIGH",
            "issues": "尚无续航记录", "nextActions": "补充试验", "version": version, **changes}


def test_edit_preserves_creation_identity_updates_record_and_project(lifecycle):
    service, engine, audit, project_id = lifecycle
    original = service.add_progress(project_id, payload(), actor_user_id=7, request_id="create")
    stored_creation = service.list_progress(project_id)[0]["createdAt"]
    updated = service.update_progress(project_id, original["id"], payload(2, summary="改进：Ａ型²，0.3℃。\n仅三个点通过。"), actor_user_id=8, request_id="edit")
    assert updated["id"] == original["id"]
    assert updated["createdBy"] == 7
    assert updated["createdAt"] == stored_creation
    assert updated["updatedBy"] == 8
    assert updated["updatedAt"]
    assert updated["version"] == 2
    assert updated["projectVersion"] == 3
    assert updated["summary"] == "改进：Ａ型²，0.3℃。\n仅三个点通过。"
    rows = service.list_progress(project_id)
    assert len(rows) == 1
    assert rows[0]["summary"] == updated["summary"]
    assert rows[0]["updatedBy"] == 8
    assert audit.events[-1]["event_name"] == "project_record_updated"
    assert audit.events[-1]["object_type"] == "PROJECT"
    assert "summary" not in audit.events[-1]["properties"]


@pytest.mark.parametrize('changes', [
    {'summary': ''}, {'summary': ' \n\u200b'}, {'summary': 3}, {'summary': 'x' * 5001},
    {'status': 'DONE'}, {'status': []}, {'riskLevel': []}, {'recordedAt': 'bad'},
    {'recordedAt': '2026-09-10T10:00:00'}, {'issues': []}, {'nextActions': 'x' * 10001},
    {'version': True}, {'version': 2.5}, {'version': 0}, {'version': '2'},
    {'createdBy': 55}, {'project_registry_id': 'another-project'},
])
def test_edit_rejects_invalid_input_without_mutation(lifecycle, changes):
    service, _, audit, project_id = lifecycle
    original = service.add_progress(project_id, payload(), actor_user_id=7, request_id='create')
    before = service.list_progress(project_id)
    events = len(audit.events)
    with pytest.raises(ProjectServiceError) as error:
        service.update_progress(project_id, original['id'], payload(**{'version': 2, **changes}), actor_user_id=8, request_id='invalid')
    assert error.value.status_code == 422
    assert service.list_progress(project_id) == before
    assert service.get(project_id)['version'] == 2
    assert len(audit.events) == events


@pytest.mark.parametrize('progress_id', ['not-a-uuid', '00000000-0000-0000-0000-000000000000'])
def test_missing_progress_is_404(lifecycle, progress_id):
    service, _, _, project_id = lifecycle
    with pytest.raises(ProjectServiceError) as error:
        service.update_progress(project_id, progress_id, payload(), actor_user_id=8, request_id='missing')
    assert error.value.status_code == 404


def test_cross_project_progress_is_404_and_unchanged(lifecycle):
    import uuid
    service, engine, _, project_id = lifecycle
    original = service.add_progress(project_id, payload(), actor_user_id=7, request_id='create')
    table = service.repository.progress
    with engine.begin() as connection:
        connection.execute(table.update().where(table.c.id == original['id']).values(project_registry_id=str(uuid.uuid4())))
    with pytest.raises(ProjectServiceError) as error:
        service.update_progress(project_id, original['id'], payload(2), actor_user_id=8, request_id='cross')
    assert error.value.status_code == 404
    with engine.connect() as connection:
        row = connection.execute(sa.select(table)).mappings().one()
        assert row['summary'] == original['summary']
        assert row['version'] == 1


def test_stale_edit_does_not_overwrite_first_edit(lifecycle):
    service, _, _, project_id = lifecycle
    original = service.add_progress(project_id, payload(), actor_user_id=7, request_id='create')
    service.update_progress(project_id, original['id'], payload(2, summary='先保存'), actor_user_id=8, request_id='first')
    with pytest.raises(ProjectServiceError) as error:
        service.update_progress(project_id, original['id'], payload(2, summary='过期覆盖'), actor_user_id=7, request_id='stale')
    assert error.value.status_code == 409
    assert service.list_progress(project_id)[0]['summary'] == '先保存'
    assert service.get(project_id)['version'] == 3


@pytest.mark.parametrize('status', ['CLOSED', 'TERMINATED'])
def test_terminal_project_edit_rejected(lifecycle, status):
    service, engine, _, project_id = lifecycle
    original = service.add_progress(project_id, payload(), actor_user_id=7, request_id='create')
    with engine.begin() as connection:
        connection.execute(service.repository.registry.update().values(status=status))
    with pytest.raises(ProjectServiceError) as error:
        service.update_progress(project_id, original['id'], payload(2), actor_user_id=8, request_id='terminal')
    assert error.value.status_code == 409
    assert service.list_progress(project_id)[0]['version'] == 1


@pytest.fixture
def edit_app(lifecycle, tmp_path):
    from app import create_app
    service, _, _, project_id = lifecycle
    class Users:
        role = 'BUSINESS_USER'
        status = 'active'
        def get_by_id(self, user_id):
            return {'id': user_id, 'role': self.role, 'status': self.status, 'version': 1}
        def list_accounts(self):
            return [{'id': 7, 'name': '创建人'}, {'id': 8, 'name': '修改人'}]
    users = Users()
    app = create_app({'TESTING': True, 'SECRET_KEY': 'progress-edit-test-secret',
                      'DATA_DIR': str(tmp_path), 'SESSION_FILE_DIR': str(tmp_path / 'sessions'),
                      'PROJECT_SERVICE': service, 'SECURITY_AUTH_ENABLED': True,
                      'CSRF_ENABLED': True, 'AI_PROVIDER': 'DISABLED'})
    app.extensions['users_repository'] = users
    original = service.add_progress(project_id, payload(), actor_user_id=7, request_id='create')
    yield app, users, f'/api/projects/{project_id}/progress/{original["id"]}'


def client_for(app, role='BUSINESS_USER'):
    client = app.test_client()
    with client.session_transaction() as session:
        session.update(user_id=8, user='edit-test', role=role, account_version=1, csrf_token='csrf-edit-test')
    return client


@pytest.mark.parametrize('role', ['BUSINESS_USER', 'SYSTEM_MAINTAINER'])
def test_edit_api_authorized_csrf_and_readback(edit_app, role):
    app, users, url = edit_app
    users.role = role
    client = client_for(app, role)
    assert client.patch(url, json=payload(2)).status_code == 403
    response = client.patch(url, json=payload(2, summary='编辑后的0.6℃：仍未通过。'), headers={'X-CSRF-Token':'csrf-edit-test'})
    assert response.status_code == 200
    assert response.json['createdBy'] == 7
    assert response.json['updatedBy'] == 8
    assert response.json['projectVersion'] == 3
    result = client.get(url.rsplit('/', 1)[0]).json['data'][0]
    assert result['summary'] == '编辑后的0.6℃：仍未通过。'
    assert result['updatedByName'] == '修改人'


@pytest.mark.parametrize('identity', ['anonymous', 'disabled', 'invalid_role'])
def test_edit_api_rejects_invalid_identity(edit_app, identity):
    app, users, url = edit_app
    client = app.test_client() if identity == 'anonymous' else client_for(app)
    if identity == 'disabled': users.status = 'disabled'
    if identity == 'invalid_role': users.role = 'INVALID'
    assert client.patch(url, json=payload(2), headers={'X-CSRF-Token':'csrf-edit-test'}).status_code in (401, 403)


def test_edit_api_invalid_json(edit_app):
    app, _, url = edit_app
    client = client_for(app)
    assert client.patch(url, json=[], headers={'X-CSRF-Token':'csrf-edit-test'}).status_code == 422


@pytest.mark.parametrize('failure', ['project_version', 'audit'])
def test_edit_transaction_rolls_back_on_late_failure(lifecycle, monkeypatch, failure):
    from app.repositories.base import OptimisticLockConflict
    service, _, audit, project_id = lifecycle
    original = service.add_progress(project_id, payload(), actor_user_id=7, request_id='create')
    before = service.list_progress(project_id)
    def fail(*args, **kwargs):
        if failure == 'project_version':
            raise OptimisticLockConflict('injected conflicting project change')
        raise RuntimeError('injected audit unavailable')
    monkeypatch.setattr(service.repository if failure == 'project_version' else audit,
                        'transition_registry' if failure == 'project_version' else 'record', fail)
    with pytest.raises(ProjectServiceError if failure == 'project_version' else RuntimeError):
        service.update_progress(project_id, original['id'], payload(2, summary='不得残留'), actor_user_id=8, request_id='failure')
    assert service.list_progress(project_id) == before
    assert service.get(project_id)['version'] == 2


def test_edit_real_audit_service_records_update_not_addition(lifecycle):
    from app.repositories.audit import AuditRepository
    from app.services.audit import AuditService
    service, engine, _, project_id = lifecycle
    repository = AuditRepository(engine)
    service.audit_service = AuditService(repository, 'progress-edit-test')
    original = service.add_progress(project_id, payload(), actor_user_id=7, request_id='create')
    service.update_progress(project_id, original['id'], payload(2, summary='仅保存在业务记录的正文'), actor_user_id=8, request_id='edit')
    with engine.connect() as connection:
        rows = list(connection.execute(sa.select(repository.table).order_by(repository.table.c.created_at)).mappings())
    assert [row['action'] for row in rows] == ['project_record_added', 'project_record_updated']
    assert rows[1]['actor_user_id'] == 8
    assert rows[1]['object_type'] == 'PROJECT'
    assert rows[1]['object_id'] == 'KY-2026-001'
    assert rows[1]['metadata']['record_type'] == 'PROGRESS'
    assert '仅保存在业务记录的正文' not in str(rows[1])
