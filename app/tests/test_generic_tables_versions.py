from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.pool import StaticPool

from app.repositories.generic_tables import GenericTablesRepository
from app.services.generic_tables import GenericTablesService
from app.tests.test_generic_tables_postgres import _schema


def test_snapshot_is_immutable_and_rollback_creates_new_current():
    engine = sa.create_engine(
        "sqlite+pysqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    _schema(engine)
    service = GenericTablesService(GenericTablesRepository(engine))
    table_id = service.create("版本表", "", "张老师")
    first = service.get_by_id(table_id)["current_version_id"]
    service.upsert_column(first, "name", "名称", "text")
    service.upsert_row(first, "row1", {"name": "A"})
    snapshot, current = service.save_snapshot(table_id, first, None, "", "李老师")
    service.upsert_row(current, "row1", {"name": "B"})
    assert service.get_rows(snapshot)[0]["row_data"] == {"name": "A"}
    restored = service.rollback_to(table_id, snapshot, "王老师")
    assert restored not in {snapshot, current}
    assert service.get_by_id(table_id)["current_version_id"] == restored
    assert service.get_rows(restored)[0]["row_data"] == {"name": "A"}
