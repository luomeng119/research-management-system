import json
import pytest
from flask import Flask
from jinja2 import ChoiceLoader,DictLoader
from app.security.csrf import csrf_token,protect_request
from app.tests.test_sensitive_term_sets import vocabulary,payload


@pytest.fixture
def web(vocabulary):
    from app.web.sensitive_terms import bp
    service,_,_=vocabulary
    app=Flask('app',template_folder='templates')
    app.config.update(TESTING=True,SECRET_KEY='terms-web-test',CSRF_ENABLED=True)
    app.jinja_loader=ChoiceLoader([DictLoader({'base.html':'{% block content %}{% endblock %}'}),app.jinja_loader])
    app.jinja_env.globals['csrf_token']=csrf_token
    app.before_request(protect_request)
    app.add_url_rule('/login',endpoint='auth.login',view_func=lambda:'login')
    app.register_blueprint(bp)
    app.extensions['sensitive_term_set_service']=service
    client=app.test_client()
    with client.session_transaction() as session:session.update(user_id=1,user='maintainer',role='SYSTEM_MAINTAINER',account_version=1,csrf_token='token')
    return client,service


def form(**changes):
    data=dict(_csrf_token='token',action='preview',format='json',expectedVersion='0',rulesJson=json.dumps(payload()['rules'],ensure_ascii=False),note='维护备注')
    data.update(changes)
    return data


def test_preview_and_cancel_never_write(web):
    client,service=web
    response=client.post('/admin/sensitive-terms',data=form())
    assert response.status_code==200
    assert '确认保存新版本' in response.get_data(as_text=True)
    assert service.current()['version']==0
    assert client.get('/admin/sensitive-terms').status_code==200
    assert service.current()['version']==0


def test_manage_page_has_explicit_add_rule_button(web):
    client,_=web
    text=client.get('/admin/sensitive-terms').get_data(as_text=True)
    assert 'id="add-sensitive-rule"' in text
    assert '添加规则' in text
    assert 'id="sensitive-rule-rows"' in text


def test_confirm_persists_and_history_readonly(web):
    client,service=web
    response=client.post('/admin/sensitive-terms',data=form(action='save'))
    assert response.status_code==302
    assert service.current()['rules'][0]['source']=='青岚-07'
    history=client.get('/admin/sensitive-terms/versions/1')
    assert history.status_code==200
    assert '青岚-07' in history.get_data(as_text=True)
    assert '确认保存新版本' not in history.get_data(as_text=True)


def test_business_and_anonymous_cannot_read_originals(web):
    client,service=web
    client.post('/admin/sensitive-terms',data=form(action='save'))
    with client.session_transaction() as session:session['role']='BUSINESS_USER';session['user_id']=2
    for path in ['/admin/sensitive-terms','/admin/sensitive-terms/versions/1']:
        response=client.get(path)
        assert response.status_code==403
        assert '青岚' not in response.get_data(as_text=True)
    assert client.post('/admin/sensitive-terms',data=form()).status_code==403
    with client.session_transaction() as session:session.clear()
    assert client.get('/admin/sensitive-terms').status_code==302


def test_csrf_and_invalid_json_preserve_input(web):
    client,service=web
    assert client.post('/admin/sensitive-terms',data=form(_csrf_token='wrong')).status_code==403
    response=client.post('/admin/sensitive-terms',data=form(rulesJson='[ malformed <script>'))
    assert response.status_code==422
    assert '[ malformed &lt;script&gt;' in response.get_data(as_text=True)
    assert service.current()['version']==0


def test_conflict_preserves_unsaved_input(web):
    client,service=web
    client.post('/admin/sensitive-terms',data=form(action='save'))
    data=form(action='save',note='未保存的备注')
    response=client.post('/admin/sensitive-terms',data=data)
    assert response.status_code==409
    text=response.get_data(as_text=True)
    assert '未保存的备注' in text and 'value="0"' in text
    assert service.current()['version']==1


def test_row_editor_can_disable_and_add_rule(web):
    client,service=web
    data=dict(_csrf_token='token',action='save',format='rows',expectedVersion='0',note='',ruleId=['project','unit',''],source=['青岚','甲分队',''],replacement=['QL','[UNIT]',''],enabled=['false','true','true'])
    assert client.post('/admin/sensitive-terms',data=data).status_code==302
    rules=service.current()['rules']
    assert len(rules)==2 and rules[0]['enabled'] is False


def test_service_unavailable_keeps_other_routes(web):
    client,_=web
    client.application.extensions.pop('sensitive_term_set_service')
    assert client.get('/admin/sensitive-terms').status_code==503
    assert client.get('/login').status_code==200


def test_json_file_import_preview_is_readonly(web):
    from io import BytesIO
    client,service=web
    data=form()
    data['rulesFile']=(BytesIO(json.dumps(payload()['rules'],ensure_ascii=False).encode('utf-8')),'terms.json')
    response=client.post('/admin/sensitive-terms',data=data,content_type='multipart/form-data')
    assert response.status_code==200
    assert '青岚-07' in response.get_data(as_text=True)
    assert service.current()['version']==0


def test_storage_error_after_form_preserves_input(web):
    from sqlalchemy.exc import OperationalError
    client,service=web
    def fail(*args,**kwargs):raise OperationalError('hidden',{},Exception('storage down'))
    service.save_version=fail
    response=client.post('/admin/sensitive-terms',data=form(action='save',note='我的未保存备注'))
    assert response.status_code==503
    assert '我的未保存备注' in response.get_data(as_text=True)
    assert 'storage down' not in response.get_data(as_text=True)
