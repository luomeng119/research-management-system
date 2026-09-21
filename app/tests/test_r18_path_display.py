from types import SimpleNamespace
from copy import deepcopy
from app import create_app
from app.tests.test_project_lifecycle import lifecycle


def test_path_display_fields_preserve_trace_and_state(lifecycle, tmp_path):
    service, engine, _, project_id = lifecycle
    service.transition_status(project_id, {'toStatus':'ACTIVE','reason':'fixture','version':1}, actor_user_id=7, request_id='r18-start')
    service.add_progress(project_id, {'recordedAt':'2026-09-08T22:36:12+08:00','status':'NORMAL','summary':'真实既有进展','version':2}, actor_user_id=7, request_id='r18-progress')
    original = deepcopy(service.detail(project_id))
    app = create_app({'TESTING':True,'SECRET_KEY':'fixture','DATA_DIR':str(tmp_path),'SESSION_FILE_DIR':str(tmp_path/'sessions'),'PROJECT_SERVICE':service,'SECURITY_AUTH_ENABLED':False,'CSRF_ENABLED':False,'AI_PROVIDER':'DISABLED'})
    app.extensions['users_repository'] = SimpleNamespace(list_accounts=lambda:[{'id':7,'name':'张老师','username':'zhang'}])
    client=app.test_client()
    with client.session_transaction() as session:
        session.update(user_id=7,user='zhang',role='BUSINESS_USER',account_version=1)
    response=client.get(f'/api/projects/{project_id}/research-path')
    assert response.status_code == 200
    root=response.json['tree']['data']
    assert root['summaryDisplay']=='一般科研项目 · 执行中'
    assert root['summary']=='GENERAL_RESEARCH · ACTIVE'
    progress=response.json['tree']['children'][1]['children'][0]['data']
    assert progress['ownerDisplay']=='张老师'
    assert progress['owner']=='记录人账号 7'
    assert progress['periodDisplay']=='2026-09-08 22:36'
    assert progress['periodLabel']=='记录时间'
    assert progress['ownerLabel']=='记录人'
    assert progress['sourceDisplay']=='进展记录'
    assert progress['source'].startswith('进展记录 ')
    app.extensions.pop('users_repository')
    fallback=client.get(f'/api/projects/{project_id}/research-path').json['tree']['children'][1]['children'][0]['data']
    assert '账号7' in fallback['ownerDisplay']
    assert service.detail(project_id)==original


def test_source_proposal_display_uses_lookup_and_preserves_uuid(lifecycle, monkeypatch):
    service, engine, _, project_id = lifecycle
    source_id='c1ba1f49-9123-461f-b458-74d86821d101'
    with engine.begin() as connection:
        connection.execute(service.repository.registry.update().values(proposal_id=source_id))
    calls=[]
    def lookup(connection, proposal_id):
        calls.append(proposal_id)
        return {'business_id':'TP-EXISTING','title':'已有来源提案'}
    monkeypatch.setattr(service.repository,'get_proposal_by_id',lookup)
    data=service.research_path(project_id)['tree']['data']
    assert calls==[source_id]
    assert data['sourceDisplay']=='来源提案 TP-EXISTING · 已有来源提案'
    assert data['source']==f'来源提案 {source_id}'
    monkeypatch.setattr(service.repository,'get_proposal_by_id',lambda *_:None)
    assert service.research_path(project_id)['tree']['data']['sourceDisplay']=='来源提案（记录未找到）'


def test_repository_proposal_lookup_is_by_id(lifecycle):
    service, engine, _, _ = lifecycle
    from datetime import datetime, timezone
    source_id='c1ba1f49-9123-461f-b458-74d86821d101'
    now=datetime.now(timezone.utc)
    with engine.begin() as connection:
        connection.execute(service.repository.proposals.insert().values(id=source_id,business_id='TP-EXISTING',created_at=now,updated_at=now,version=1))
    with engine.connect() as connection:
        assert service.repository.get_proposal_by_id(connection,source_id)['business_id']=='TP-EXISTING'
        assert service.repository.get_proposal_by_id(connection,'c1ba1f49-9123-461f-b458-74d86821d102') is None
