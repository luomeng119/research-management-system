from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.engine import Engine


class GenericTablesRepository:
    """SQLAlchemy persistence boundary for the retained generic-table module."""

    table_names = frozenset({
        "generic_tables", "generic_table_versions",
        "generic_table_columns", "generic_table_data",
    })

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        metadata = sa.MetaData()
        self.tables = sa.Table("generic_tables", metadata, autoload_with=engine)
        self.versions = sa.Table("generic_table_versions", metadata, autoload_with=engine)
        self.columns = sa.Table("generic_table_columns", metadata, autoload_with=engine)
        self.data = sa.Table("generic_table_data", metadata, autoload_with=engine)

    @staticmethod
    def _rows(connection, statement) -> list[dict]:
        return [dict(row) for row in connection.execute(statement).mappings()]

    @staticmethod
    def _one(connection, statement) -> dict | None:
        row = connection.execute(statement).mappings().first()
        return dict(row) if row else None

    def list_tables(self) -> list[dict]:
        latest = sa.select(
            self.versions.c.table_id,
            sa.func.count().label("version_count"),
            sa.func.max(self.versions.c.version_number).label("latest_version_number"),
        ).group_by(self.versions.c.table_id).subquery()
        current = self.versions.alias("current_version")
        statement = sa.select(
            self.tables,
            latest.c.version_count,
            latest.c.latest_version_number,
            current.c.version_number.label("cur_version_number"),
            current.c.version_label.label("cur_version_label"),
            current.c.creator.label("cur_creator"),
            current.c.created_at.label("cur_created_at"),
        ).outerjoin(latest, latest.c.table_id == self.tables.c.table_id).outerjoin(
            current, current.c.version_id == self.tables.c.current_version_id,
        ).order_by(self.tables.c.updated_at.desc())
        with self.engine.connect() as connection:
            rows = self._rows(connection, statement)
            for row in rows:
                row["latest_version_id"] = self._one(connection, sa.select(self.versions.c.version_id).where(
                    self.versions.c.table_id == row["table_id"],
                ).order_by(self.versions.c.version_number.desc(), self.versions.c.version_id.desc()).limit(1))["version_id"]
            return rows

    def get_table(self, table_id: str, connection=None) -> dict | None:
        statement = sa.select(self.tables).where(self.tables.c.table_id == table_id)
        if connection is not None:
            return self._one(connection, statement)
        with self.engine.connect() as reader:
            return self._one(reader, statement)

    def get_table_with_current(self, table_id: str) -> dict | None:
        version = self.versions.alias("current_version")
        statement = sa.select(
            self.tables,
            version.c.version_number.label("cur_version_number"),
            version.c.version_label.label("cur_version_label"),
            version.c.create_method.label("cur_create_method"),
            version.c.row_count.label("cur_row_count"),
            version.c.is_locked.label("cur_is_locked"),
            version.c.creator.label("cur_creator"),
            version.c.created_at.label("cur_created_at"),
        ).outerjoin(version, version.c.version_id == self.tables.c.current_version_id).where(
            self.tables.c.table_id == table_id,
        )
        with self.engine.connect() as connection:
            return self._one(connection, statement)

    def lock_table(self, connection, table_id: str) -> dict | None:
        statement = sa.select(self.tables).where(self.tables.c.table_id == table_id)
        if connection.dialect.name == "postgresql":
            statement = statement.with_for_update(of=self.tables)
        return self._one(connection, statement)

    def list_versions(self, table_id: str) -> list[dict]:
        statement = sa.select(self.versions).where(
            self.versions.c.table_id == table_id,
        ).order_by(self.versions.c.version_number.desc(), self.versions.c.version_id.desc())
        with self.engine.connect() as connection:
            return self._rows(connection, statement)

    def get_version(self, version_id: str, connection=None) -> dict | None:
        statement = sa.select(self.versions).where(self.versions.c.version_id == version_id)
        if connection is not None:
            return self._one(connection, statement)
        with self.engine.connect() as reader:
            return self._one(reader, statement)

    def next_version_number(self, connection, table_id: str) -> int:
        value = connection.execute(sa.select(sa.func.max(self.versions.c.version_number)).where(
            self.versions.c.table_id == table_id,
        )).scalar_one_or_none()
        return int(value or 0) + 1

    def list_columns(self, version_id: str, connection=None) -> list[dict]:
        statement = sa.select(self.columns).where(
            self.columns.c.version_id == version_id,
        ).order_by(self.columns.c.col_index, self.columns.c.id)
        if connection is not None:
            return self._rows(connection, statement)
        with self.engine.connect() as reader:
            return self._rows(reader, statement)

    def list_rows(self, version_id: str, keyword: str | None = None, *, limit: int | None = None, offset: int = 0, connection=None) -> list[dict]:
        statement = sa.select(self.data).where(self.data.c.version_id == version_id)
        if keyword:
            statement = statement.where(sa.cast(self.data.c.row_data, sa.Text).ilike(f"%{keyword}%"))
        statement = statement.order_by(self.data.c.row_index, self.data.c.id).offset(offset)
        if limit is not None:
            statement = statement.limit(limit)
        if connection is not None:
            return self._rows(connection, statement)
        with self.engine.connect() as reader:
            return self._rows(reader, statement)

    def count_rows(self, connection, version_id: str) -> int:
        return int(connection.execute(sa.select(sa.func.count()).select_from(self.data).where(
            self.data.c.version_id == version_id,
        )).scalar_one())

    def count_matching_rows(self, version_id: str, keyword: str | None = None, connection=None) -> int:
        statement = sa.select(sa.func.count()).select_from(self.data).where(self.data.c.version_id == version_id)
        if keyword:
            statement = statement.where(sa.cast(self.data.c.row_data, sa.Text).ilike(f"%{keyword}%"))
        if connection is not None:
            return int(connection.execute(statement).scalar_one())
        with self.engine.connect() as reader:
            return int(reader.execute(statement).scalar_one())

    def copy_columns(self, connection, source_id: str, target_id: str, created_at) -> None:
        names = ["version_id", "col_key", "col_name", "col_type", "col_index", "col_width", "col_align", "col_summary", "col_options", "created_at"]
        statement = sa.select(
            sa.literal(target_id), self.columns.c.col_key, self.columns.c.col_name,
            self.columns.c.col_type, self.columns.c.col_index, self.columns.c.col_width,
            self.columns.c.col_align, self.columns.c.col_summary, self.columns.c.col_options,
            sa.literal(created_at),
        ).where(self.columns.c.version_id == source_id)
        connection.execute(self.columns.insert().from_select(names, statement))

    def copy_rows(self, connection, source_id: str, target_id: str, now) -> None:
        names = ["version_id", "row_key", "row_index", "row_data", "row_color", "created_at", "updated_at"]
        statement = sa.select(
            sa.literal(target_id), self.data.c.row_key, self.data.c.row_index,
            self.data.c.row_data, self.data.c.row_color, sa.literal(now), sa.literal(now),
        ).where(self.data.c.version_id == source_id)
        connection.execute(self.data.insert().from_select(names, statement))
