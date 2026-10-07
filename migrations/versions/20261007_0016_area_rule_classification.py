"""Store an area band when the exact vehicle area is not supplied."""

import sqlalchemy as sa
from alembic import op

revision = "20261007_0016"
down_revision = "20261005_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("media_assets", "application_forms"):
        op.alter_column(table, "area_m2", existing_type=sa.Float(), nullable=True, schema="public")
        op.alter_column(table, "bottom_height_m", existing_type=sa.Float(), nullable=True, schema="public")
        op.add_column(table, sa.Column("area_rule_classification", sa.String(16), nullable=True), schema="public")
        op.create_check_constraint(
            f"ck_{table}_area_rule_classification",
            table,
            "area_rule_classification IS NULL OR area_rule_classification IN ('within_limit', 'above_limit')",
            schema="public",
        )
        op.create_check_constraint(
            f"ck_{table}_area_source",
            table,
            "area_m2 IS NULL OR area_rule_classification IS NULL",
            schema="public",
        )


def downgrade() -> None:
    for table in ("media_assets", "application_forms"):
        op.execute(
            f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM public.{table} "
            "WHERE area_m2 IS NULL OR bottom_height_m IS NULL) "
            "THEN RAISE EXCEPTION 'Downgrade requires non-null vehicle measurements'; END IF; END $$"
        )
        op.drop_constraint(f"ck_{table}_area_source", table, schema="public")
        op.drop_constraint(f"ck_{table}_area_rule_classification", table, schema="public")
        op.drop_column(table, "area_rule_classification", schema="public")
        op.alter_column(table, "bottom_height_m", existing_type=sa.Float(), nullable=False, schema="public")
        op.alter_column(table, "area_m2", existing_type=sa.Float(), nullable=False, schema="public")
