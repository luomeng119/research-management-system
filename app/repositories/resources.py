from __future__ import annotations

import uuid

import sqlalchemy as sa

from app.repositories.base import paginate


class ResearchResourcesRepository:
    """PostgreSQL-compatible persistence for the retained expert resources."""

    def __init__(self, engine) -> None:
        self.engine = engine
        metadata = sa.MetaData()
        self.experts = sa.Table("experts", metadata, autoload_with=engine)
        self.expert_groups = sa.Table("expert_groups", metadata, autoload_with=engine)
        self.expert_group_members = sa.Table(
            "expert_group_members", metadata, autoload_with=engine
        )
        self.expert_import_batches = sa.Table(
            "expert_import_batches", metadata, autoload_with=engine
        )

    def list_experts(self, connection, *, page, page_size, keyword=None):
        statement = sa.select(self.experts)
        if keyword:
            escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{escaped}%"
            statement = statement.where(sa.or_(
                self.experts.c.name.ilike(pattern, escape="\\"),
                self.experts.c.unit.ilike(pattern, escape="\\"),
                self.experts.c.expertise.ilike(pattern, escape="\\"),
            ))
        total = connection.scalar(
            sa.select(sa.func.count()).select_from(statement.subquery())
        )
        rows = connection.execute(
            paginate(
                statement.order_by(
                    self.experts.c.created_at.asc(), self.experts.c.id.asc()
                ),
                page=page,
                page_size=page_size,
            )
        ).mappings()
        return [dict(row) for row in rows], int(total or 0)

    def get_expert(self, connection, expert_id):
        return connection.execute(
            sa.select(self.experts).where(self.experts.c.expert_id == expert_id)
        ).mappings().first()

    def list_available_experts(
        self, connection, group_id, *, keyword=None, unit=None,
        position=None, expertise=None, limit=20,
    ):
        member = self.expert_group_members.alias("member")
        statement = sa.select(self.experts).where(~sa.exists(
            sa.select(1).select_from(member).where(sa.and_(
                member.c.group_id == group_id,
                member.c.expert_id == self.experts.c.expert_id,
            ))
        ))
        if keyword:
            escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{escaped}%"
            statement = statement.where(sa.or_(
                self.experts.c.name.ilike(pattern, escape="\\"),
                self.experts.c.unit.ilike(pattern, escape="\\"),
                self.experts.c.expertise.ilike(pattern, escape="\\"),
            ))
        for column, value in (
            (self.experts.c.unit, unit),
            (self.experts.c.position, position),
            (self.experts.c.expertise, expertise),
        ):
            if value:
                statement = statement.where(column == value)
        return [dict(row) for row in connection.execute(
            statement.order_by(
                self.experts.c.created_at.desc(), self.experts.c.id.desc()
            ).limit(limit)
        ).mappings()]

    def expert_facets(self, connection):
        def values(column):
            return [row[0] for row in connection.execute(
                sa.select(column).where(
                    column.is_not(None), column != ""
                ).distinct().order_by(column)
            )]
        return {
            "units": values(self.experts.c.unit),
            "positions": values(self.experts.c.position),
            "expertises": values(self.experts.c.expertise),
        }

    def export_experts(self, connection, expert_ids=None):
        statement = sa.select(self.experts)
        if expert_ids is not None:
            statement = statement.where(self.experts.c.expert_id.in_(expert_ids))
        return [dict(row) for row in connection.execute(
            statement.order_by(self.experts.c.created_at, self.experts.c.id)
        ).mappings()]

    def existing_expert_keys(self, connection, keys):
        found = set()
        keys = list(keys)
        normalized_name = sa.func.coalesce(self.experts.c.name, "")
        normalized_unit = sa.func.coalesce(self.experts.c.unit, "")
        normalized_phone = sa.func.coalesce(self.experts.c.phone, "")
        for start in range(0, len(keys), 200):
            chunk = keys[start:start + 200]
            if not chunk:
                continue
            rows = connection.execute(
                sa.select(
                    normalized_name, normalized_unit, normalized_phone
                ).where(sa.tuple_(
                    normalized_name, normalized_unit, normalized_phone
                ).in_(chunk))
            )
            found.update((row[0] or "", row[1] or "", row[2] or "") for row in rows)
        return found

    @staticmethod
    def lock_expert_identity_space(connection):
        if connection.dialect.name == "postgresql":
            connection.execute(sa.text("SELECT pg_advisory_xact_lock(7810202501)"))

    def expert_with_key(self, connection, key, *, exclude_expert_id=None):
        normalized_name = sa.func.coalesce(self.experts.c.name, "")
        normalized_unit = sa.func.coalesce(self.experts.c.unit, "")
        normalized_phone = sa.func.coalesce(self.experts.c.phone, "")
        statement = sa.select(self.experts.c.expert_id).where(sa.and_(
            normalized_name == key[0],
            normalized_unit == key[1],
            normalized_phone == key[2],
        ))
        if exclude_expert_id:
            statement = statement.where(self.experts.c.expert_id != exclude_expert_id)
        return connection.scalar(statement.limit(1))

    def insert_expert(self, connection, values):
        connection.execute(self.experts.insert().values(**values))

    def update_expert(self, connection, expert_id, values):
        result = connection.execute(
            self.experts.update()
            .where(self.experts.c.expert_id == expert_id)
            .values(**values)
        )
        return result.rowcount

    def expert_membership_count(self, connection, expert_id):
        return int(connection.scalar(
            sa.select(sa.func.count()).select_from(self.expert_group_members).where(
                self.expert_group_members.c.expert_id == expert_id
            )
        ) or 0)

    def delete_expert(self, connection, expert_id):
        return connection.execute(
            self.experts.delete().where(self.experts.c.expert_id == expert_id)
        ).rowcount

    def insert_expert_group(self, connection, values):
        connection.execute(self.expert_groups.insert().values(**values))

    def list_expert_groups(self, connection, *, page, page_size):
        member_count = (
            sa.select(
                self.expert_group_members.c.group_id,
                sa.func.count().label("member_count"),
            )
            .group_by(self.expert_group_members.c.group_id)
            .subquery()
        )
        statement = (
            sa.select(
                self.expert_groups,
                sa.func.coalesce(member_count.c.member_count, 0).label("member_count"),
            )
            .outerjoin(
                member_count,
                member_count.c.group_id == self.expert_groups.c.group_id,
            )
        )
        total = int(connection.scalar(
            sa.select(sa.func.count()).select_from(self.expert_groups)
        ) or 0)
        rows = connection.execute(
            paginate(
                statement.order_by(
                    self.expert_groups.c.created_at.desc(),
                    self.expert_groups.c.id.desc(),
                ),
                page=page,
                page_size=page_size,
            )
        ).mappings()
        return [dict(row) for row in rows], total

    def get_expert_group(self, connection, group_id):
        group = connection.execute(
            sa.select(self.expert_groups).where(
                self.expert_groups.c.group_id == group_id
            )
        ).mappings().first()
        if group is None:
            return None
        members = connection.execute(
            sa.select(self.experts, self.expert_group_members.c.selected_by,
                      self.expert_group_members.c.selected_at)
            .select_from(
                self.expert_group_members.join(
                    self.experts,
                    self.expert_group_members.c.expert_id == self.experts.c.expert_id,
                )
            )
            .where(self.expert_group_members.c.group_id == group_id)
            .order_by(self.expert_group_members.c.selected_at, self.experts.c.id)
        ).mappings()
        return dict(group), [dict(member) for member in members]

    def insert_expert_group_member(self, connection, values):
        connection.execute(self.expert_group_members.insert().values(**values))

    def delete_expert_group_member(self, connection, group_id, expert_id):
        return connection.execute(
            self.expert_group_members.delete().where(sa.and_(
                self.expert_group_members.c.group_id == group_id,
                self.expert_group_members.c.expert_id == expert_id,
            ))
        ).rowcount

    def delete_expert_group(self, connection, group_id):
        connection.execute(
            self.expert_group_members.delete().where(
                self.expert_group_members.c.group_id == group_id
            )
        )
        return connection.execute(
            self.expert_groups.delete().where(
                self.expert_groups.c.group_id == group_id
            )
        ).rowcount

    @staticmethod
    def _uuid(connection, value):
        if connection.dialect.name == "postgresql":
            return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
        return str(value)

    def insert_expert_import_batch(self, connection, values):
        payload = dict(values)
        payload["id"] = self._uuid(connection, payload["id"])
        connection.execute(self.expert_import_batches.insert().values(**payload))

    def get_expert_import_batch(self, connection, batch_id, *, lock=False):
        statement = sa.select(self.expert_import_batches).where(
            self.expert_import_batches.c.id == self._uuid(connection, batch_id)
        )
        if lock and connection.dialect.name == "postgresql":
            statement = statement.with_for_update(of=self.expert_import_batches)
        return connection.execute(statement).mappings().first()

    def update_expert_import_batch(self, connection, batch_id, values):
        return connection.execute(
            self.expert_import_batches.update()
            .where(self.expert_import_batches.c.id == self._uuid(connection, batch_id))
            .values(**values)
        ).rowcount


class EquipmentResourcesRepository:
    """Bounded PostgreSQL persistence for retained equipment resource records."""

    def __init__(self, engine) -> None:
        self.engine = engine
        metadata = sa.MetaData()
        self.equipment = sa.Table("equipment", metadata, autoload_with=engine)
        self.knowledge_subclasses = sa.Table(
            "knowledge_subclasses", metadata, autoload_with=engine
        )
        self.research_units = sa.Table("research_units", metadata, autoload_with=engine)

    @staticmethod
    def _pattern(value):
        escaped = str(value).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return f"%{escaped}%"

    def list_equipment(self, connection, *, page, page_size, category=None,
                       form=None, tech_status=None, keyword=None, subclass=None):
        statement = sa.select(self.equipment)
        for column, value in (
            (self.equipment.c.category, category), (self.equipment.c.form, form),
            (self.equipment.c.tech_status, tech_status),
            (self.equipment.c.subclass, subclass),
        ):
            if value:
                statement = statement.where(column == value)
        if keyword:
            pattern = self._pattern(keyword)
            statement = statement.where(sa.or_(
                self.equipment.c.name.ilike(pattern, escape="\\"),
                self.equipment.c.model.ilike(pattern, escape="\\"),
                self.equipment.c.equipment_id.ilike(pattern, escape="\\"),
            ))
        total = int(connection.scalar(
            sa.select(sa.func.count()).select_from(statement.subquery())
        ) or 0)
        rows = connection.execute(paginate(
            statement.order_by(self.equipment.c.created_at.desc(), self.equipment.c.id.desc()),
            page=page, page_size=page_size,
        )).mappings()
        return [dict(row) for row in rows], total

    def get_equipment(self, connection, equipment_id):
        return connection.execute(sa.select(self.equipment).where(
            self.equipment.c.equipment_id == equipment_id
        )).mappings().first()

    def equipment_stats(self, connection):
        categories = connection.execute(sa.select(
            self.equipment.c.category, sa.func.count().label("count"),
            sa.func.coalesce(sa.func.sum(self.equipment.c.price), 0).label("value"),
        ).group_by(self.equipment.c.category)).mappings()
        statuses = connection.execute(sa.select(
            self.equipment.c.tech_status, sa.func.count().label("count")
        ).group_by(self.equipment.c.tech_status)).mappings()
        return [dict(row) for row in categories], [dict(row) for row in statuses]

    def insert_equipment(self, connection, values):
        connection.execute(self.equipment.insert().values(**values))

    def update_equipment(self, connection, equipment_id, values):
        return connection.execute(self.equipment.update().where(
            self.equipment.c.equipment_id == equipment_id
        ).values(**values)).rowcount

    def delete_equipment(self, connection, equipment_id):
        return connection.execute(self.equipment.delete().where(
            self.equipment.c.equipment_id == equipment_id
        )).rowcount

    def list_research_units(self, connection):
        return [dict(row) for row in connection.execute(
            sa.select(self.research_units).order_by(self.research_units.c.name, self.research_units.c.id)
        ).mappings()]

    def get_research_unit_by_name(self, connection, name):
        return connection.execute(sa.select(self.research_units).where(
            self.research_units.c.name == name
        )).mappings().first()

    def insert_research_unit(self, connection, values):
        connection.execute(self.research_units.insert().values(**values))

    def get_research_unit(self, connection, unit_id):
        return connection.execute(sa.select(self.research_units).where(
            self.research_units.c.unit_id == unit_id
        )).mappings().first()

    def update_research_unit(self, connection, unit_id, values):
        return connection.execute(self.research_units.update().where(
            self.research_units.c.unit_id == unit_id
        ).values(**values)).rowcount

    def delete_research_unit(self, connection, unit_id):
        return connection.execute(self.research_units.delete().where(
            self.research_units.c.unit_id == unit_id
        )).rowcount

    def list_subclasses(self, connection, parent_category=None):
        statement = sa.select(self.knowledge_subclasses)
        if parent_category:
            statement = statement.where(
                self.knowledge_subclasses.c.parent_category == parent_category
            )
        return [dict(row) for row in connection.execute(statement.order_by(
            self.knowledge_subclasses.c.parent_category,
            self.knowledge_subclasses.c.subclass_name,
            self.knowledge_subclasses.c.id,
        )).mappings()]

    def get_subclass(self, connection, parent_category, subclass_name):
        return connection.execute(sa.select(self.knowledge_subclasses).where(sa.and_(
            self.knowledge_subclasses.c.parent_category == parent_category,
            self.knowledge_subclasses.c.subclass_name == subclass_name,
        ))).mappings().first()

    def insert_subclass(self, connection, values):
        return connection.execute(
            self.knowledge_subclasses.insert().values(**values).returning(
                self.knowledge_subclasses.c.id
            )
        ).scalar_one()

    def delete_subclass(self, connection, row_id):
        return connection.execute(self.knowledge_subclasses.delete().where(
            self.knowledge_subclasses.c.id == row_id
        )).rowcount
