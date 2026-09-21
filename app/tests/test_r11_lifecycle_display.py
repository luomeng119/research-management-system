from types import SimpleNamespace
from copy import deepcopy

from app import create_app
from app.tests.test_project_lifecycle import lifecycle


def test_lifecycle_names_dates_and_pagination_preserve_raw_records(lifecycle, tmp_path):
    service, _, _, project_id = lifecycle
    service.transition_status(project_id, {'toStatus':'ACTIVE','reason':'演练','version':1}, actor_user_id=7, request_id='test-start')
    for index, actor in enumerate([7,8,9,999], 2):
        service.add_progress(project_id, {'recordedAt':'2026-09-08T22:36:12+08:00','status':'NORMAL',
            'summary':f'演练{actor}','version':index}, actor_user_id=actor, request_id=f'test-{actor}')
    original = deepcopy(service.detail(project_id)['progress'])
    app = create_app({'TESTING':True,'SECRET_KEY':'test','DATA_DIR':str(tmp_path),'SESSION_FILE_DIR':str(tmp_path/'sessions'),
                      'PROJECT_SERVICE':service,'SECURITY_AUTH_ENABLED':False,'CSRF_ENABLED':False,'AI_PROVIDER':'DISABLED'})
    calls = []
    def accounts():
        calls.append(True)
        return [{'id':7,'name':'张老师','username':'zhang'},{'id':8,'name':'李老师','username':'7'},
                {'id':9,'name':'王老师','username':'wang'}]
    app.extensions['users_repository'] = SimpleNamespace(list_accounts=accounts)
    client = app.test_client()
    with client.session_transaction() as session:
        session.update(user_id=7,user='zhang',name='张老师',role='BUSINESS_USER',account_version=1)
    html = client.get(f'/projects/{project_id}/overview').get_data(as_text=True)
    for name in ['张老师','李老师','王老师','未知用户（账号999）']:
        assert '记录人 '+name in html
    raw = original[0]['recordedAt']
    assert f'<time title="{raw}">{raw.replace("T", " ")[:16]}</time>' in html
    assert len(calls) == 1
    assert 'title="FILED">备案</span>' in html
    assert 'title="STATUS_TRANSITION">状态变更</span>' in html
    assert '<h3 title="ACTIVE">执行中</h3>' in html
    assert '<span title="PENDING">待启动</span>' in html
    response = client.get(f'/api/projects/{project_id}/progress?page=1&pageSize=2')
    assert response.status_code == 200
    for item in response.json['data']:
        prior = next(row for row in original if row['id']==item['id'])
        assert item['createdBy']==prior['createdBy'] and item['recordedAt']==prior['recordedAt']
        assert item['createdByName']
    assert len(calls)==2
    assert service.detail(project_id)['progress']==original


def test_status_summary_mapping_is_limited_to_transition_records(lifecycle, tmp_path, monkeypatch):
    service, _, _, project_id = lifecycle
    detail = service.detail(project_id)
    detail['changes'] = [dict(changeType=kind, afterSummary='ACTIVE', beforeSummary='PENDING', basis='原文',
                              decision='FILED', decisionDate='2026-09-08', createdAt=None) for kind in ['OTHER','STATUS_TRANSITION']]
    detail['changes'].append({**detail['changes'][1], 'afterSummary':'UNKNOWN_STATE', 'beforeSummary':'自定义历史状态'})
    monkeypatch.setattr(service,'detail',lambda _:detail)
    app=create_app({'TESTING':True,'SECRET_KEY':'test','DATA_DIR':str(tmp_path),'SESSION_FILE_DIR':str(tmp_path/'sessions'),
                    'PROJECT_SERVICE':service,'SECURITY_AUTH_ENABLED':False,'CSRF_ENABLED':False,'AI_PROVIDER':'DISABLED'})
    client=app.test_client()
    with client.session_transaction() as session:
        session.update(user_id=7,user='zhang',name='张老师',role='BUSINESS_USER',account_version=1)
    html=client.get(f'/projects/{project_id}/overview').get_data(as_text=True)
    assert '<h3>ACTIVE</h3>' in html and '<span>PENDING</span>' in html
    assert '<h3 title="ACTIVE">执行中</h3>' in html
    assert '<h3 title="UNKNOWN_STATE">UNKNOWN_STATE</h3>' in html
    assert '<span title="自定义历史状态">自定义历史状态</span>' in html
    assert detail['changes'][0]['afterSummary']=='ACTIVE'
