"""Add a separate annual sequence for HESP response receipts."""

import sqlalchemy as sa
from alembic import op

revision = "20260928_0013"
down_revision = "20260928_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "requirement_response_counters",
        sa.Column("year", sa.Integer(), primary_key=True),
        sa.Column("last_value", sa.Integer(), nullable=False),
        schema="public",
    )
    op.execute("ALTER TABLE public.requirement_response_counters ENABLE ROW LEVEL SECURITY")
    op.execute("""
        DO $$
        DECLARE app_role text;
        BEGIN
          FOREACH app_role IN ARRAY ARRAY['anon', 'authenticated'] LOOP
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = app_role) THEN
              EXECUTE format('REVOKE ALL PRIVILEGES ON TABLE public.requirement_response_counters FROM %I', app_role);
            END IF;
          END LOOP;
        END $$
    """)


def downgrade() -> None:
    op.drop_table("requirement_response_counters", schema="public")
