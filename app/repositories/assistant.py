from __future__ import annotations

import uuid

import sqlalchemy as sa

from app.repositories.base import optimistic_update


class AssistantRepository:
    def __init__(self, engine) -> None:
        self.engine = engine
        metadata = sa.MetaData()
        self.proposals = sa.Table("proposals", metadata, autoload_with=engine)
        self.drafts = sa.Table("proposal_ai_drafts", metadata, autoload_with=engine)

    @staticmethod
    def _id(connection, value):
        if connection.dialect.name == "postgresql":
            try:
                return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
            except (TypeError, ValueError, AttributeError):
                return None
        return str(value)

    @staticmethod
    def _values(table, values):
        return {key: value for key, value in values.items() if key in table.c}

    def get_proposal(self, connection, business_id: str, *, lock: bool = False):
        statement = sa.select(self.proposals).where(
            self.proposals.c.business_id == business_id
        )
        if lock and connection.dialect.name == "postgresql":
            statement = statement.with_for_update(of=self.proposals)
        return connection.execute(statement).mappings().first()

    def insert_draft(self, connection, values: dict) -> None:
        payload = dict(values)
        payload["id"] = self._id(connection, payload["id"])
        payload["proposal_id"] = self._id(connection, payload["proposal_id"])
        connection.execute(
            self.drafts.insert().values(**self._values(self.drafts, payload))
        )

    def get_draft(self, connection, draft_id, proposal_id, *, lock: bool = False):
        statement = sa.select(self.drafts).where(
            self.drafts.c.id == self._id(connection, draft_id),
            self.drafts.c.proposal_id == self._id(connection, proposal_id),
        )
        if lock and connection.dialect.name == "postgresql":
            statement = statement.with_for_update(of=self.drafts)
        return connection.execute(statement).mappings().first()

    def update_proposal(self, connection, *, proposal_id, expected_version, values):
        return optimistic_update(
            connection,
            self.proposals,
            record_id=self._id(connection, proposal_id),
            expected_version=expected_version,
            values=self._values(self.proposals, values),
        )

    def mark_applied(self, connection, *, draft_id, accepted_fields, actor_user_id) -> bool:
        values = {
            "status": "APPLIED",
            "accepted_fields": accepted_fields,
            "updated_by": actor_user_id,
            "version": self.drafts.c.version + 1,
        }
        if "updated_at" in self.drafts.c:
            values["updated_at"] = sa.func.now()
        result = connection.execute(
            self.drafts.update()
            .where(
                self.drafts.c.id == self._id(connection, draft_id),
                self.drafts.c.status == "READY",
            )
            .values(**values)
        )
        return result.rowcount == 1

    def list_drafts_for_test(self) -> list[dict]:
        with self.engine.connect() as connection:
            return [dict(row) for row in connection.execute(sa.select(self.drafts)).mappings()]
