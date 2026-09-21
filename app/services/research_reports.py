"""Authenticated web boundaries supply the actor; revisions remain immutable."""
from __future__ import annotations

from datetime import datetime, timezone
import uuid
import hashlib
import json
import re

from app.services.sensitive_terms import compile_rules, replace_sensitive_fields, SensitiveTermError

from app.text import has_meaningful_text


class ResearchReportServiceError(RuntimeError):
    def __init__(self, code, message, status_code=400, *, fields=None):
        super().__init__(message)
        self.code, self.message, self.status_code = code, message, status_code
        self.fields = fields or {}


def _xml_character(character):
    number = ord(character)
    return (
        character in '\t\n\r'
        or 0x20 <= number <= 0xD7FF
        or 0xE000 <= number <= 0xFFFD
        or 0x10000 <= number <= 0x10FFFF
    )


def _text(payload, key, limit, *, required=False, default=''):
    value = payload.get(key, default)
    if (
        not isinstance(value, str)
        or len(value) > limit
        or any(not _xml_character(ch) for ch in value)
        or (required and not has_meaningful_text(value))
    ):
        raise ResearchReportServiceError('VALIDATION_ERROR', '报告字段无效', 422, fields={key: f'请提供有效文本，最多 {limit} 字符'})
    return value


def _page(page, page_size):
    if type(page) is not int or type(page_size) is not int or page < 1 or not 1 <= page_size <= 100:
        raise ResearchReportServiceError('VALIDATION_ERROR', '分页参数无效', 422)
    return page, page_size


def _serialize(row):
    mapping = {'object_id':'objectId','current_version':'currentVersion','created_by':'createdBy', 'updated_by':'updatedBy','created_at':'createdAt','updated_at':'updatedAt','report_id':'reportId','version_no':'versionNo','created_by_name':'createdByName','draft_id':'draftId','redaction_snapshot':'redactionSnapshot'}
    result = {}
    for key, value in row.items():
        if key in {'proposal_id','project_registry_id'}:
            continue
        if isinstance(value, datetime):
            value = value.isoformat()
        elif isinstance(value, uuid.UUID):
            value = str(value)
        result[mapping.get(key,key)] = value
    if 'proposal_id' in row:
        result['objectType'] = 'PROPOSAL' if row['proposal_id'] is not None else 'PROJECT'
    return result


class ResearchReportService:
    def __init__(self, repository, audit_service, sensitive_term_service=None):
        self.repository, self.audit_service = repository, audit_service
        self.sensitive_term_service = sensitive_term_service

    def redaction_context(self, snapshot=None, *, version_no=None, required=False):
        """Trusted backend helper; compiled rules must never be serialized to a client.

        Version identity and hash cover the full rules list (including disabled
        entries); replacement itself uses enabled rules only. An existing snapshot
        always wins over current vocabulary and a conflicting submitted version.
        """
        strategy = 'literal-longest-ascii-boundary-alias-protected-v1'
        if version_no is not None and (type(version_no) is not int or version_no < 0):
            raise ResearchReportServiceError('VALIDATION_ERROR', '词库版本号无效', 422)
        if snapshot is not None:
            if (not isinstance(snapshot, dict) or set(snapshot) != {'version','rulesHash','strategyVersion'}
                    or type(snapshot['version']) is not int or snapshot['version'] < 0
                    or snapshot['strategyVersion'] != strategy):
                raise ResearchReportServiceError('REDACTION_INTEGRITY_ERROR', '报告词库快照不可用', 503)
            if version_no is not None and version_no != snapshot['version']:
                raise ResearchReportServiceError('REDACTION_VERSION_CONFLICT', '词库版本与报告固定版本不一致，请刷新后重试', 409)
            version_no = snapshot['version']
        if self.sensitive_term_service is None:
            if required or snapshot is not None or version_no is not None:
                raise ResearchReportServiceError('REDACTION_UNAVAILABLE', '词库服务尚未就绪，本次操作未保存', 503)
            return None, None
        try:
            vocabulary = (self.sensitive_term_service.current() if version_no is None
                          else self.sensitive_term_service.get_version(version_no))
            if not isinstance(vocabulary, dict) or type(vocabulary.get('version')) is not int or vocabulary['version'] < 0:
                raise ValueError('Invalid vocabulary')
            if version_no is not None and vocabulary['version'] != version_no:
                raise ValueError('Wrong vocabulary version')
            rules = vocabulary.get('rules')
            if not isinstance(rules, list) or any(not isinstance(rule,dict) or type(rule.get('enabled')) is not bool for rule in rules):
                raise ValueError('Invalid vocabulary rules')
            # Copy before compilation: later maintenance or test-double mutation
            # cannot alter this operation's frozen dictionary.
            canonical = json.dumps(rules, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
            copied = json.loads(canonical)
            compiled = compile_rules([{key: rule.get(key) for key in ('id','source','replacement')} for rule in copied if rule['enabled']])
            metadata = dict(version=vocabulary['version'], rulesHash=hashlib.sha256(canonical.encode('utf-8')).hexdigest(), strategyVersion=strategy)
            if snapshot is not None and metadata != snapshot:
                raise ValueError('Vocabulary integrity mismatch')
            return metadata, compiled
        except Exception as error:
            raise ResearchReportServiceError('REDACTION_INTEGRITY_ERROR', '报告指定的词库版本不可用，请联系维护人员核对，本次操作未保存', 503) from error

    @staticmethod
    def redact_fields(value, compiled):
        if compiled is None:
            return value
        try:
            return replace_sensitive_fields(value, compiled)['value']
        except SensitiveTermError as error:
            raise ResearchReportServiceError('REDACTION_FAILED', '词库替换存在冲突，本次操作未保存，请核对词库与内容', 422) from error

    def upgrade_redaction_fields(self, value, old_snapshot, new_snapshot, new_compiled):
        """Translate existing aliases by stable rule identity, never reverse to originals.

        Ambiguous/deleted mappings are rejected only when their old alias occurs in
        this input. Original attachments do not use this conversion: they are
        processed directly from their authorized original text by the new rules.
        """
        if old_snapshot is None or old_snapshot == new_snapshot:
            return self.redact_fields(value, new_compiled)
        _, old_compiled = self.redaction_context(old_snapshot, required=True)
        new_rules = {rule.id: rule for rule in new_compiled.rules}
        aliases = {}
        for rule in old_compiled.rules:
            target = new_rules.get(rule.id)
            replacement = target.replacement if target is not None and target.source == rule.source else None
            aliases.setdefault(rule.replacement, set()).add(replacement)
        ordered = sorted(aliases, key=lambda alias: (-len(alias), alias))
        pattern = re.compile('|'.join(re.escape(alias) for alias in ordered)) if ordered else None

        def convert(item):
            if isinstance(item, str):
                if pattern is None:
                    return item
                # Check every alias occurrence, not only the winning regex match.
                # Shared or partially overlapping aliases cannot prove identity.
                spans = []
                for alias in ordered:
                    for match in re.finditer('(?=' + re.escape(alias) + ')', item):
                        targets = aliases[alias]
                        if len(targets) != 1 or None in targets:
                            raise ResearchReportServiceError('REDACTION_UPGRADE_AMBIGUOUS', '旧代称与新词库存在歧义，未生成或保存；请清空或人工核对旧正文后重试', 422)
                        spans.append((match.start(), match.start() + len(alias)))
                spans.sort()
                if any(start < previous_end for (_, previous_end), (start, _) in zip(spans, spans[1:])):
                    raise ResearchReportServiceError('REDACTION_UPGRADE_AMBIGUOUS', '旧代称存在交叠，未生成或保存；请清空或人工核对旧正文后重试', 422)
                return pattern.sub(lambda match: next(iter(aliases[match.group()])), item)
            if isinstance(item, list):
                return [convert(child) for child in item]
            if isinstance(item, dict):
                return {key: convert(child) for key, child in item.items()}
            return item

        return self.redact_fields(convert(value), new_compiled)

    def _snapshot_column(self, snapshot):
        if 'redaction_snapshot' not in self.repository.versions.c:
            if snapshot is not None:
                raise ResearchReportServiceError('REDACTION_UNAVAILABLE', '报告词库记录尚未就绪，本次操作未保存', 503)
            return {}
        return {'redaction_snapshot': snapshot}


    def _parent(self, connection, object_type, object_id):
        if not isinstance(object_type, str) or object_type not in {'PROPOSAL','PROJECT'} or not isinstance(object_id,str) or not object_id or len(object_id) > 200:
            raise ResearchReportServiceError('VALIDATION_ERROR','请选择所属提案或项目',422)
        parents = self.repository.parent(connection,object_type,object_id)
        if not parents:
            raise ResearchReportServiceError('PARENT_NOT_FOUND','所属提案或项目不存在',404)
        if len(parents) != 1:
            raise ResearchReportServiceError('PARENT_AMBIGUOUS','项目编号存在歧义，请先核对项目',409)
        return parents[0]

    def _get(self, connection, report_id):
        try:
            row = self.repository.get(connection,report_id)
        except (ValueError, TypeError, AttributeError):
            row = None
        if row is None:
            raise ResearchReportServiceError('REPORT_NOT_FOUND','科研报告不存在',404)
        return row

    def _actor(self, connection, actor):
        if type(actor) is not int or actor <= 0 or not self.repository.actor_exists(connection, actor):
            raise ResearchReportServiceError('AUTHENTICATION_REQUIRED','请登录后操作',401)

    def _audit(self, connection, report_id, actor, request_id, operation):
        try:
            self.audit_service.record(
                connection, event_name='file_operation_completed', user_id=actor,
                object_type='DOCUMENT', object_id=str(report_id), result='SUCCESS',
                request_id=request_id, duration_ms=0,
                properties={'operation': operation, 'file_type': 'research_report'},
            )
        except Exception as error:
            # Audit is part of this transaction; never commit an unaudited revision.
            # Do not expose database errors, credentials, or report text to the UI.
            raise ResearchReportServiceError(
                'AUDIT_UNAVAILABLE', '审计服务暂不可用，报告尚未保存，请稍后重试', 503
            ) from error

    def create(self, payload, *, actor_user_id, request_id):
        if not isinstance(payload,dict):
            raise ResearchReportServiceError('VALIDATION_ERROR','报告参数无效',422)
        title = _text(payload,'title',200,required=True)
        purpose = _text(payload,'purpose',2000)
        body, note = _text(payload,'body',200000), _text(payload,'note',2000)
        snapshot, compiled = self.redaction_context()
        fields = self.redact_fields(dict(title=title,purpose=purpose,body=body,note=note), compiled)
        title, purpose = _text(fields,'title',200,required=True), _text(fields,'purpose',2000)
        body, note = _text(fields,'body',200000), _text(fields,'note',2000)
        snapshot_column = self._snapshot_column(snapshot)
        now, report_id = datetime.now(timezone.utc), uuid.uuid4()
        with self.repository.engine.begin() as connection:
            self._actor(connection, actor_user_id)
            parent = self._parent(connection,payload.get('objectType'),payload.get('objectId'))
            parent_column = 'proposal_id' if payload['objectType'] == 'PROPOSAL' else 'project_registry_id'
            self.repository.insert_report(connection,dict(id=report_id, **{parent_column:parent['id']},
                title=title,purpose=purpose,current_version=1,created_by=actor_user_id,updated_by=actor_user_id,
                created_at=now,updated_at=now))
            self.repository.append_version(connection,dict(id=uuid.uuid4(),report_id=report_id,version_no=1,
                body=body,note=note,created_by=actor_user_id,created_at=now,**snapshot_column))
            self._audit(connection,report_id,actor_user_id,request_id,'report_created')
            result = _serialize(self._get(connection,report_id))
        return result

    def get(self, report_id):
        with self.repository.engine.connect() as connection:
            return _serialize(self._get(connection,report_id))

    def list_for_object(self, object_type, object_id, *, page=1, page_size=20):
        page,page_size = _page(page,page_size)
        with self.repository.engine.connect() as connection:
            parent = self._parent(connection,object_type,object_id)
            rows,total = self.repository.list_for_parent(connection,object_type,parent['id'],page,page_size)
            return dict(items=[_serialize(r) for r in rows],page=page,pageSize=page_size,total=total)

    def current(self, report_id):
        with self.repository.engine.connect() as connection:
            report = self._get(connection,report_id)
            return self._version(connection,self.repository.get_version(connection,report_id,report['current_version']))

    def get_version(self, report_id, version_no):
        if type(version_no) is not int or version_no < 1:
            raise ResearchReportServiceError('VALIDATION_ERROR','版本号无效',422)
        with self.repository.engine.connect() as connection:
            self._get(connection,report_id)
            row = self.repository.get_version(connection,report_id,version_no)
            if row is None:
                raise ResearchReportServiceError('VERSION_NOT_FOUND','报告版本不存在',404)
            return self._version(connection,row)

    def history(self, report_id, *, page=1, page_size=20):
        page,page_size = _page(page,page_size)
        with self.repository.engine.connect() as connection:
            self._get(connection,report_id)
            rows,total = self.repository.history(connection,report_id,page,page_size)
            items = []
            for row in rows:
                item = _serialize(row)
                if item.get('redactionSnapshot') is not None:
                    _, compiled = self.redaction_context(item['redactionSnapshot'])
                    item['createdByName'] = self.redact_fields(item.get('createdByName'), compiled)
                items.append(item)
            return dict(items=items,page=page,pageSize=page_size,total=total)

    def _version(self, connection, row):
        result = _serialize(row)
        result.setdefault('draftId', None)
        result.setdefault('redactionSnapshot', None)
        report = self._get(connection, row['report_id'])
        result.update(title=report['title'], purpose=report['purpose'])
        result.update(sources=[], model=None, promptVersion=None)
        if result['draftId']:
            draft = self.repository.get_draft(connection, row['report_id'], result['draftId'])
            if draft is None:
                raise ResearchReportServiceError('DRAFT_INTEGRITY_ERROR', '报告来源记录不可用', 503)
            result.update(sources=draft['sources'], model=draft['model_name'], promptVersion=draft['prompt_version'], title=draft['input_title'], purpose=draft['input_purpose'])
        if result['redactionSnapshot'] is not None:
            _, compiled = self.redaction_context(result['redactionSnapshot'])
            processed = self.redact_fields({key:result[key] for key in ('title','purpose','body','note','createdByName') if key in result}, compiled)
            result.update(processed)
            # Preserve source identifiers and original attachment hashes. Only
            # display/content fields are mapped; legacy snapshots remain untouched.
            sources = []
            for source in result['sources']:
                fields = self.redact_fields({key:source[key] for key in ('filename','text','segments') if key in source}, compiled)
                mapped = dict(source, **fields)
                if 'text' in mapped:
                    mapped['originalTextSha256'] = source.get('originalTextSha256', source.get('textSha256'))
                    mapped['textSha256'] = hashlib.sha256(mapped['text'].encode('utf-8')).hexdigest()
                sources.append(mapped)
            result['sources'] = sources
        return result

    def save_version(self, report_id, payload, *, actor_user_id, request_id):
        if not isinstance(payload,dict) or type(payload.get('baseVersion')) is not int or payload['baseVersion'] < 1:
            raise ResearchReportServiceError('VERSION_REQUIRED','请提供当前报告版本',422)
        body = _text(payload,'body',200000,default=None)
        note = _text(payload,'note',2000)
        now, expected = datetime.now(timezone.utc), payload['baseVersion']
        with self.repository.engine.begin() as connection:
            self._actor(connection, actor_user_id)
            self._get(connection,report_id)
            draft_id = payload.get('draftId')
            if draft_id:
                try:
                    draft = self.repository.get_draft(connection,report_id,draft_id)
                except (ValueError,TypeError,AttributeError):
                    draft = None
                if draft is None:
                    raise ResearchReportServiceError('DRAFT_NOT_FOUND','报告草稿不存在或不属于此报告',404)
                if draft['base_version'] != expected:
                    raise ResearchReportServiceError('VERSION_CONFLICT','草稿基于其他报告版本，请重新生成或核对',409)
                snapshot = draft.get('redaction_snapshot')
            else:
                current = self.repository.get_version(connection,report_id,expected)
                draft_id = current.get('draft_id') if current else None
                snapshot = current.get('redaction_snapshot') if current else None
            snapshot, compiled = self.redaction_context(snapshot, version_no=payload.get('dictionaryVersion'))
            fields = self.redact_fields(dict(body=body,note=note), compiled)
            body, note = _text(fields,'body',200000), _text(fields,'note',2000)
            snapshot_column = self._snapshot_column(snapshot)
            if not self.repository.advance(connection,report_id,expected,actor_user_id,now):
                raise ResearchReportServiceError('VERSION_CONFLICT','报告已被其他页面更新；当前修改尚未保存，请先核对最新版本',409)
            self.repository.append_version(connection,dict(id=uuid.uuid4(),report_id=report_id,version_no=expected+1,
                body=body,note=note,created_by=actor_user_id,created_at=now,
                **({'draft_id':draft_id} if 'draft_id' in self.repository.versions.c else {}), **snapshot_column))
            self._audit(connection,report_id,actor_user_id,request_id,'report_version_saved')
            result = _serialize(self._get(connection,report_id))
        return result
