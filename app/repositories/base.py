from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection
from sqlalchemy.sql import Select


class OptimisticLockConflict(RuntimeError):
    """The stored row changed after the caller read its version."""


def paginate(
    statement: Select[Any],
    *,
    page: int,
    page_size: int,
    max_page_size: int = 100,
) -> Select[Any]:
    if page < 1:
        raise ValueError("page must be at least 1")
    if page_size < 1 or page_size > max_page_size:
        raise ValueError(f"page_size must be between 1 and {max_page_size}")
    return statement.limit(page_size).offset((page - 1) * page_size)


def optimistic_update(
    connection: Connection,
    table: sa.Table,
    *,
    record_id: Any,
    expected_version: int,
    values: Mapping[str, Any],
) -> int:
    if "version" in values:
        raise ValueError("version is controlled by optimistic_update")
    new_version = expected_version + 1
    result = connection.execute(
        table.update()
        .where(table.c.id == record_id, table.c.version == expected_version)
        .values(**dict(values), version=new_version, updated_at=sa.func.now())
    )
    if result.rowcount != 1:
        raise OptimisticLockConflict(
            f"record {record_id!s} is no longer at version {expected_version}"
        )
    return new_version
