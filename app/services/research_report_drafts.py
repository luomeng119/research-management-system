"""Prepare local generation outside transactions, then save immutable evidence."""
from datetime import datetime,timezone
import hashlib
import uuid
import re

from app.services.files import FileServiceError
from app.services.research_reports import ResearchReportServiceError,_text,_serialize


def _draft(row):
    result=_serialize(row)
    for source,target in [('base_version','baseVersion'),('input_body','inputBody'),('input_title','inputTitle'),('input_purpose','inputPurpose'),('model_name','model'),('prompt_version','promptVersion'),('input_hash','inputHash'),('generated_body','body')]:
        result[target]=result.pop(source)
    return result


def _remap_source_citations(body, old_sources, new_sources):
    """Rebind local labels by immutable attachment identity, never by position."""
    if not old_sources:
        return body
    old = {source['sourceId']: source for source in old_sources}
    new = {(str(source['fileId']), source['versionNo']): source['sourceId'] for source in new_sources}
    items = r' *S[1-8](?: *[,，] *S[1-8])* *'
    interval = r' *S[1-8] *- *S[1-8] *'
    pattern = rf'\[来源 *S[1-8]\]|\((?:{items}|{interval})\)|（(?:{items}|{interval})）'
    # Keep validation on original spans: removing one tag must not create another.
    unmatched = re.sub(pattern, '', body)
    if '[来源' in unmatched or re.search(r'[（(]\s*S\d', unmatched):
        raise ResearchReportServiceError('SOURCE_REFERENCE_INVALID','旧正文来源标签格式无效，请核对后重新生成',422)

    def replace(match):
        label = match.group()
        numbers = [int(number) for number in re.findall(r'S([1-8])', label)]
        if '-' in label:
            if numbers[0] > numbers[1]:
                raise ResearchReportServiceError('SOURCE_REFERENCE_INVALID','旧正文来源范围无效',422)
            numbers = list(range(numbers[0], numbers[1] + 1))
        ids = [f'S{number}' for number in numbers]
        replacements = []
        unchanged = True
        for source_id in ids:
            source = old.get(source_id)
            if source is None:
                raise ResearchReportServiceError('SOURCE_REFERENCE_INVALID','旧正文引用未记录的来源，请核对后重新生成',422)
            mapped = new.get((str(source['fileId']),source['versionNo']))
            if mapped is None:
                replacements.append(f"【旧来源未纳入：文件{source['fileId']}，版本{source['versionNo']}；不可作为本次证据】")
            else:
                replacements.append(f'[来源{mapped}]')
            unchanged = unchanged and mapped == source_id
        return label if unchanged else ''.join(replacements)

    return re.sub(pattern, replace, body)


class ResearchReportDraftService:
    def __init__(self,repository,report_service,file_service,assistant,source_extractor,sensitive_term_service=None):
        self.repository=repository
        self.report_service=report_service
        self.file_service=file_service
        self.assistant=assistant
        self.source_extractor=source_extractor
        self.sensitive_term_service=sensitive_term_service

    def get(self,report_id,draft_id):
        with self.repository.engine.connect() as connection:
            self.report_service._get(connection,report_id)
            try:
                row=self.repository.get(connection,report_id,draft_id)
            except (TypeError,ValueError,AttributeError):
                row=None
            if row is None:
                raise ResearchReportServiceError('DRAFT_NOT_FOUND','报告草稿不存在',404)
            return _draft(row)

    def generate(self,report_id,payload,*,actor_user_id,request_id):
        if not isinstance(payload,dict) or type(payload.get('baseVersion')) is not int or payload['baseVersion']<1:
            raise ResearchReportServiceError('VERSION_REQUIRED','请提供当前报告版本',422)
        body=_text(payload,'body',12000,default=None)
        selected=payload.get('selections')
        if not isinstance(selected,list) or not 1<=len(selected)<=8:
            raise ResearchReportServiceError('INVALID_SOURCES','请选择 1 至 8 个资料版本',422)
        seen=set()
        for item in selected:
            if not isinstance(item,dict) or not isinstance(item.get('fileId'),str) or not item['fileId'] or len(item['fileId'])>200 or type(item.get('versionNo')) is not int or item['versionNo']<1:
                raise ResearchReportServiceError('INVALID_SOURCES','资料版本参数无效',422)
            key=(item['fileId'],item['versionNo'])
            if key in seen:
                raise ResearchReportServiceError('INVALID_SOURCES','请勿重复选择相同资料版本',422)
            seen.add(key)
        old_sources = None
        with self.repository.engine.connect() as connection:
            self.report_service._actor(connection,actor_user_id)
            report=self.report_service._get(connection,report_id)
            if report['current_version']!=payload['baseVersion']:
                raise ResearchReportServiceError('VERSION_CONFLICT','报告版本已变化，请刷新后生成',409)
            report=dict(report)
            current = self.report_service.repository.get_version(connection,report_id,payload['baseVersion'])
            inherited_snapshot = current.get('redaction_snapshot') if current else None
            if current is not None and current.get('draft_id'):
                prior_draft = self.report_service.repository.get_draft(connection,report_id,current['draft_id'])
                if prior_draft is None:
                    raise ResearchReportServiceError('DRAFT_INTEGRITY_ERROR','报告来源记录不可用',503)
                report['title'], report['purpose'] = prior_draft['input_title'], prior_draft['input_purpose']
                old_sources = prior_draft['sources']
        # Freeze exactly once before source extraction/inference. Never fall back to
        # unprocessed generation when vocabulary infrastructure is unavailable.
        context_service = self.report_service
        if self.sensitive_term_service is not None:
            from app.services.research_reports import ResearchReportService
            context_service = ResearchReportService(self.report_service.repository, self.report_service.audit_service, self.sensitive_term_service)
        snapshot, compiled = context_service.redaction_context(version_no=payload.get('dictionaryVersion'),required=True)
        input_version = payload.get('inputDictionaryVersion')
        if inherited_snapshot is not None or input_version is not None:
            inherited_snapshot, _ = context_service.redaction_context(inherited_snapshot,version_no=input_version,required=True)
        if 'redaction_snapshot' not in self.repository.table.c:
            raise ResearchReportServiceError('REDACTION_UNAVAILABLE','报告词库记录尚未就绪，本次生成未保存',503)
        fields = context_service.upgrade_redaction_fields(dict(title=report['title'],purpose=report['purpose'],body=body),inherited_snapshot,snapshot,compiled)
        title, purpose = _text(fields,'title',200,required=True), _text(fields,'purpose',2000)
        body = _text(fields,'body',12000)
        sources=[]
        for index,item in enumerate(selected,1):
            try:
                opened=self.file_service.open_version_stream(item['fileId'],item['versionNo'],object_type='PROPOSAL' if report['proposal_id'] is not None else 'PROJECT',object_id=report['object_id'])
            except FileServiceError as error:
                raise ResearchReportServiceError(error.code,error.message,error.status_code) from error
            try:
                _text(opened,'originalName',255,required=True)
                extracted=self.source_extractor(opened)
            except ResearchReportServiceError:
                raise
            except ValueError as error:
                raise ResearchReportServiceError('SOURCE_EXTRACTION_FAILED','资料文本提取失败，请检查资料格式和内容',422) from error
            finally:
                if not opened['stream'].closed:
                    opened['stream'].close()
            _text(extracted,'text',12000,required=True)
            processed = context_service.redact_fields(dict(text=extracted['text'],segments=extracted['segments'],filename=opened['originalName']),compiled)
            _text(processed,'text',12000,required=True)
            _text(processed,'filename',255,required=True)
            # Original attachment hashes stay unchanged. The text hash describes
            # the processed model input, with the original extraction hash separate.
            original_extracted_text = extracted['text']
            extracted = dict(extracted, text=processed['text'],segments=processed['segments'],
                             originalTextSha256=extracted['textSha256'],
                             textSha256=hashlib.sha256(processed['text'].encode('utf-8')).hexdigest())
            if extracted.get('sourceKind') == 'IMAGE':
                extracted['originalImageBlockedByRedaction'] = processed['text'] != original_extracted_text
            sources.append(dict(sourceId=f'S{index}',label=f'S{index}',fileId=item['fileId'],versionNo=item['versionNo'],filename=processed['filename'],sha256=opened['sha256'],objectType='PROPOSAL' if report['proposal_id'] is not None else 'PROJECT',objectId=report['object_id'],sizeBytes=opened['sizeBytes'],mediaType=opened['mediaType'],**extracted))
        if sum(len(source['text']) for source in sources)>12000:
            raise ResearchReportServiceError('SOURCE_TOO_LARGE','所选资料合计超过 12000 字符，请缩小本次资料范围',422)
        body = _text(dict(body=_remap_source_citations(body, old_sources, sources)), 'body', 12000)
        # No database connection or parent lock is held during extraction or inference.
        try:
            generated=self.assistant.generate(title=title,purpose=purpose,current_body=body,sources=sources)
        except ResearchReportServiceError:
            raise
        except ValueError as error:
            raise ResearchReportServiceError('MODEL_INPUT_LIMIT',str(error),422) from error
        except Exception as error:
            raise ResearchReportServiceError('MODEL_UNAVAILABLE','本地模型暂未完成生成，报告未保存，请稍后重试',503) from error
        if not isinstance(generated,dict):
            raise ResearchReportServiceError('MODEL_INVALID_RESPONSE','本地模型返回无效草稿，报告未保存',503)
        try:
            generated_body=_text(generated,'body',200000,required=True)
            generated_body=_text(dict(body=context_service.redact_fields(generated_body,compiled)),'body',200000,required=True)
            model=_text(generated,'model',200,required=True)
            prompt=_text(generated,'promptVersion',200,required=True)
        except ResearchReportServiceError as error:
            raise ResearchReportServiceError('MODEL_INVALID_RESPONSE','本地模型返回无效草稿，报告未保存',503) from error
        draft_id=uuid.uuid4()
        values=dict(redaction_snapshot=snapshot,id=draft_id,report_id=report_id,base_version=payload['baseVersion'],input_title=title,input_purpose=purpose,input_body=body,sources=sources,model_name=model,prompt_version=prompt,usage=generated.get('usage',{}),input_hash=generated.get('inputHash'),generated_body=generated_body,created_by=actor_user_id,created_at=datetime.now(timezone.utc))
        with self.repository.engine.begin() as connection:
            self.report_service._actor(connection,actor_user_id)
            latest_version=self.report_service.repository.lock_current(connection,report_id)
            if latest_version!=payload['baseVersion']:
                raise ResearchReportServiceError('VERSION_CONFLICT','生成期间报告已变化；请核对最新版本后重新生成',409)
            self.repository.insert(connection,values)
            self.report_service._audit(connection,report_id,actor_user_id,request_id,'report_draft_generated')
            result=_draft(self.repository.get(connection,report_id,draft_id))
        return result
