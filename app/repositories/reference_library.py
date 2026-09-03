from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.engine import Engine


class ReferenceLibraryRepository:
    """SQLAlchemy boundary for retained standards and template-file metadata."""

    table_names = frozenset({"standards", "reference_template_folders", "reference_template_items"})

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        metadata = sa.MetaData()
        self.standards = sa.Table("standards", metadata, autoload_with=engine)
        self.folders = sa.Table("reference_template_folders", metadata, autoload_with=engine)
        self.items = sa.Table("reference_template_items", metadata, autoload_with=engine)

    @staticmethod
    def _values(table: sa.Table, **values):
        return {key: value for key, value in values.items() if key in table.c}

    @staticmethod
    def _bump(table: sa.Table, **values):
        if "version" in table.c:
            values["version"] = table.c.version + 1
        return ReferenceLibraryRepository._values(table, **values)

    @staticmethod
    def _mapping(row):
        return dict(row) if row is not None else None

    def list_standards(self, *, category: str | None = None, active_only: bool = True) -> list[dict]:
        statement = sa.select(self.standards)
        if active_only and "status" in self.standards.c:
            statement = statement.where(self.standards.c.status == "ACTIVE")
        if category:
            statement = statement.where(self.standards.c.category == category)
        statement = statement.order_by(self.standards.c.name)
        with self.engine.connect() as connection:
            return [dict(row) for row in connection.execute(statement).mappings()]

    def get_standard(self, doc_id: str, *, active_only: bool = True) -> dict | None:
        statement = sa.select(self.standards).where(self.standards.c.doc_id == doc_id).limit(2)
        if active_only and "status" in self.standards.c:
            statement = statement.where(self.standards.c.status == "ACTIVE")
        with self.engine.connect() as connection:
            rows = list(connection.execute(statement).mappings())
        return self._mapping(rows[0]) if len(rows) == 1 else None

    def has_active_standard_name(self, name: str) -> bool:
        statement = sa.select(sa.literal(1)).select_from(self.standards).where(
            sa.func.lower(self.standards.c.name) == name.casefold()
        ).limit(1)
        if "status" in self.standards.c:
            statement = statement.where(self.standards.c.status == "ACTIVE")
        with self.engine.connect() as connection:
            return connection.execute(statement).scalar_one_or_none() is not None

    def insert_standard(self, writer, *, doc_id: str, name: str, category: str, file_type: str, actor_user_id: int) -> None:
        writer.execute(self.standards.insert().values(**self._values(
            self.standards, doc_id=doc_id, name=name, category=category,
            file_type=file_type, uploader=str(actor_user_id), status="ACTIVE",
            created_by=actor_user_id, updated_by=actor_user_id, version=1,
        )))

    def archive_standard(self, connection, doc_id: str, *, actor_user_id: int) -> bool:
        result = connection.execute(self.standards.update().where(
            self.standards.c.doc_id == doc_id,
            self.standards.c.status == "ACTIVE",
        ).values(**self._bump(self.standards, status="ARCHIVED", updated_by=actor_user_id)))
        return result.rowcount == 1

    def active_folders(self) -> list[dict]:
        statement = sa.select(self.folders).where(self.folders.c.status == "ACTIVE").order_by(self.folders.c.name)
        with self.engine.connect() as connection:
            return [dict(row) for row in connection.execute(statement).mappings()]

    def all_folders(self) -> list[dict]:
        with self.engine.connect() as connection:
            return [dict(row) for row in connection.execute(sa.select(self.folders)).mappings()]

    def active_items(self) -> list[dict]:
        statement = sa.select(self.items).where(self.items.c.status == "ACTIVE").order_by(self.items.c.display_name)
        with self.engine.connect() as connection:
            return [dict(row) for row in connection.execute(statement).mappings()]

    def all_items(self) -> list[dict]:
        with self.engine.connect() as connection:
            return [dict(row) for row in connection.execute(sa.select(self.items)).mappings()]

    def create_folder(self, connection, *, folder_id: str, parent_id: str | None, name: str, actor_user_id: int) -> None:
        connection.execute(self.folders.insert().values(**self._values(
            self.folders, id=folder_id, parent_id=parent_id, name=name, status="ACTIVE",
            created_by=actor_user_id, updated_by=actor_user_id, version=1,
        )))

    def rename_folder(self, connection, folder_id: str, *, name: str, actor_user_id: int) -> bool:
        result = connection.execute(self.folders.update().where(
            self.folders.c.id == folder_id, self.folders.c.status == "ACTIVE",
        ).values(**self._bump(self.folders, name=name, updated_by=actor_user_id)))
        return result.rowcount == 1

    def archive_folder_tree(self, connection, folder_id: str, *, actor_user_id: int) -> tuple[list[str], list[str]]:
        statement = sa.select(self.folders.c.id, self.folders.c.parent_id, self.folders.c.status)
        if connection.dialect.name == "postgresql":
            statement = statement.with_for_update(of=self.folders)
        rows = list(connection.execute(statement).mappings())
        by_parent: dict[str, list[str]] = {}
        status = {}
        for row in rows:
            status[str(row["id"])] = row["status"]
            if row["parent_id"] is not None:
                by_parent.setdefault(str(row["parent_id"]), []).append(str(row["id"]))
        if status.get(folder_id) != "ACTIVE":
            return [], []
        folder_ids, pending = [], [folder_id]
        while pending:
            current = pending.pop()
            folder_ids.append(current)
            pending.extend(by_parent.get(current, ()))
        connection.execute(self.folders.update().where(self.folders.c.id.in_(folder_ids)).values(**self._bump(
            self.folders, status="ARCHIVED", updated_by=actor_user_id,
        )))
        item_rows = list(connection.execute(sa.select(self.items.c.template_id).where(
            self.items.c.folder_id.in_(folder_ids), self.items.c.status == "ACTIVE",
        )).scalars())
        connection.execute(self.items.update().where(self.items.c.folder_id.in_(folder_ids), self.items.c.status == "ACTIVE").values(**self._bump(
            self.items, status="ARCHIVED", updated_by=actor_user_id,
        )))
        return folder_ids, [str(value) for value in item_rows]

    def insert_template(self, writer, *, template_id: str, item_id: str, folder_id: str | None, display_name: str, actor_user_id: int) -> None:
        writer.execute(self.items.insert().values(**self._values(
            self.items, id=item_id, template_id=template_id, folder_id=folder_id,
            display_name=display_name, status="ACTIVE", created_by=actor_user_id,
            updated_by=actor_user_id, version=1,
        )))

    def update_template_name(self, connection, template_id: str, *, display_name: str, actor_user_id: int) -> bool:
        result = connection.execute(self.items.update().where(
            self.items.c.template_id == template_id, self.items.c.status == "ACTIVE",
        ).values(**self._bump(self.items, display_name=display_name, updated_by=actor_user_id)))
        return result.rowcount == 1

    def archive_template(self, connection, template_id: str, *, actor_user_id: int) -> bool:
        result = connection.execute(self.items.update().where(
            self.items.c.template_id == template_id, self.items.c.status == "ACTIVE",
        ).values(**self._bump(self.items, status="ARCHIVED", updated_by=actor_user_id)))
        return result.rowcount == 1

    @staticmethod
    def new_id() -> str:
        return str(uuid.uuid4())
