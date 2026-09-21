"""Maintenance-only vocabulary snapshots; importing and previewing never writes."""
import json
import re
import uuid

from flask import Blueprint,current_app,render_template,request,redirect,url_for
from sqlalchemy.exc import SQLAlchemyError
from app.security.auth import maintenance_required,current_identity
from app.services.sensitive_term_sets import SensitiveTermSetError,_validated_rules
from app.services.sensitive_terms import is_supported_text

bp=Blueprint('sensitive_terms',__name__)


def _service():
    service=current_app.extensions.get('sensitive_term_set_service')
    if service is None:raise SensitiveTermSetError('UNAVAILABLE','词库服务尚未就绪，请联系系统维护人员',503)
    return service


def _number(value):
    if not isinstance(value,str) or not re.fullmatch(r'0|[1-9][0-9]{0,8}',value):
        raise SensitiveTermSetError('VALIDATION_ERROR','词库版本参数无效',422)
    return int(value)


def _unique_object(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise ValueError('duplicate JSON key')
        result[key]=value
    return result


def _read_rules(raw_json=None):
    if request.form.get('format')=='json':
        raw=raw_json if raw_json is not None else request.form.get('rulesJson','')
        if len(raw)>1600000:
            raise SensitiveTermSetError('VALIDATION_ERROR','导入JSON过大',422)
        try:value=json.loads(raw,object_pairs_hook=_unique_object)
        except (ValueError,RecursionError) as error:
            raise SensitiveTermSetError('VALIDATION_ERROR','JSON格式无效，请检查后重新预览',422) from error
        return _validated_rules(value)
    if request.form.get('format')!='rows':
        raise SensitiveTermSetError('VALIDATION_ERROR','请选择编辑或JSON导入',422)
    columns=[request.form.getlist(name) for name in ('ruleId','source','replacement','enabled')]
    if len({len(column) for column in columns})!=1 or len(columns[0])>1005:
        raise SensitiveTermSetError('VALIDATION_ERROR','词库表格格式无效',422)
    rows=[]
    for identifier,source,replacement,enabled in zip(*columns):
        if not identifier and not source and not replacement:continue
        if enabled not in {'true','false'}:
            raise SensitiveTermSetError('VALIDATION_ERROR','启用状态无效',422)
        rows.append(dict(id=identifier or str(uuid.uuid4()),source=source,replacement=replacement,enabled=enabled=='true'))
    return _validated_rules(rows)


def _posted_rows():
    columns=[request.form.getlist(name) for name in ('ruleId','source','replacement','enabled')]
    return [dict(id=identifier,source=source,replacement=replacement,enabled=enabled=='true') for identifier,source,replacement,enabled in zip(*columns)]


def _render(*,snapshot=None,rules=None,form=None,error=None,preview=False,status=200,history=None):
    return render_template('sensitive_terms/manage.html',snapshot=snapshot,rules=rules or [],form=form or {},error=error,preview=preview,history=history or {'items':[]}),status


@bp.route('/admin/sensitive-terms',methods=['GET','POST'])
@maintenance_required
def manage():
    form=request.form.to_dict() if request.method=='POST' else {}
    rules=_posted_rows() if request.method=='POST' and form.get('format')=='rows' else []
    try:
        service=_service()
        if request.method=='GET':
            snapshot=service.current()
            page=_number(request.args.get('page','1'))
            if page<1:raise SensitiveTermSetError('VALIDATION_ERROR','页码无效',422)
            return _render(snapshot=snapshot,rules=snapshot['rules'],form=dict(expectedVersion=str(snapshot['version']),note='',rulesJson=json.dumps(snapshot['rules'],ensure_ascii=False,indent=2)),history=service.history(page=page))
        action=form.get('action')
        if action not in {'preview','save'}:
            raise SensitiveTermSetError('VALIDATION_ERROR','操作无效，请重新预览',422)
        expected=_number(form.get('expectedVersion'))
        upload=request.files.get('rulesFile') if form.get('format')=='json' else None
        if upload is not None and upload.filename:
            content=upload.stream.read(1600001)
            if len(content)>1600000:
                raise SensitiveTermSetError('VALIDATION_ERROR','导入JSON文件过大',422)
            try:form['rulesJson']=content.decode('utf-8-sig')
            except UnicodeDecodeError as error:
                raise SensitiveTermSetError('VALIDATION_ERROR','JSON文件必须使用UTF-8编码',422) from error
        rules=_read_rules(form.get('rulesJson'))
        note=form.get('note','')
        if not is_supported_text(note) or len(note)>2000:
            raise SensitiveTermSetError('VALIDATION_ERROR','备注格式无效',422)
        if action=='preview':
            return _render(rules=rules,form=form,preview=True)
        identity=current_identity()
        saved=service.save_version(dict(expectedVersion=expected,rules=rules,note=note),actor_user_id=identity.user_id,request_id=getattr(request,'request_id','unknown'))
        return redirect(url_for('sensitive_terms.version',version_no=saved['version']))
    except SensitiveTermSetError as error:
        return _render(rules=rules,form=form,error=error.message,status=error.status_code)
    except SQLAlchemyError:
        current_app.logger.error('sensitive term storage unavailable')
        return _render(rules=rules,form=form,error='词库存储暂不可用，当前输入尚未保存',status=503)


@bp.get('/admin/sensitive-terms/versions/<int:version_no>')
@maintenance_required
def version(version_no):
    try:
        return render_template('sensitive_terms/version.html',snapshot=_service().get_version(version_no))
    except SensitiveTermSetError as error:
        return _render(error=error.message,status=error.status_code)
    except SQLAlchemyError:
        return _render(error='词库存储暂不可用',status=503)
