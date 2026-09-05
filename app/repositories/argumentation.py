"""Persistence for the retained project argumentation editor (not proposals)."""

import json
import re
import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert


class ArgumentationConflict(RuntimeError):
    """A stale editor or ambiguous legacy data cannot overwrite saved content."""


class ArgumentationRepository:
    def __init__(self, engine):
        self.engine = engine
        metadata = sa.MetaData()
        self.templates = sa.Table("doc_templates", metadata, autoload_with=engine)
        self.documents = sa.Table("project_documents", metadata, autoload_with=engine)
        self.versions = sa.Table("document_versions", metadata, autoload_with=engine)
        self.equipment = sa.Table("equipment", metadata, autoload_with=engine)
        self.projects = {
            category: sa.Table(table, metadata, autoload_with=engine)
            for category, table in {
                "research": "projects",
                "crypto": "crypto_projects",
                "security": "security_projects",
            }.items()
        }

    @staticmethod
    def legacy_row(row):
        if row is None:
            return None
        result = dict(row)
        for key, value in result.items():
            if isinstance(value, datetime):
                result[key] = value.isoformat(sep=" ")
        if isinstance(result.get("chapter_tree"), dict):
            result["chapter_tree"] = json.dumps(
                result["chapter_tree"], ensure_ascii=False
            )
        return result

    def get_template_by_category(self, category):
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(self.templates)
                    .where(
                        self.templates.c.category == category,
                    )
                    .order_by(
                        self.templates.c.version.desc(), self.templates.c.id.desc()
                    )
                    .limit(1)
                )
                .mappings()
                .first()
            )
            return self.legacy_row(row)

    def get_all_templates(self):
        with self.engine.connect() as connection:
            return [
                self.legacy_row(row)
                for row in connection.execute(
                    sa.select(self.templates).order_by(
                        self.templates.c.category,
                        self.templates.c.version.desc(),
                    )
                ).mappings()
            ]

    def save_template(self, template_id, name, category, file_path, chapter_tree, *, expected_version, expected_template_id=None):
        if isinstance(chapter_tree, str):
            chapter_tree = json.loads(chapter_tree)
        if category not in self.projects or not isinstance(name, str):
            raise ValueError("模板类别或名称不正确")
        self.validate_tree(chapter_tree)
        if type(expected_version) is not int or expected_version < 0:
            raise ValueError("请刷新模板后再保存：缺少有效版本")
        if expected_version > 0 and expected_template_id != template_id:
            raise ArgumentationConflict("当前模板已变化，请刷新后重试；当前修改尚未保存")
        now = datetime.now(timezone.utc)
        values = dict(name=name, chapter_tree=chapter_tree, updated_at=now)
        if file_path is not None:
            values['file_path'] = file_path
        if expected_version == 0:
            statement = insert(self.templates).values(
                **values, template_id=template_id, category=category,
                version=1, created_at=now,
            ).on_conflict_do_nothing(index_elements=[self.templates.c.template_id])
        else:
            statement = self.templates.update().where(
                self.templates.c.template_id == template_id,
                self.templates.c.category == category,
                self.templates.c.version == expected_version,
            ).values(**values, version=self.templates.c.version + 1)
        with self.engine.begin() as connection:
            version = connection.execute(statement.returning(self.templates.c.version)).scalar_one_or_none()
            if version is None:
                raise ArgumentationConflict("模板已被其他页面更新，请刷新后重试；当前修改尚未保存")
            return version

    @staticmethod
    def validate_tree(tree):
        if not isinstance(tree, dict) or not isinstance(tree.get("chapters"), list):
            raise ValueError("模板必须包含章节列表")
        seen = set()

        def walk(chapters, depth):
            if not isinstance(chapters, list) or depth > 16:
                raise ValueError("章节结构无效")
            for chapter in chapters:
                if not isinstance(chapter, dict):
                    raise ValueError("章节必须为对象")
                identifier = chapter.get("id")
                if (
                    not isinstance(identifier, str)
                    or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", identifier)
                    or identifier in seen
                ):
                    raise ValueError("章节编号无效或重复")
                seen.add(identifier)
                if not isinstance(chapter.get("title"), str):
                    raise ValueError("章节标题必须为文本")
                if any(
                    key in chapter and not isinstance(chapter[key], str)
                    for key in ("type", "content")
                ):
                    raise ValueError("章节类型与正文必须为文本")
                walk(chapter.get("children", []), depth + 1)

        walk(tree["chapters"], 0)

    def save_revision(
        self,
        connection,
        *,
        project_id,
        category,
        content,
        editor,
        change_note,
        expected_version
    ):
        if type(expected_version) is not int or expected_version < 0:
            raise ValueError("请提供当前文档版本")
        project_table = self.projects.get(category)
        if project_table is None:
            raise ValueError("不支持的项目类别")
        # The existing parent row serializes even simultaneous first saves.
        project = connection.execute(
            sa.select(project_table.c.project_id)
            .where(
                project_table.c.project_id == project_id,
            )
            .with_for_update()
        ).first()
        if project is None:
            raise LookupError("项目不存在")
        documents = (
            connection.execute(
                sa.select(self.documents)
                .where(
                    self.documents.c.project_id == project_id,
                    self.documents.c.category == category,
                )
                .limit(2)
            )
            .mappings()
            .all()
        )
        if len(documents) > 1:
            raise ArgumentationConflict("该项目存在重复论证文档，请先核对历史数据")
        document = documents[0] if documents else None
        doc_id = document["doc_id"] if document else uuid.uuid4().hex
        history = self._versions(connection, doc_id)
        latest = max((row["version_num"] for row in history), default=0)
        if document and document.get("content") and not history:
            raise ArgumentationConflict("原文档缺少版本历史，请先核对历史数据")
        if latest != expected_version:
            raise ArgumentationConflict("文档已被更新，请保留当前输入并刷新后重试")
        if document and document.get("content"):
            previous = json.loads(document["content"])
            if not isinstance(previous, dict):
                raise ArgumentationConflict("历史正文格式异常，请先核对历史数据")
            # Template edits must not silently discard fields hidden by that template.
            content = json.dumps(
                {**previous, **json.loads(content)}, ensure_ascii=False
            )
        version_num = latest + 1
        now = datetime.now(timezone.utc)
        template_id = connection.scalar(
            sa.select(self.templates.c.template_id)
            .where(
                self.templates.c.category == category,
            )
            .order_by(self.templates.c.version.desc(), self.templates.c.id.desc())
            .limit(1)
        )
        values = dict(content=content, updated_at=now)
        if document:
            connection.execute(
                self.documents.update()
                .where(
                    self.documents.c.id == document["id"],
                )
                .values(**values)
            )
        else:
            connection.execute(
                self.documents.insert().values(
                    doc_id=doc_id,
                    project_id=project_id,
                    category=category,
                    template_id=template_id,
                    created_at=now,
                    **values,
                )
            )
        connection.execute(
            self.versions.insert().values(
                version_id=uuid.uuid4().hex,
                doc_id=doc_id,
                version_num=version_num,
                version_number=str(version_num),
                content=content,
                editor=editor,
                change_note=change_note,
                created_at=now,
            )
        )
        return doc_id, version_num

    def get_document_by_project(self, project_id, category):
        with self.engine.connect() as connection:
            return self.legacy_row(
                connection.execute(
                    sa.select(self.documents)
                    .where(
                        self.documents.c.project_id == project_id,
                        self.documents.c.category == category,
                    )
                    .order_by(self.documents.c.id.desc())
                    .limit(1)
                )
                .mappings()
                .first()
            )

    def get_document_by_id(self, doc_id):
        with self.engine.connect() as connection:
            return self.legacy_row(
                connection.execute(
                    sa.select(self.documents).where(
                        self.documents.c.doc_id == doc_id,
                    )
                )
                .mappings()
                .first()
            )

    def get_document_versions(self, doc_id):
        with self.engine.connect() as connection:
            return self._versions(connection, doc_id)

    def _versions(self, connection, doc_id):
        rows = [
            self.legacy_row(row)
            for row in connection.execute(
                sa.select(self.versions).where(self.versions.c.doc_id == doc_id)
            ).mappings()
        ]
        seen = set()
        for row in rows:
            number = row.get("version_num")
            if number is None:
                raw = str(row.get("version_number") or "")
                if not raw.isascii() or not raw.isdigit():
                    raise ArgumentationConflict("历史版本号异常，请先核对历史数据")
                number = int(raw)
            if number < 1 or number in seen:
                raise ArgumentationConflict("历史版本号重复或异常，请先核对历史数据")
            seen.add(number)
            row["version_num"] = number
        return sorted(rows, key=lambda row: row["version_num"], reverse=True)

    def get_projects(self, category, project_id=None):
        table = self.projects.get(category)
        if table is None:
            raise ValueError("不支持的项目类别")
        statement = sa.select(table).order_by(table.c.id.desc())
        if project_id is not None:
            statement = statement.where(table.c.project_id == project_id)
        with self.engine.connect() as connection:
            return [
                self.legacy_row(row) for row in connection.execute(statement).mappings()
            ]

    def get_equipment(self):
        with self.engine.connect() as connection:
            return [
                self.legacy_row(row)
                for row in connection.execute(
                    sa.select(self.equipment).order_by(self.equipment.c.id)
                ).mappings()
            ]
