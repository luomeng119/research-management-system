from datetime import date
from copy import deepcopy
import pytest
from app import create_app
from app.tests.test_project_lifecycle import lifecycle


@pytest.mark.parametrize('linked', [True, False])
def test_overview_existing_dates_and_source(lifecycle, tmp_path, monkeypatch, linked):
    service, engine, _, project_id=lifecycle
    source_id='c1ba1f49-9123-461f-b458-74d86821d101'
    with engine.begin() as connection:
        connection.execute(service.repository.registry.update().values(proposal_id=source_id if linked else None))
        connection.execute(service.repository.category_tables['GENERAL_RESEARCH'].update().values(start_date=date(2026,9,8) if linked else None,planned_end_date=date(2026,9,28) if linked else None))
    monkeypatch.setattr(service.repository,'get_proposal_by_id',lambda connection,id:{'business_id':'TP-EXISTING','title':'原始来源提案'} if id==source_id else None)
    before=deepcopy(service.detail(project_id))
    app=create_app({'TESTING':True,'SECRET_KEY':'fixture','DATA_DIR':str(tmp_path),'SESSION_FILE_DIR':str(tmp_path/'sessions'),'PROJECT_SERVICE':service,'SECURITY_AUTH_ENABLED':False,'CSRF_ENABLED':False,'AI_PROVIDER':'DISABLED'})
    client=app.test_client()
    with client.session_transaction() as session:
        session.update(user_id=7,user='zhang',role='BUSINESS_USER',account_version=1)
    html=client.get(f'/projects/{project_id}/overview').get_data(as_text=True)
    if linked:
        assert '开始日期：2026-09-08' in html and '计划结束：2026-09-28' in html
        assert 'href="/proposals/TP-EXISTING"' in html and 'TP-EXISTING · 原始来源提案' in html
        monkeypatch.setattr(service.repository,'get_proposal_by_id',lambda *_:None)
        missing=client.get(f'/projects/{project_id}/overview').get_data(as_text=True)
        assert f'<span title="{source_id}">记录未找到</span>' in missing
    else:
        assert '开始日期：未填写' in html and '计划结束：未填写' in html
        assert '来源提案：无来源' in html
    assert service.detail(project_id)==before
    assert '计划开始' not in html
