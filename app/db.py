from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine


HEAD_REVISION = "0001_v1_core"


def get_runtime_database_url() -> str:
    value = os.environ.get("DATABASE_URL")
    if not value:
        raise RuntimeError("DATABASE_URL is required for PostgreSQL runtime access")
    return value


def get_migration_database_url() -> str:
    value = os.environ.get("MIGRATION_DATABASE_URL")
    if not value:
        raise RuntimeError("MIGRATION_DATABASE_URL is required for Alembic migrations")
    return value


def create_runtime_engine(
    database_url: str | None = None,
    *,
    pool_size: int | None = None,
    max_overflow: int | None = None,
    pool_timeout: int | None = None,
) -> Engine:
    return sa.create_engine(
        database_url or get_runtime_database_url(),
        pool_size=pool_size or int(os.environ.get("DB_POOL_SIZE", "5")),
        max_overflow=(
            max_overflow
            if max_overflow is not None
            else int(os.environ.get("DB_MAX_OVERFLOW", "5"))
        ),
        pool_timeout=pool_timeout or int(os.environ.get("DB_POOL_TIMEOUT", "10")),
        pool_pre_ping=True,
    )


@contextmanager
def transaction(engine: Engine) -> Iterator[Connection]:
    with engine.begin() as connection:
        yield connection


def check_schema_version(
    engine: Engine,
    *,
    expected_revision: str = HEAD_REVISION,
) -> str:
    try:
        with engine.connect() as connection:
            revision = connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
    except sa.exc.SQLAlchemyError as exc:
        raise RuntimeError("PostgreSQL is unavailable or has no Alembic schema") from exc
    if revision != expected_revision:
        raise RuntimeError(
            f"database schema revision {revision!r} does not match code head "
            f"{expected_revision!r}"
        )
    return revision


def initialize_runtime_database(database_url: str | None = None) -> Engine:
    engine = create_runtime_engine(database_url)
    try:
        check_schema_version(engine)
    except Exception:
        engine.dispose()
        raise
    return engine
