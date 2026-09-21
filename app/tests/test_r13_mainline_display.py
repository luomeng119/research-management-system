from types import SimpleNamespace
from contextlib import nullcontext
import re

import pytest
import sqlalchemy as sa

from app import create_app
from app.services.projects import ProjectServiceError
from app.tests.test_proposals import engine, service, _create, _web_client
from app.tests.test_project_lifecycle import lifecycle


@pytest.mark.parametrize('failure,category,status',[(RuntimeError('/private/failure'),'CRYPTO_APPLICATION',500),
    (ProjectServiceError('VALIDATION_ERROR','计划日期无效',422),'SECURITY_CONFIDENTIALITY',422)])
def test_processing_failure_keeps_all_fields_and_idempotency(tmp_path,engine,service,failure,category,status):
    proposal=_create(service)
    client=_web_client(tmp_path,engine,service)
    business_id=proposal['businessId']
    client.post(f'/proposals/{business_id}/argumentations',data={'version':'1','summary':'演练论证',
        'argumentationDate':'2026-09-09','conclusion':'可处理','basis':'演练依据'})
    def fail(*args,**kwargs):
        raise failure
    client.application.extensions['project_service']=SimpleNamespace(establish_from_proposal=fail)
    fields={'decision':'ESTABLISH','decisionDate':'2026-09-09','conclusion':'演练结论','basis':'演练处理依据',
            'projectCategory':category,'projectName':'演练项目','projectLeader':'李老师','plannedEndDate':'2026-12-01',
            'version':'2','idempotencyKey':'keep-this-request-key'}
    before=service.get(business_id)
    response=client.post(f'/proposals/{business_id}/decisions',data=fields)
    assert response.status_code==status
    html=response.get_data(as_text=True)
    assert '未找到科研提案' not in html and '/private/failure' not in html
    assert '论证处理' in html
    for field in ['decisionDate','conclusion','basis','projectName','projectLeader','plannedEndDate','idempotencyKey']:
        assert f'value="{fields[field]}"' in html
    assert re.search(rf'<option value="{category}"\s+selected>',html)
    assert service.get(business_id)==before
    assert service.list_decisions(business_id)['total']==0


def test_established_proposal_links_its_existing_registry(tmp_path,engine,service):
    proposal=_create(service)
    table=sa.Table('proposals',sa.MetaData(),autoload_with=engine)
    with engine.begin() as connection:
        connection.execute(table.update().where(table.c.id==proposal['id']).values(status='ESTABLISHED'))
    calls=[]
    def reference(connection, proposal_id):
        calls.append(proposal_id)
        return {'id':'registry-123','business_id':'KY-EXISTING'}, {'name':'已关联项目'}
    client=_web_client(tmp_path,engine,service)
    client.application.extensions['project_service']=SimpleNamespace(repository=SimpleNamespace(
        engine=SimpleNamespace(connect=lambda:nullcontext(None)),get_project_ref_by_proposal=reference))
    html=client.get(f"/proposals/{proposal['businessId']}").get_data(as_text=True)
    assert '/projects/registry-123/overview' in html and '查看已立项项目' in html
    assert '已关联项目' in html and calls==[proposal['id']]


def test_closure_displays_existing_no_output_reason_only(lifecycle,tmp_path,monkeypatch):
    service,_,_,project_id=lifecycle
    detail=service.detail(project_id)
    detail['closure']={'conclusion':'PASS','closedAt':'2026-09-09T10:00:00+08:00','summary':'演练完成',
        'remainingIssues':None,'noOutputReason':'演练：仅验证流程，未形成科研成果。'}
    monkeypatch.setattr(service,'detail',lambda _:detail)
    app=create_app({'TESTING':True,'SECRET_KEY':'test','DATA_DIR':str(tmp_path),'SESSION_FILE_DIR':str(tmp_path/'sessions'),
                    'PROJECT_SERVICE':service,'SECURITY_AUTH_ENABLED':False,'CSRF_ENABLED':False,'AI_PROVIDER':'DISABLED'})
    client=app.test_client()
    with client.session_transaction() as session:
        session.update(user_id=7,user='zhang',name='张老师',role='BUSINESS_USER',account_version=1)
    html=client.get(f'/projects/{project_id}/overview').get_data(as_text=True)
    assert '<dt>无成果原因</dt><dd>演练：仅验证流程，未形成科研成果。</dd>' in html
    detail['closure']['noOutputReason']=None
    html=client.get(f'/projects/{project_id}/overview').get_data(as_text=True)
    assert '<dt>无成果原因</dt>' not in html
