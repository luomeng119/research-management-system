from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

from app.repositories.base import optimistic_update, paginate


CATEGORY_TABLES = {
    "GENERAL_RESEARCH": "projects",
    "SECURITY_CONFIDENTIALITY": "security_projects",
    "CRYPTO_APPLICATION": "crypto_projects",
}


class ProjectsRepository:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        metadata = sa.MetaData()
        self.proposals = sa.Table("proposals", metadata, autoload_with=engine)
        self.decisions = sa.Table("proposal_decisions", metadata, autoload_with=engine)
        self.registry = sa.Table("project_registry", metadata, autoload_with=engine)
        self.category_tables = {
            category: sa.Table(name, metadata, autoload_with=engine)
            for category, name in CATEGORY_TABLES.items()
        }
        inspector = sa.inspect(engine)
        self.progress = (
            sa.Table("project_progress", metadata, autoload_with=engine)
            if inspector.has_table("project_progress") else None
        )
        self.changes = (
            sa.Table("project_changes", metadata, autoload_with=engine)
            if inspector.has_table("project_changes") else None
        )
        self.outputs = (
            sa.Table("project_outputs", metadata, autoload_with=engine)
            if inspector.has_table("project_outputs") else None
        )
        self.closures = (
            sa.Table("project_closures", metadata, autoload_with=engine)
            if inspector.has_table("project_closures") else None
        )
        self.object_files = (
            sa.Table("object_files", metadata, autoload_with=engine)
            if inspector.has_table("object_files") else None
        )

    @staticmethod
    def _id(connection: Connection, value):
        if connection.dialect.name == "postgresql":
            return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
        return str(value)

    @staticmethod
    def _values(table: sa.Table, values: dict) -> dict:
        return {key: value for key, value in values.items() if key in table.c}

    def get_proposal(self, connection: Connection, business_id: str, *, lock=False):
        statement = sa.select(self.proposals).where(
            self.proposals.c.business_id == business_id
        )
        if lock and connection.dialect.name == "postgresql":
            statement = statement.with_for_update(of=self.proposals)
        return connection.execute(statement).mappings().first()

    def update_proposal(
        self, connection: Connection, *, proposal_id, expected_version: int, values: dict
    ) -> int:
        return optimistic_update(
            connection,
            self.proposals,
            record_id=self._id(connection, proposal_id),
            expected_version=expected_version,
            values=self._values(self.proposals, values),
        )

    def get_decision_by_key(self, connection: Connection, key: str):
        return connection.execute(
            sa.select(self.decisions).where(self.decisions.c.idempotency_key == key)
        ).mappings().first()

    def insert_decision(self, connection: Connection, values: dict) -> None:
        payload = dict(values)
        payload["id"] = self._id(connection, payload["id"])
        payload["proposal_id"] = self._id(connection, payload["proposal_id"])
        connection.execute(
            self.decisions.insert().values(**self._values(self.decisions, payload))
        )

    def insert_registry(self, connection: Connection, values: dict) -> None:
        payload = dict(values)
        payload["id"] = self._id(connection, payload["id"])
        if payload.get("proposal_id") is not None:
            payload["proposal_id"] = self._id(connection, payload["proposal_id"])
        connection.execute(
            self.registry.insert().values(**self._values(self.registry, payload))
        )

    def insert_category_project(
        self, connection: Connection, *, category: str, values: dict
    ) -> None:
        table = self.category_tables[category]
        payload = dict(values)
        payload["registry_id"] = self._id(connection, payload["registry_id"])
        connection.execute(table.insert().values(**self._values(table, payload)))

    def get_registry_by_proposal(self, connection: Connection, proposal_id):
        return connection.execute(
            sa.select(self.registry).where(
                self.registry.c.proposal_id == self._id(connection, proposal_id)
            )
        ).mappings().first()

    def get_registry(self, connection: Connection, registry_id):
        return connection.execute(
            sa.select(self.registry).where(
                self.registry.c.id == self._id(connection, registry_id)
            )
        ).mappings().first()

    def get_registry_for_update(self, connection: Connection, registry_id):
        statement = sa.select(self.registry).where(
            self.registry.c.id == self._id(connection, registry_id)
        )
        if connection.dialect.name == "postgresql":
            statement = statement.with_for_update(of=self.registry)
        return connection.execute(statement).mappings().first()

    def get_registry_by_category_business_id(
        self, connection: Connection, *, category: str, business_id: str, lock=False
    ):
        statement = sa.select(self.registry).where(
            self.registry.c.category == category,
            self.registry.c.business_id == business_id,
        )
        if lock and connection.dialect.name == "postgresql":
            statement = statement.with_for_update(of=self.registry)
        return connection.execute(statement).mappings().first()

    def get_category_project(self, connection: Connection, registry: dict):
        table = self.category_tables[registry["category"]]
        return connection.execute(
            sa.select(table).where(
                table.c.registry_id == self._id(connection, registry["id"])
            )
        ).mappings().first()

    def get_category_projects(
        self, connection: Connection, registries: list[dict]
    ) -> dict:
        projects = {}
        for category, table in self.category_tables.items():
            registry_ids = [
                self._id(connection, registry["id"])
                for registry in registries if registry["category"] == category
            ]
            if not registry_ids:
                continue
            rows = connection.execute(
                sa.select(table).where(table.c.registry_id.in_(registry_ids))
            ).mappings()
            projects.update({row["registry_id"]: dict(row) for row in rows})
        return projects

    def get_project_ref_by_proposal(self, connection: Connection, proposal_id):
        registry = self.get_registry_by_proposal(connection, proposal_id)
        if registry is None:
            return None, None
        return registry, self.get_category_project(connection, registry)

    def list_registry(
        self,
        connection: Connection,
        *,
        page: int,
        page_size: int,
        category: str | None = None,
        status: str | None = None,
    ) -> tuple[list[dict], int]:
        filters = []
        if category:
            filters.append(self.registry.c.category == category)
        if status:
            filters.append(self.registry.c.status == status)
        base = sa.select(self.registry).where(*filters)
        total = connection.scalar(sa.select(sa.func.count()).select_from(base.subquery()))
        rows = connection.execute(
            paginate(
                base.order_by(self.registry.c.updated_at.desc(), self.registry.c.id.desc()),
                page=page,
                page_size=page_size,
            )
        ).mappings()
        return [dict(row) for row in rows], int(total or 0)

    def list_category_projects(
        self,
        connection: Connection,
        *,
        category: str,
        page: int,
        page_size: int,
        status: str | None = None,
        keyword: str | None = None,
    ) -> tuple[list[dict], int]:
        table = self.category_tables[category]
        filters = [self.registry.c.category == category]
        if status:
            filters.append(table.c.status == status)
        if keyword:
            escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{escaped}%"
            filters.append(sa.or_(
                table.c.project_id.ilike(pattern, escape="\\"),
                table.c.name.ilike(pattern, escape="\\"),
                table.c.leader.ilike(pattern, escape="\\"),
            ))
        base = sa.select(table).join(
            self.registry, table.c.registry_id == self.registry.c.id
        ).where(*filters)
        total = connection.scalar(sa.select(sa.func.count()).select_from(base.subquery()))
        order_column = table.c.created_at if "created_at" in table.c else table.c.id
        rows = connection.execute(
            paginate(base.order_by(order_column.desc(), table.c.id.desc()), page=page, page_size=page_size)
        ).mappings()
        return [dict(row) for row in rows], int(total or 0)

    def update_category_project(
        self, connection: Connection, *, category: str, registry_id, values: dict
    ) -> int:
        table = self.category_tables[category]
        result = connection.execute(
            table.update().where(
                table.c.registry_id == self._id(connection, registry_id)
            ).values(**self._values(table, values))
        )
        return result.rowcount

    def update_registry(self, connection: Connection, *, registry_id, values: dict) -> int:
        result = connection.execute(
            self.registry.update().where(
                self.registry.c.id == self._id(connection, registry_id)
            ).values(**self._values(self.registry, values))
        )
        return result.rowcount

    def transition_registry(
        self, connection: Connection, *, registry_id, expected_version: int, values: dict
    ) -> int:
        return optimistic_update(
            connection,
            self.registry,
            record_id=self._id(connection, registry_id),
            expected_version=expected_version,
            values=self._values(self.registry, values),
        )

    def insert_process_record(
        self, connection: Connection, *, record_type: str, values: dict
    ) -> None:
        table = {
            "PROGRESS": self.progress,
            "CHANGE": self.changes,
            "OUTPUT": self.outputs,
            "CLOSURE": self.closures,
        }[record_type]
        payload = dict(values)
        payload["id"] = self._id(connection, payload["id"])
        payload["project_registry_id"] = self._id(
            connection, payload["project_registry_id"]
        )
        connection.execute(table.insert().values(**self._values(table, payload)))

    def list_process_records(
        self, connection: Connection, *, record_type: str, registry_id,
        limit: int | None = None, offset: int = 0,
    ) -> list[dict]:
        table = {
            "PROGRESS": self.progress,
            "CHANGE": self.changes,
            "OUTPUT": self.outputs,
        }[record_type]
        order_column = (
            table.c.recorded_at if record_type == "PROGRESS" else table.c.created_at
        )
        statement = (
            sa.select(table)
            .where(
                table.c.project_registry_id == self._id(connection, registry_id)
            )
            .order_by(order_column.desc(), table.c.id.desc())
            .offset(offset)
        )
        if limit is not None:
            statement = statement.limit(limit)
        rows = connection.execute(statement).mappings()
        return [dict(row) for row in rows]

    def count_process_records_by_type(
        self, connection: Connection, *, record_type: str, registry_id
    ) -> int:
        table = {
            "PROGRESS": self.progress,
            "CHANGE": self.changes,
            "OUTPUT": self.outputs,
        }[record_type]
        return int(connection.scalar(
            sa.select(sa.func.count()).select_from(table).where(
                table.c.project_registry_id == self._id(connection, registry_id)
            )
        ) or 0)

    def get_closure(self, connection: Connection, registry_id):
        return connection.execute(
            sa.select(self.closures).where(
                self.closures.c.project_registry_id
                == self._id(connection, registry_id)
            )
        ).mappings().first()

    def update_closure(
        self, connection: Connection, *, closure_id, expected_version: int, values: dict
    ) -> int:
        return optimistic_update(
            connection,
            self.closures,
            record_id=self._id(connection, closure_id),
            expected_version=expected_version,
            values=self._values(self.closures, values),
        )

    def count_process_records(self, connection: Connection, registry_id) -> int:
        project_id = self._id(connection, registry_id)
        tables = (self.progress, self.changes, self.outputs, self.closures)
        return sum(
            int(connection.scalar(
                sa.select(sa.func.count()).select_from(table).where(
                    table.c.project_registry_id == project_id
                )
            ) or 0)
            for table in tables if table is not None
        )

    def count_project_files(self, connection: Connection, business_id: str) -> int:
        if self.object_files is None:
            return 0
        return int(connection.scalar(
            sa.select(sa.func.count()).select_from(self.object_files).where(
                self.object_files.c.object_type == "PROJECT",
                self.object_files.c.object_id == business_id,
            )
        ) or 0)

    def delete_category_project(
        self, connection: Connection, *, category: str, registry_id
    ) -> int:
        table = self.category_tables[category]
        result = connection.execute(table.delete().where(
            table.c.registry_id == self._id(connection, registry_id)
        ))
        return result.rowcount

    def delete_registry(self, connection: Connection, *, registry_id) -> int:
        result = connection.execute(self.registry.delete().where(
            self.registry.c.id == self._id(connection, registry_id)
        ))
        return result.rowcount
