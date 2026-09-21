from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

PROJECT_PATH_PREFIX = "PROJECT_TREE:"
DELETED_PROJECT_PATH_PREFIX = "PROJECT_TREE_DELETED:"

OBJECT_TABLES = {
    "PROPOSAL": ("proposals", "business_id"),
    "PROJECT": ("project_registry", "business_id"),
    "EXPERT": ("experts", "expert_id"),
    "EQUIPMENT": ("equipment", "equipment_id"),
    "STANDARD": ("standards", "doc_id"),
    "TEMPLATE": ("reference_template_items", "template_id"),
    "GENERIC_TABLE": ("generic_tables", "table_id"),
    "EXPENSE": ("expense_reimbursement", "id"),
    "INVOICE": ("expense_invoice", "id"),
    "PAYMENT": ("expense_payment", "id"),
    "DOCUMENT": ("project_documents", "doc_id"),
}


class FilesRepository:
    """SQLAlchemy boundary for controlled file metadata."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        metadata = sa.MetaData()
        self.files = sa.Table("stored_files", metadata, autoload_with=engine)
        self.versions = sa.Table("stored_file_versions", metadata, autoload_with=engine)
        self.links = sa.Table("object_files", metadata, autoload_with=engine)
        self._object_tables: dict[str, tuple[sa.Table, str]] = {}

    @staticmethod
    def _id(connection: Connection, value: str | uuid.UUID):
        if connection.dialect.name == "postgresql":
            try:
                return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
            except (TypeError, ValueError, AttributeError):
                return None
        return str(value)

    @staticmethod
    def _row_values(table: sa.Table, **values):
        return {key: value for key, value in values.items() if key in table.c}

    def object_exists(
        self, connection: Connection, object_type: str, object_id: str, *, lock: bool = False
    ) -> bool:
        definition = OBJECT_TABLES.get(object_type)
        if definition is None:
            return False
        if object_type not in self._object_tables:
            table_name, key = definition
            try:
                table = sa.Table(table_name, sa.MetaData(), autoload_with=connection)
            except sa.exc.NoSuchTableError:
                return False
            if key not in table.c:
                return False
            self._object_tables[object_type] = (table, key)
        table, key = self._object_tables[object_type]
        value: object = object_id
        if isinstance(table.c[key].type, (sa.Integer, sa.BigInteger)):
            try:
                value = int(object_id)
            except (TypeError, ValueError):
                return False
        statement = sa.select(sa.literal(1)).select_from(table).where(table.c[key] == value).limit(1)
        if lock and connection.dialect.name == "postgresql":
            statement = statement.with_for_update(read=True, of=table)
        return connection.execute(statement).scalar_one_or_none() is not None

    def object_allows_file_write(
        self, connection: Connection, object_type: str, object_id: str, *, lock: bool = False
    ) -> bool | None:
        """Return None for a missing object, otherwise whether file mutation is allowed."""
        definition = OBJECT_TABLES.get(object_type)
        if definition is None:
            return None
        if object_type not in self._object_tables:
            if not self.object_exists(connection, object_type, object_id):
                return None
        table, key = self._object_tables[object_type]
        value: object = object_id
        if isinstance(table.c[key].type, (sa.Integer, sa.BigInteger)):
            try:
                value = int(object_id)
            except (TypeError, ValueError):
                return None
        if object_type in {"INVOICE", "PAYMENT"}:
            reimbursements = sa.Table(
                "expense_reimbursement", sa.MetaData(), autoload_with=connection
            )
            statement = sa.select(table.c.reimbursement_id, table.c.status).where(table.c[key] == value).limit(1)
            child = connection.execute(statement).mappings().first()
            if child is None:
                return None
            if child["reimbursement_id"] is None:
                locked = statement.with_for_update(of=table) if lock and connection.dialect.name == "postgresql" else statement
                current = connection.execute(locked).mappings().first()
                return bool(current and current["reimbursement_id"] is None and current["status"] == "未匹配")
            parent_statement = sa.select(reimbursements.c.status).where(
                reimbursements.c.id == child["reimbursement_id"]
            )
            if lock and connection.dialect.name == "postgresql":
                parent_statement = parent_statement.with_for_update(of=reimbursements)
            parent_status = connection.scalar(parent_statement)
            locked = statement.with_for_update(of=table) if lock and connection.dialect.name == "postgresql" else statement
            current = connection.execute(locked).mappings().first()
            return bool(
                parent_status == "草稿" and current
                and current["reimbursement_id"] == child["reimbursement_id"]
                and current["status"] == "已匹配"
            )
        has_write_status = object_type in {"PROPOSAL", "STANDARD", "TEMPLATE", "EXPENSE"} and "status" in table.c
        columns = [table.c.status] if has_write_status else [sa.literal("WRITABLE")]
        statement = sa.select(*columns).select_from(table).where(table.c[key] == value).limit(1)
        if lock and connection.dialect.name == "postgresql":
            statement = statement.with_for_update(of=table)
        status = connection.execute(statement).scalar_one_or_none()
        if status is None:
            return None
        if object_type == "PROPOSAL":
            return status not in {"ESTABLISHED", "REJECTED"}
        if object_type in {"STANDARD", "TEMPLATE"}:
            return status != "ARCHIVED"
        if object_type == "EXPENSE":
            # Older FileService contract fixtures predate the finance status
            # column; only the current finance schema can enforce draft state.
            return status == "草稿" if "status" in table.c else True
        return True

    def lock_expense_for_document_generation(self, connection: Connection, object_id: str):
        if "EXPENSE" not in self._object_tables:
            if not self.object_exists(connection, "EXPENSE", object_id):
                return None
        table, key = self._object_tables["EXPENSE"]
        try:
            value = int(object_id)
        except (TypeError, ValueError):
            return None
        statement = sa.select(table.c.status).where(table.c[key] == value)
        if connection.dialect.name == "postgresql":
            statement = statement.with_for_update(of=table)
        return connection.scalar(statement)

    def create_file(
        self,
        connection: Connection,
        *,
        file_id: uuid.UUID,
        business_id: str,
        original_name: str,
        media_type: str,
        actor_user_id: int,
    ) -> None:
        connection.execute(
            self.files.insert().values(
                **self._row_values(
                    self.files,
                    id=self._id(connection, file_id),
                    business_id=business_id,
                    original_name=original_name,
                    media_type=media_type,
                    status="ACTIVE",
                    created_by=actor_user_id,
                    updated_by=actor_user_id,
                    version=1,
                )
            )
        )

    def create_version(
        self,
        connection: Connection,
        *,
        version_id: uuid.UUID,
        file_id: uuid.UUID | str,
        version_no: int,
        storage_path: str,
        sha256: str,
        size_bytes: int,
        media_type: str,
        actor_user_id: int,
    ) -> None:
        connection.execute(
            self.versions.insert().values(
                **self._row_values(
                    self.versions,
                    id=self._id(connection, version_id),
                    file_id=self._id(connection, file_id),
                    version_no=version_no,
                    storage_path=storage_path,
                    sha256=sha256,
                    size_bytes=size_bytes,
                    media_type=media_type,
                    created_by=actor_user_id,
                    updated_by=actor_user_id,
                    version=1,
                )
            )
        )

    def link_object(
        self,
        connection: Connection,
        *,
        link_id: uuid.UUID,
        object_type: str,
        object_id: str,
        file_id: uuid.UUID | str,
        actor_user_id: int,
        purpose: str | None = None,
    ) -> None:
        connection.execute(
            self.links.insert().values(
                **self._row_values(
                    self.links,
                    id=self._id(connection, link_id),
                    object_type=object_type,
                    object_id=str(object_id),
                    file_id=self._id(connection, file_id),
                    purpose=purpose,
                    created_by=actor_user_id,
                    updated_by=actor_user_id,
                    version=1,
                )
            )
        )

    def has_multiple_links(self, connection: Connection, *, file_id: str) -> bool:
        rows = connection.execute(sa.select(self.links.c.id).where(
            self.links.c.file_id == self._id(connection, file_id),
            self.live_link_condition(),
        ).limit(2)).all()
        return len(rows) > 1

    def live_link_condition(self):
        return sa.or_(self.links.c.object_type != "PROJECT", self.links.c.purpose.is_(None),
                      ~self.links.c.purpose.startswith(DELETED_PROJECT_PATH_PREFIX, autoescape=True))

    def list_deleted_project_paths(self, connection: Connection, *, project_id: str):
        purposes = connection.execute(sa.select(self.links.c.purpose).where(
            self.links.c.object_type == "PROJECT", self.links.c.object_id == str(project_id),
            self.links.c.purpose.startswith(DELETED_PROJECT_PATH_PREFIX, autoescape=True),
        ).distinct()).scalars()
        return [value[len(DELETED_PROJECT_PATH_PREFIX):] for value in purposes]

    def delete_project_path(self, connection: Connection, *, project_id: str, file_id: str,
                            filepath: str, actor_user_id: int):
        values = {"purpose": DELETED_PROJECT_PATH_PREFIX + filepath,
                  "updated_by": actor_user_id, "version": self.links.c.version + 1}
        if "updated_at" in self.links.c:
            values["updated_at"] = sa.func.now()
        result = connection.execute(self.links.update().where(
            self.links.c.object_type == "PROJECT", self.links.c.object_id == str(project_id),
            self.links.c.file_id == self._id(connection, file_id),
            self.links.c.purpose == PROJECT_PATH_PREFIX + filepath,
        ).values(**values))
        if result.rowcount != 1:
            raise RuntimeError("project file link changed during delete")

    def has_live_links(self, connection: Connection, *, file_id: str):
        return connection.execute(sa.select(self.links.c.id).where(
            self.links.c.file_id == self._id(connection, file_id), self.live_link_condition(),
        ).limit(1)).first() is not None

    def rename_project_path(self, connection: Connection, *, project_id: str,
                            file_id: str, purpose: str, new_purpose: str,
                            original_name: str, actor_user_id: int):
        file_values = {"original_name": original_name, "updated_by": actor_user_id}
        link_values = {"purpose": new_purpose, "updated_by": actor_user_id,
                       "version": self.links.c.version + 1}
        if "updated_at" in self.files.c:
            file_values["updated_at"] = sa.func.now()
        if "updated_at" in self.links.c:
            link_values["updated_at"] = sa.func.now()
        connection.execute(self.files.update().where(
            self.files.c.id == self._id(connection, file_id),
        ).values(**file_values))
        changed = connection.execute(self.links.update().where(
            self.links.c.object_type == "PROJECT", self.links.c.object_id == str(project_id),
            self.links.c.file_id == self._id(connection, file_id), self.links.c.purpose == purpose,
        ).values(**link_values))
        if changed.rowcount != 1:
            raise RuntimeError("project file link changed during rename")

    def list_project_paths(self, connection: Connection, *, project_id: str):
        statement = sa.select(
            self.files, self.links.c.purpose, self.versions.c.size_bytes,
        ).join(self.links, self.links.c.file_id == self.files.c.id).join(
            self.versions, sa.and_(self.versions.c.file_id == self.files.c.id,
                                  self.versions.c.version_no == self.files.c.version),
        ).where(
            self.links.c.object_type == "PROJECT", self.links.c.object_id == str(project_id),
            self.links.c.purpose.startswith("PROJECT_TREE:"), self.files.c.status == "ACTIVE",
        ).order_by(self.links.c.purpose)
        return list(connection.execute(statement).mappings())

    def get_project_path_file(self, connection: Connection, *, project_id: str, purpose: str):
        # The caller holds the project write lock, including before the first link exists.
        statement = sa.select(self.files).join(
            self.links, self.links.c.file_id == self.files.c.id,
        ).where(
            self.links.c.object_type == "PROJECT",
            self.links.c.object_id == str(project_id),
            self.links.c.purpose == purpose,
            self.files.c.status == "ACTIVE",
        )
        return connection.execute(statement).mappings().one_or_none()

    def get_linked_file(
        self,
        connection: Connection,
        *,
        file_id: str,
        object_type: str,
        object_id: str,
        lock: bool = False,
    ):
        statement = (
            sa.select(self.files)
            .join(self.links, self.links.c.file_id == self.files.c.id)
            .where(
                self.files.c.id == self._id(connection, file_id),
                self.links.c.object_type == object_type,
                self.links.c.object_id == str(object_id),
                self.live_link_condition(),
            )
        )
        if lock and connection.dialect.name == "postgresql":
            statement = statement.with_for_update(of=self.files)
        return connection.execute(statement).mappings().first()

    def get_version(self, connection: Connection, *, file_id: str, version_no: int):
        statement = sa.select(self.versions).where(
            self.versions.c.file_id == self._id(connection, file_id),
            self.versions.c.version_no == version_no,
        )
        return connection.execute(statement).mappings().first()

    def list_versions(self, connection: Connection, *, file_id: str):
        statement = sa.select(self.versions).where(
            self.versions.c.file_id == self._id(connection, file_id)
        ).order_by(self.versions.c.version_no.desc())
        return list(connection.execute(statement).mappings())

    def count_versions(self, connection: Connection, *, file_id: str) -> int:
        statement = sa.select(sa.func.count()).select_from(self.versions).where(
            self.versions.c.file_id == self._id(connection, file_id)
        )
        return int(connection.scalar(statement) or 0)

    def archive_file(self, connection: Connection, *, file_id: str,
                     expected_version: int, actor_user_id: int) -> bool:
        """Change lifecycle status without inventing a new content version."""
        values = {"status": "ARCHIVED", "updated_by": actor_user_id}
        if "updated_at" in self.files.c:
            values["updated_at"] = sa.func.now()
        result = connection.execute(self.files.update().where(
            self.files.c.id == self._id(connection, file_id),
            self.files.c.version == expected_version,
            self.files.c.status == "ACTIVE",
        ).values(**values))
        return result.rowcount == 1

    def bump_file(
        self,
        connection: Connection,
        *,
        file_id: str,
        expected_version: int,
        actor_user_id: int,
        original_name: str | None = None,
        media_type: str | None = None,
        status: str | None = None,
    ) -> bool:
        values = {"version": expected_version + 1, "updated_by": actor_user_id}
        if "updated_at" in self.files.c:
            values["updated_at"] = sa.func.now()
        if original_name is not None:
            values["original_name"] = original_name
        if media_type is not None:
            values["media_type"] = media_type
        if status is not None:
            values["status"] = status
        result = connection.execute(
            self.files.update()
            .where(
                self.files.c.id == self._id(connection, file_id),
                self.files.c.version == expected_version,
            )
            .values(**values)
        )
        return result.rowcount == 1

    def list_for_object(self, connection: Connection, *, object_type: str, object_id: str):
        statement = (
            sa.select(self.files)
            .join(self.links, self.links.c.file_id == self.files.c.id)
            .where(
                self.links.c.object_type == object_type,
                self.links.c.object_id == str(object_id),
                self.files.c.status == "ACTIVE",
                self.live_link_condition(),
            )
            .order_by(self.files.c.business_id)
        )
        return list(connection.execute(statement).mappings())
