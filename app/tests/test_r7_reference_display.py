from datetime import datetime, timezone
from io import BytesIO
from types import SimpleNamespace

from app.tests.test_reference_library import reference_engine, reference_service, reference_routes, _pdf, _seed_folder


def test_standard_list_resolves_user_and_preserves_unknown_and_missing_date(reference_routes, reference_service, monkeypatch):
    when = datetime(2026, 9, 8, 12, 34, 56, tzinfo=timezone.utc)
    rows = [dict(doc_id='one', name='标准', category='国家标准', file_type='TXT', uploader='10', upload_time=None, created_at=when),
            dict(doc_id='two', name='历史', category='国家标准', file_type='TXT', uploader='历史上传人', upload_time=None)]
    monkeypatch.setattr(reference_service.repository, 'list_standards', lambda **kw: rows)
    monkeypatch.setattr(reference_service, '_first_file', lambda *args: {'fileId':'file', 'versionNo':1})
    reference_routes.application.extensions['users_repository'] = SimpleNamespace(list_accounts=lambda: [
        dict(id=10, username='qa', name='张老师'), dict(id=11, username='10', name='数字用户名')])
    html = reference_routes.get('/standards/').get_data(as_text=True)
    assert '<td>张老师</td>' in html
    assert '<td>历史上传人</td>' in html
    assert '>2026-09-08 12:34</time>' in html
    assert '未记录' in html and '<td>None</td>' not in html
    assert rows[0]['uploader'] == '10' and rows[0]['upload_time'] is None


def test_template_tree_uses_current_controlled_version_size_and_preserves_download(reference_engine, reference_service):
    _seed_folder(reference_engine, 'folder', '方案模板')
    payload = _pdf(b'x' * 433).getvalue()
    reference_service.upload_template(BytesIO(payload), 'a.pdf', '方案模板', 'a.pdf', actor_user_id=7, request_id='size-test')
    file = reference_service.build_template_tree()[0]['files'][0]
    assert file['size'] == len(payload)
    opened = reference_service.open_template_download('方案模板/a.pdf')
    assert opened['stream'].read() == payload


def test_template_size_display_does_not_round_small_files_to_zero(reference_routes):
    import subprocess
    html = reference_routes.get('/templates/').get_data(as_text=True)
    function = html.split('function formatReferenceSize(', 1)[1].split('function renderTree(', 1)[0]
    script = 'function formatReferenceSize(' + function + '''
const assert = require('node:assert/strict');
assert.equal(formatReferenceSize(442), '442 B');
assert.equal(formatReferenceSize(0), '0 B');
assert.equal(formatReferenceSize(null), '未记录');
assert.equal(formatReferenceSize(1024), '1.0 KB');
'''
    subprocess.run(['node', '-e', script], check=True)
