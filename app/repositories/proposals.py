from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

from app.repositories.base import optimistic_update, paginate


class ProposalsRepository:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        metadata = sa.MetaData()
        self.proposals = sa.Table("proposals", metadata, autoload_with=engine)
        self.argumentations = sa.Table(
            "proposal_argumentations", metadata, autoload_with=engine
        )
        self.decisions = sa.Table("proposal_decisions", metadata, autoload_with=engine)

    @staticmethod
    def _uuid(connection: Connection, value):
        if connection.dialect.name == "postgresql":
            return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
        return str(value)

    @staticmethod
    def _values(table: sa.Table, values: dict) -> dict:
        return {key: value for key, value in values.items() if key in table.c}

    def insert_proposal(self, connection: Connection, values: dict) -> None:
        payload = dict(values)
        payload["id"] = self._uuid(connection, payload["id"])
        connection.execute(self.proposals.insert().values(**self._values(self.proposals, payload)))

    def get(self, connection: Connection, business_id: str, *, lock: bool = False):
        statement = sa.select(self.proposals).where(
            self.proposals.c.business_id == business_id
        )
        if lock and connection.dialect.name == "postgresql":
            statement = statement.with_for_update(of=self.proposals)
        return connection.execute(statement).mappings().first()

    def list(
        self,
        connection: Connection,
        *,
        page: int,
        page_size: int,
        status: str | None = None,
        source_type: str | None = None,
        keyword: str | None = None,
        updated_after=None,
    ) -> tuple[list[dict], int]:
        filters = []
        if status:
            filters.append(self.proposals.c.status == status)
        if source_type:
            filters.append(self.proposals.c.source_type == source_type)
        if keyword:
            escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{escaped}%"
            filters.append(
                sa.or_(
                    self.proposals.c.title.ilike(pattern, escape="\\"),
                    self.proposals.c.business_id.ilike(pattern, escape="\\"),
                )
            )
        if updated_after is not None:
            filters.append(self.proposals.c.updated_at >= updated_after)
        base = sa.select(
            self.proposals.c.id,
            self.proposals.c.business_id,
            self.proposals.c.title,
            self.proposals.c.source_type,
            self.proposals.c.status,
            self.proposals.c.updated_by,
            self.proposals.c.updated_at,
            self.proposals.c.version,
        ).where(*filters)
        total = connection.scalar(
            sa.select(sa.func.count()).select_from(base.subquery())
        )
        statement = paginate(
            base.order_by(self.proposals.c.updated_at.desc(), self.proposals.c.id.desc()),
            page=page,
            page_size=page_size,
        )
        rows = [dict(row) for row in connection.execute(statement).mappings()]
        return rows, int(total or 0)

    def update(
        self,
        connection: Connection,
        *,
        proposal_id,
        expected_version: int,
        values: dict,
    ) -> int:
        return optimistic_update(
            connection,
            self.proposals,
            record_id=self._uuid(connection, proposal_id),
            expected_version=expected_version,
            values=self._values(self.proposals, values),
        )

    def insert_argumentation(self, connection: Connection, values: dict) -> None:
        payload = dict(values)
        payload["id"] = self._uuid(connection, payload["id"])
        payload["proposal_id"] = self._uuid(connection, payload["proposal_id"])
        connection.execute(
            self.argumentations.insert().values(
                **self._values(self.argumentations, payload)
            )
        )

    def list_argumentations(
        self, connection: Connection, *, proposal_id, page: int, page_size: int
    ) -> tuple[list[dict], int]:
        proposal_id = self._uuid(connection, proposal_id)
        base = sa.select(self.argumentations).where(
            self.argumentations.c.proposal_id == proposal_id
        )
        total = connection.scalar(
            sa.select(sa.func.count()).select_from(base.subquery())
        )
        rows = connection.execute(
            paginate(
                base.order_by(
                    self.argumentations.c.created_at.desc(),
                    self.argumentations.c.id.desc(),
                ),
                page=page,
                page_size=page_size,
            )
        ).mappings()
        return [dict(row) for row in rows], int(total or 0)

    def get_decision_by_key(self, connection: Connection, key: str):
        return connection.execute(
            sa.select(self.decisions).where(self.decisions.c.idempotency_key == key)
        ).mappings().first()

    def insert_decision(self, connection: Connection, values: dict) -> None:
        payload = dict(values)
        payload["id"] = self._uuid(connection, payload["id"])
        payload["proposal_id"] = self._uuid(connection, payload["proposal_id"])
        connection.execute(
            self.decisions.insert().values(**self._values(self.decisions, payload))
        )

    def list_decisions(
        self,
        connection: Connection,
        *,
        proposal_id,
        page: int,
        page_size: int,
    ) -> tuple[list[dict], int]:
        base = sa.select(self.decisions).where(
            self.decisions.c.proposal_id == self._uuid(connection, proposal_id)
        )
        total = connection.scalar(
            sa.select(sa.func.count()).select_from(base.subquery())
        )
        rows = connection.execute(
            paginate(
                base.order_by(
                    self.decisions.c.created_at.desc(), self.decisions.c.id.desc()
                ),
                page=page,
                page_size=page_size,
            )
        ).mappings()
        return [dict(row) for row in rows], int(total or 0)
