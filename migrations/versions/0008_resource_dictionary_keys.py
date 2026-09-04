"""Protect canonical research-unit names.

Revision ID: 0008_resource_dictionary_keys
Revises: 0007_equipment_resources
"""
from alembic import op
import sqlalchemy as sa


revision = "0008_resource_dictionary_keys"
down_revision = "0007_equipment_resources"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    duplicates = bind.scalar(sa.text("""
        SELECT count(*) FROM (
          SELECT btrim(name) FROM research_units
          GROUP BY btrim(name) HAVING count(*) > 1
        ) names
    """))
    if duplicates:
        raise RuntimeError("duplicate research-unit names prevent resource upgrade")
    bind.execute(sa.text("UPDATE research_units SET name = btrim(name)"))
    op.create_check_constraint(
        "ck_research_units_name_trimmed", "research_units",
        "name = btrim(name) AND name <> ''",
    )
    op.create_unique_constraint(
        "uq_research_units_name", "research_units", ["name"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_research_units_name", "research_units", type_="unique")
    op.drop_constraint("ck_research_units_name_trimmed", "research_units", type_="check")
