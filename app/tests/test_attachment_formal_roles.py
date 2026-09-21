"""Attachment access follows both formal roles and existing identity/object guards."""
import pytest
from flask import jsonify

from app.tests.test_file_service import engine, service, web_app, _business_client, _pdf
from app.security.csrf import protect_request
from app.services.projects import ProjectServiceError
from app.services.files import FileServiceError


class Users:
    role='BUSINESS_USER'
    status='active'
    version=1
    def get_by_id(self, user_id):
        return dict(id=user_id,role=self.role,status=self.status,version=self.version)


@pytest.fixture
def secured(web_app):
    users=Users()
    web_app.config.update(SECURITY_AUTH_ENABLED=True,CSRF_ENABLED=False)
    web_app.extensions['users_repository']=users
    web_app.before_request(protect_request)
    return web_app,users


@pytest.mark.parametrize('role',['BUSINESS_USER','SYSTEM_MAINTAINER'])
def test_both_formal_roles_can_round_trip_attachments_without_bypassing_owner(secured,role):
    app,users=secured;users.role=role
    client=_business_client(app,role)
    response=client.post('/api/files',data={'objectType':'EXPENSE','objectId':'7','file':(_pdf(b'roles'),'roles.pdf')})
    assert response.status_code==201
    file_id=response.get_json()['fileId']
    assert client.get('/api/files?objectType=EXPENSE&objectId=7').status_code==200
    url=f'/api/files/{file_id}/versions/1/download'
    assert client.get(url+'?objectType=EXPENSE&objectId=7').data.endswith(b'roles')
    assert client.get(url+'?objectType=EXPENSE&objectId=8').status_code==404
    assert client.get(url+'?objectType=STANDARD&objectId=7').status_code==404
    assert client.post(f'/api/files/{file_id}/versions',data={'objectType':'EXPENSE','objectId':'7','expectedVersion':'1','file':(_pdf(b'v2'),'roles.pdf')}).status_code==201
    versions=client.get(f'/api/files/{file_id}/versions?objectType=EXPENSE&objectId=7').get_json()['versions']
    assert [version['versionNo'] for version in versions]==[2,1]


@pytest.mark.parametrize('condition',['anonymous','invalid_role','disabled','stale_version'])
def test_invalid_or_inactive_identity_cannot_access_files(secured,condition):
    app,users=secured
    client=app.test_client() if condition=='anonymous' else _business_client(app)
    if condition=='invalid_role':
        with client.session_transaction() as session:session['role']='NOT_A_FORMAL_ROLE'
    elif condition=='disabled':users.status='disabled'
    elif condition=='stale_version':users.version=2
    assert client.get('/api/files?objectType=EXPENSE&objectId=7').status_code==401
    assert client.post('/api/files',data={'objectType':'EXPENSE','objectId':'7','file':(_pdf(),'x.pdf')}).status_code==401


@pytest.mark.parametrize('role',['BUSINESS_USER','SYSTEM_MAINTAINER'])
def test_attachment_write_still_requires_csrf(secured,role):
    app,users=secured;users.role=role;app.config['CSRF_ENABLED']=True
    client=_business_client(app,role)
    with client.session_transaction() as session:session['csrf_token']='known-test-token'
    def upload(token=None):
        return client.post('/api/files',data={'objectType':'EXPENSE','objectId':'7','file':(_pdf(),'x.pdf')},headers={'X-CSRF-Token':token} if token else {})
    assert upload().status_code==403
    assert upload('wrong').status_code==403
    assert upload('known-test-token').status_code==201


BRIDGE_OPERATIONS=['download','archive','delete_folder','delete_file','rename','upload','upload_folder']


@pytest.fixture
def bridge_app(secured):
    from app.routes import _project_bridge as bridge
    app,users=secured
    checked=[]
    class Projects:
        def get_legacy(self,**kwargs):
            checked.append(kwargs)
            raise ProjectServiceError('PROJECT_NOT_FOUND','项目不存在',404)
    app.extensions['project_service']=Projects()
    functions={
        'download':lambda:bridge.controlled_project_download('KY-WRONG','GENERAL','技术文件/x.txt'),
        'archive':lambda:bridge.controlled_project_archive('KY-WRONG','GENERAL'),
        'delete_folder':lambda:bridge.delete_project_folder('KY-WRONG','GENERAL'),
        'delete_file':lambda:bridge.delete_controlled_project_file('KY-WRONG','GENERAL'),
        'rename':lambda:bridge.rename_controlled_project_file('KY-WRONG','GENERAL'),
        'upload':lambda:bridge.upload_project_file('KY-WRONG','GENERAL'),
        'upload_folder':lambda:bridge.upload_project_folder('KY-WRONG','GENERAL'),
    }
    app.add_url_rule('/api/test-project/<operation>','project_guard',lambda operation:functions[operation](),methods=['POST'])
    return app,users,checked


@pytest.mark.parametrize('role',['BUSINESS_USER','SYSTEM_MAINTAINER'])
@pytest.mark.parametrize('operation',BRIDGE_OPERATIONS)
def test_every_project_attachment_guard_reaches_category_object_check(bridge_app,role,operation):
    app,users,checked=bridge_app;users.role=role
    response=_business_client(app,role).post('/api/test-project/'+operation,data={'folder_path':'技术文件'})
    assert response.status_code==404
    assert checked==[dict(category='GENERAL',business_id='KY-WRONG')]


@pytest.mark.parametrize('condition',['anonymous','invalid_role','disabled'])
@pytest.mark.parametrize('operation',BRIDGE_OPERATIONS)
def test_project_attachment_guards_reject_invalid_identity_before_object_access(bridge_app,condition,operation):
    app,users,checked=bridge_app
    client=app.test_client() if condition=='anonymous' else _business_client(app)
    if condition=='invalid_role':
        with client.session_transaction() as session:session['role']='INVALID'
    elif condition=='disabled':users.status='disabled'
    assert client.post('/api/test-project/'+operation,data={'folder_path':'技术文件'}).status_code in {401,403}
    assert checked==[]


@pytest.mark.parametrize('role',['BUSINESS_USER','SYSTEM_MAINTAINER'])
def test_equipment_file_guard_accepts_active_formal_roles(secured,role):
    from app.routes.equipment import _business_identity,_can_access_equipment_files
    app,users=secured;users.role=role
    app.add_url_rule('/api/equipment-guard','equipment_guard',lambda:jsonify(userId=_business_identity().user_id,visible=_can_access_equipment_files()))
    result=_business_client(app,role).get('/api/equipment-guard')
    assert result.status_code==200 and result.get_json()['visible'] is True


@pytest.mark.parametrize('condition',['anonymous','invalid_role','disabled'])
def test_equipment_file_guard_rejects_invalid_identity(secured,condition):
    from app.routes.equipment import _business_identity,_can_access_equipment_files
    app,users=secured
    def check():
        assert not _can_access_equipment_files()
        try:_business_identity()
        except FileServiceError as error:return jsonify(code=error.code),error.status_code
        raise AssertionError('invalid identity authorized')
    app.add_url_rule('/api/equipment-guard','equipment_guard',check)
    client=app.test_client() if condition=='anonymous' else _business_client(app)
    if condition=='invalid_role':
        with client.session_transaction() as session:session['role']='INVALID'
    elif condition=='disabled':users.status='disabled'
    assert client.get('/api/equipment-guard').status_code==401


def test_single_file_project_endpoint_rejects_multiple_files_without_partial_write(secured,monkeypatch):
    from app.routes._project_bridge import upload_project_file
    app,users=secured;users.role='SYSTEM_MAINTAINER';writes=[]
    class Projects:
        def get_legacy(self,**kwargs):return {'project_id':'KY-TEST'}
    app.extensions['project_service']=Projects()
    monkeypatch.setattr(app.extensions['file_service'],'upload_project_path',lambda *args,**kwargs:writes.append(kwargs) or dict(fileId='recorded',versionNo=1))
    app.add_url_rule('/api/test-project-upload','project_upload',lambda:upload_project_file('KY-TEST','GENERAL'),methods=['POST'])
    response=_business_client(app,'SYSTEM_MAINTAINER').post('/api/test-project-upload',data={'file':[(_pdf(b'first'),'first.pdf'),(_pdf(b'second'),'second.pdf')]})
    assert response.status_code==400
    assert response.get_json()['code']=='MULTIPLE_FILES_NOT_ALLOWED'
    assert writes==[]
