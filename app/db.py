from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import sqlalchemy as sa
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.engine import Connection, Engine

DEFAULT_ALEMBIC_CONFIG = Path(__file__).resolve().parents[1] / "alembic.ini"


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
    alembic_config_path: str | Path = DEFAULT_ALEMBIC_CONFIG,
) -> str:
    config = Config(str(alembic_config_path))
    code_heads = set(ScriptDirectory.from_config(config).get_heads())
    if len(code_heads) != 1:
        raise RuntimeError(
            f"expected exactly one code head, found {sorted(code_heads)!r}"
        )
    try:
        with engine.connect() as connection:
            database_revisions = tuple(
                connection.scalars(
                    sa.text("SELECT version_num FROM alembic_version")
                ).all()
            )
    except sa.exc.SQLAlchemyError as exc:
        raise RuntimeError("PostgreSQL is unavailable or has no Alembic schema") from exc
    if len(database_revisions) != 1:
        raise RuntimeError(
            "expected exactly one database revision, "
            f"found {sorted(database_revisions)!r}"
        )
    database_revision = database_revisions[0]
    if {database_revision} != code_heads:
        code_head = next(iter(code_heads))
        raise RuntimeError(
            f"database schema revision {database_revision!r} does not match "
            f"code head {code_head!r}"
        )
    return database_revision


def initialize_runtime_database(database_url: str | None = None) -> Engine:
    engine = create_runtime_engine(database_url)
    try:
        check_schema_version(engine)
    except Exception:
        engine.dispose()
        raise
    return engine
