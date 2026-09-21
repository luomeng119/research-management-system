import io

import pytest
import sqlalchemy as sa

from app.tests.test_proposals import engine, service, _create, _web_client
from app.repositories.files import FilesRepository
from app.services.files import FileService


@pytest.fixture
def attachments(tmp_path, engine, service):
    files = FileService(FilesRepository(engine), service.audit_service,
                        storage_root=tmp_path / 'attachments', max_bytes=1024, preview_max_bytes=512)
    client = _web_client(tmp_path, engine, service, file_service=files)
    proposal = _create(service)['businessId']
    return client, proposal


def test_history_keeps_independent_names_and_downloads_every_version(attachments):
    client, proposal = attachments
    query = {'objectType': 'PROPOSAL', 'objectId': proposal}
    def upload(payload):
        return client.post('/api/files', data={**query, 'file': (io.BytesIO(payload), '演练-版本验证.txt')}).get_json()
    first, other = upload(b'original'), upload(b'independent')
    assert first['fileId'] != other['fileId']
    url = f"/proposals/{proposal}/attachments/{first['fileId']}/versions"
    response = client.post(url, data={'expectedVersion': '1', 'file': (io.BytesIO(b'revised'), '演练-版本验证.txt')})
    assert response.status_code == 302
    history = client.get(f"/api/files/{first['fileId']}/versions", query_string=query).get_json()
    assert [v['versionNo'] for v in history['versions']] == [2, 1]
    assert all(v['createdAt'] and v['sizeBytes'] > 0 for v in history['versions'])
    assert 'storagePath' not in str(history)
    for file, version, payload in [(first, 1, b'original'), (first, 2, b'revised'), (other, 1, b'independent')]:
        downloaded = client.get(f"/api/files/{file['fileId']}/versions/{version}/download", query_string=query)
        assert downloaded.data == payload
    html = client.get(f'/proposals/{proposal}').get_data(as_text=True)
    assert first['fileId'] in html and other['fileId'] in html
    assert '版本历史' in html and '上传此文件新版本' in html
    assert '当前 v2' in html and '当前 v1' in html
    stale = client.post(url, data={'expectedVersion': '1', 'file': (io.BytesIO(b'stale'), '演练-版本验证.txt')})
    assert stale.status_code == 409


def test_history_object_binding_and_anonymous_rejection(attachments, service):
    client, proposal = attachments
    result = client.post('/api/files', data={'objectType': 'PROPOSAL', 'objectId': proposal,
                        'file': (io.BytesIO(b'private'), '演练-版本验证.txt')}).get_json()
    url = f"/api/files/{result['fileId']}/versions"
    wrong = _create(service)['businessId']
    assert client.get(url, query_string={'objectType': 'PROPOSAL', 'objectId': wrong}).status_code == 404
    with client.session_transaction() as session:
        session.clear()
    client.application.config['SECURITY_AUTH_ENABLED'] = True
    assert client.get(url, query_string={'objectType': 'PROPOSAL', 'objectId': proposal}).status_code == 401


def test_version_page_rejects_wrong_object_terminal_and_missing_csrf(attachments, service, engine):
    client, proposal = attachments
    query = {'objectType': 'PROPOSAL', 'objectId': proposal}
    first = client.post('/api/files', data={**query, 'file': (io.BytesIO(b'original'), '演练.txt')}).get_json()
    file_id = first['fileId']
    def submit(object_id, expected=1):
        return client.post(f'/proposals/{object_id}/attachments/{file_id}/versions',
                           data={'expectedVersion': str(expected), 'file': (io.BytesIO(b'revised'), '演练.txt')})
    wrong = _create(service)['businessId']
    assert submit(wrong).status_code == 404
    assert submit('TP-NOT-FOUND').status_code == 404
    with client.session_transaction() as session:
        session['role'] = 'SYSTEM_MAINTAINER'
    assert submit(proposal).status_code == 302
    with client.session_transaction() as session:
        session['role'] = 'BUSINESS_USER'
    proposals = sa.Table('proposals', sa.MetaData(), autoload_with=engine)
    with engine.begin() as connection:
        connection.execute(proposals.update().where(proposals.c.business_id == proposal).values(status='REJECTED'))
    assert submit(proposal, expected=2).status_code == 409
    history = client.get(f'/api/files/{file_id}/versions', query_string=query).get_json()
    assert [v['versionNo'] for v in history['versions']] == [2,1]
    assert client.get(f'/api/files/{file_id}/versions/1/download', query_string=query).data == b'original'
    client.application.config['CSRF_ENABLED'] = True
    assert submit(proposal).status_code == 403
