from __future__ import annotations

import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine


class AuditRepository:
    """Append-only application boundary for ``audit_events``."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.table = sa.Table("audit_events", sa.MetaData(), autoload_with=engine)

    def insert(self, connection: Connection, event: dict):
        event_id = uuid.uuid4()
        values = dict(event)
        values.setdefault(
            "id", event_id if connection.dialect.name == "postgresql" else str(event_id)
        )
        values.setdefault("created_at", datetime.now(timezone.utc))
        connection.execute(self.table.insert().values(**values))
        return str(event_id)
