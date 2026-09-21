"""Vocabulary maintenance requires the existing active SYSTEM_MAINTAINER role.

Read methods serve trusted application code. Web callers must still use existing
login/maintenance decorators; reading a snapshot is not a grant to expose original
terms to business-user pages. Active rules alone feed the replacement engine.
"""
from datetime import datetime,timezone

from app.security.auth import SYSTEM_MAINTAINER,normalize_role
from app.services.sensitive_terms import compile_rules,SensitiveTermError,is_supported_text


class SensitiveTermSetError(RuntimeError):
    def __init__(self,code,message,status_code=400,*,fields=None):
        super().__init__(message)
        self.code,self.message,self.status_code=code,message,status_code
        self.fields=fields or {}


def _snapshot(row):
    names={'version_no':'version','created_by':'createdBy','created_at':'createdAt','created_by_name':'createdByName'}
    return {names.get(key,key):value.isoformat() if isinstance(value,datetime) else value for key,value in row.items()}


def _empty():
    return dict(version=0,rules=[],note='',createdBy=None,createdByName=None,createdAt=None)


def _validated_rules(value):
    if not isinstance(value,list) or len(value)>1000:
        raise SensitiveTermSetError('VALIDATION_ERROR','词库必须是最多1000条的映射列表',422)
    copied=[]
    ids=set()
    sources=set()
    for item in value:
        if not isinstance(item,dict) or type(item.get('enabled')) is not bool:
            raise SensitiveTermSetError('VALIDATION_ERROR','词库规则必须包含有效启用状态',422)
        rule={key:item.get(key) for key in ('id','source','replacement')}
        try:
            compile_rules([rule])
        except SensitiveTermError as error:
            raise SensitiveTermSetError('VALIDATION_ERROR',str(error),422) from error
        if rule['id'] in ids or rule['source'] in sources:
            raise SensitiveTermSetError('VALIDATION_ERROR','词库编号或原词重复，请先修正',422)
        ids.add(rule['id'])
        sources.add(rule['source'])
        copied.append(dict(**rule,enabled=item['enabled']))
    try:
        compile_rules([{k:v for k,v in rule.items() if k!='enabled'} for rule in copied if rule['enabled']])
    except SensitiveTermError as error:
        raise SensitiveTermSetError('VALIDATION_ERROR',str(error),422) from error
    return copied


class SensitiveTermSetService:
    def __init__(self,repository,audit_service):
        self.repository,self.audit_service=repository,audit_service

    def _maintainer(self,connection,actor_id):
        if type(actor_id) is not int or actor_id<=0:
            raise SensitiveTermSetError('AUTHENTICATION_REQUIRED','请登录后操作',401)
        user=self.repository.actor(connection,actor_id)
        if user is None:
            raise SensitiveTermSetError('AUTHENTICATION_REQUIRED','请登录后操作',401)
        try:role=normalize_role(user['role'])
        except ValueError:role=None
        if user['status']!='active' or role!=SYSTEM_MAINTAINER:
            raise SensitiveTermSetError('FORBIDDEN','需要有效的系统维护权限',403)

    def current(self):
        with self.repository.engine.connect() as connection:
            version=self.repository.current_number(connection)
            if version is None:
                raise SensitiveTermSetError('VOCABULARY_UNAVAILABLE','词库服务尚未就绪',503)
            if version==0:return _empty()
            row=self.repository.get_version(connection,version)
            if row is None:
                raise SensitiveTermSetError('VOCABULARY_INTEGRITY_ERROR','词库版本记录不可用',503)
            return _snapshot(row)

    def get_version(self,version_no):
        if type(version_no) is not int or version_no<0:
            raise SensitiveTermSetError('VALIDATION_ERROR','词库版本号无效',422)
        if version_no==0:return _empty()
        with self.repository.engine.connect() as connection:
            row=self.repository.get_version(connection,version_no)
            if row is None:
                raise SensitiveTermSetError('VERSION_NOT_FOUND','词库版本不存在',404)
            return _snapshot(row)

    def history(self,*,page=1,page_size=20):
        if type(page) is not int or page<1 or type(page_size) is not int or not 1<=page_size<=100:
            raise SensitiveTermSetError('VALIDATION_ERROR','分页参数无效',422)
        with self.repository.engine.connect() as connection:
            rows,total=self.repository.history(connection,page,page_size)
            return dict(items=[_snapshot(row) for row in rows],page=page,pageSize=page_size,total=total)

    def save_version(self,payload,*,actor_user_id,request_id):
        if not isinstance(payload,dict) or type(payload.get('expectedVersion')) is not int or payload['expectedVersion']<0:
            raise SensitiveTermSetError('VERSION_REQUIRED','请提供当前词库版本',422)
        rules=_validated_rules(payload.get('rules'))
        note=payload.get('note','')
        if not is_supported_text(note) or len(note)>2000:
            raise SensitiveTermSetError('VALIDATION_ERROR','词库备注必须是最多2000字符的有效文本',422)
        expected=payload['expectedVersion']
        with self.repository.engine.begin() as connection:
            self._maintainer(connection,actor_user_id)
            if not self.repository.advance(connection,expected):
                raise SensitiveTermSetError('VERSION_CONFLICT','词库已被其他操作更新；请刷新核对后保存',409)
            self.repository.insert(connection,dict(version_no=expected+1,rules=rules,note=note,created_by=actor_user_id,created_at=datetime.now(timezone.utc)))
            try:
                self.audit_service.record(connection,event_name='file_operation_completed',user_id=actor_user_id,object_type='DOCUMENT',object_id=f'sensitive-terms-v{expected+1}',result='SUCCESS',request_id=request_id,duration_ms=0,properties={'operation':'sensitive_terms_saved','file_type':'sensitive_terms'})
            except Exception as error:
                raise SensitiveTermSetError('AUDIT_UNAVAILABLE','审计服务暂不可用，词库尚未保存',503) from error
            result=_snapshot(self.repository.get_version(connection,expected+1))
        return result
