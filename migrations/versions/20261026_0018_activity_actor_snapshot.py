"""Preserve the actor's role and name at the time of each activity."""

import sqlalchemy as sa
from alembic import op

revision = "20261026_0018"
down_revision = "20261014_0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("activity_logs", sa.Column("actor_role", sa.String(length=16), nullable=True), schema="public")
    op.add_column("activity_logs", sa.Column("actor_name", sa.String(length=160), nullable=True), schema="public")
    op.create_index("ix_activity_logs_actor_role", "activity_logs", ["actor_role"], schema="public")


def downgrade() -> None:
    op.drop_index("ix_activity_logs_actor_role", table_name="activity_logs", schema="public")
    op.drop_column("activity_logs", "actor_name", schema="public")
    op.drop_column("activity_logs", "actor_role", schema="public")
