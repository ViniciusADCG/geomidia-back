"""Add the authorization expiration date to media assets."""

import sqlalchemy as sa
from alembic import op

revision = "20260820_0009"
down_revision = "20260820_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "media_assets",
        sa.Column("expiration_date", sa.Date(), nullable=True),
        schema="public",
    )
    op.create_index(
        "ix_media_assets_expiration_date",
        "media_assets",
        ["expiration_date"],
        unique=False,
        schema="public",
    )


def downgrade() -> None:
    op.drop_index("ix_media_assets_expiration_date", table_name="media_assets", schema="public")
    op.drop_column("media_assets", "expiration_date", schema="public")
