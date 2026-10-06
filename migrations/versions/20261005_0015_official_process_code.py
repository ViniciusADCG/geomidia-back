"""Store the official process number separately from the immutable origin code."""

import sqlalchemy as sa
from alembic import op

revision = "20261005_0015"
down_revision = "20260928_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("media_assets", sa.Column("official_process_code", sa.String(length=80), nullable=True), schema="public")


def downgrade() -> None:
    op.drop_column("media_assets", "official_process_code", schema="public")
