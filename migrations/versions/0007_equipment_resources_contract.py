"""Harden the retained equipment and research-resource data contract.

Revision ID: 0007_equipment_resources
Revises: 0006_reference_library
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0007_equipment_resources"
down_revision = "0006_reference_library"
branch_labels = None
depends_on = None


def _count(bind, statement: str) -> int:
    return int(bind.scalar(sa.text(statement)) or 0)


def upgrade() -> None:
    bind = op.get_bind()

    if _count(bind, """
        SELECT count(*) FROM (
          SELECT btrim(equipment_id) AS equipment_id
          FROM equipment
          WHERE equipment_id IS NOT NULL AND btrim(equipment_id) <> ''
          GROUP BY btrim(equipment_id) HAVING count(*) > 1
        ) duplicates
    """):
        raise RuntimeError("duplicate equipment identifiers prevent equipment-resource upgrade")

    if _count(bind, """
        SELECT count(*) FROM equipment blank
        JOIN equipment existing
          ON existing.id <> blank.id
         AND btrim(existing.equipment_id) = 'EQP-LEGACY-' || blank.id::text
        WHERE blank.equipment_id IS NULL OR btrim(blank.equipment_id) = ''
    """):
        raise RuntimeError("generated legacy equipment identifiers would collide")

    if _count(bind, """
        SELECT count(*) FROM equipment_group_members member
        LEFT JOIN equipment item
          ON btrim(item.equipment_id) = btrim(member.equipment_id)
         AND btrim(item.equipment_id) <> ''
        WHERE btrim(member.equipment_id) = '' OR item.id IS NULL
    """) or _count(bind, """
        SELECT count(*) FROM device_host_relations relation
        LEFT JOIN equipment item
          ON btrim(item.equipment_id) = btrim(relation.device_id)
         AND btrim(item.equipment_id) <> ''
        LEFT JOIN host_devices host ON host.host_id = relation.host_id
        WHERE btrim(relation.device_id) = '' OR item.id IS NULL OR host.id IS NULL
    """):
        raise RuntimeError("orphan equipment-resource relations prevent upgrade")

    if _count(bind, """
        SELECT count(*) FROM equipment_group_members WHERE quantity < 1
    """) or _count(bind, """
        SELECT count(*) FROM device_host_relations WHERE quantity < 1
    """):
        raise RuntimeError("non-positive equipment-resource quantities prevent upgrade")

    if _count(bind, """
        SELECT count(*) FROM (
          SELECT btrim(project_id) AS project_id FROM equipment_groups
          WHERE project_id IS NOT NULL AND btrim(project_id) <> ''
          GROUP BY btrim(project_id) HAVING count(*) > 1
        ) duplicates
    """):
        raise RuntimeError("duplicate project equipment groups prevent upgrade")

    bind.execute(sa.text("""
        UPDATE equipment
        SET equipment_id = CASE
          WHEN equipment_id IS NULL OR btrim(equipment_id) = ''
            THEN 'EQP-LEGACY-' || id::text
          ELSE btrim(equipment_id)
        END
    """))
    bind.execute(sa.text("""
        UPDATE equipment_group_members SET equipment_id = btrim(equipment_id)
    """))
    bind.execute(sa.text("""
        UPDATE device_host_relations SET device_id = btrim(device_id)
    """))
    bind.execute(sa.text("""
        UPDATE equipment_groups SET project_id = NULLIF(btrim(project_id), '')
        WHERE project_id IS NOT NULL
    """))

    op.alter_column("equipment", "equipment_id", nullable=False)
    op.create_unique_constraint(
        "uq_equipment_equipment_id", "equipment", ["equipment_id"]
    )
    op.create_check_constraint(
        "ck_equipment_equipment_id_trimmed",
        "equipment", "equipment_id = btrim(equipment_id) AND equipment_id <> ''",
    )
    op.create_check_constraint(
        "ck_equipment_groups_project_id_trimmed",
        "equipment_groups",
        "project_id IS NULL OR (project_id = btrim(project_id) AND project_id <> '')",
    )
    op.add_column("equipment_group_members", sa.Column("location", sa.Text()))
    op.create_check_constraint(
        "ck_equipment_group_members_quantity_positive",
        "equipment_group_members", "quantity >= 1",
    )
    op.create_check_constraint(
        "ck_device_host_relations_quantity_positive",
        "device_host_relations", "quantity >= 1",
    )
    op.create_foreign_key(
        "fk_equipment_group_members_equipment_id",
        "equipment_group_members", "equipment",
        ["equipment_id"], ["equipment_id"], ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_device_host_relations_device_id",
        "device_host_relations", "equipment",
        ["device_id"], ["equipment_id"], ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_device_host_relations_host_id",
        "device_host_relations", "host_devices",
        ["host_id"], ["host_id"], ondelete="CASCADE",
    )
    op.create_index(
        "uq_equipment_groups_project_id", "equipment_groups", ["project_id"],
        unique=True, postgresql_where=sa.text("project_id IS NOT NULL"),
    )
    op.create_index(
        "ix_equipment_group_members_equipment_id",
        "equipment_group_members", ["equipment_id"],
    )
    op.create_index(
        "ix_device_host_relations_host_device",
        "device_host_relations", ["host_id", "device_id"],
    )
    op.create_index(
        "ix_host_devices_category_name", "host_devices", ["category", "name", "id"]
    )


def downgrade() -> None:
    bind = op.get_bind()
    if _count(bind, """
        SELECT count(*) FROM equipment_group_members
        WHERE location IS NOT NULL AND btrim(location) <> ''
    """):
        raise RuntimeError("equipment usage locations prevent equipment-resource downgrade")

    op.drop_index("ix_host_devices_category_name", table_name="host_devices")
    op.drop_index("ix_device_host_relations_host_device", table_name="device_host_relations")
    op.drop_index("ix_equipment_group_members_equipment_id", table_name="equipment_group_members")
    op.drop_index("uq_equipment_groups_project_id", table_name="equipment_groups")
    op.drop_constraint(
        "fk_device_host_relations_host_id", "device_host_relations", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_device_host_relations_device_id", "device_host_relations", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_equipment_group_members_equipment_id",
        "equipment_group_members", type_="foreignkey",
    )
    op.drop_constraint(
        "ck_device_host_relations_quantity_positive",
        "device_host_relations", type_="check",
    )
    op.drop_constraint(
        "ck_equipment_group_members_quantity_positive",
        "equipment_group_members", type_="check",
    )
    op.drop_column("equipment_group_members", "location")
    op.drop_constraint(
        "ck_equipment_groups_project_id_trimmed", "equipment_groups", type_="check"
    )
    op.drop_constraint(
        "ck_equipment_equipment_id_trimmed", "equipment", type_="check"
    )
    op.drop_constraint("uq_equipment_equipment_id", "equipment", type_="unique")
    op.alter_column("equipment", "equipment_id", nullable=True)
