"""Add attachment links to media assets."""

from alembic import op

revision = "20260728_0004"
down_revision = "20260728_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE media_assets ADD COLUMN IF NOT EXISTS attachment_links text")


def downgrade() -> None:
    op.execute("ALTER TABLE media_assets DROP COLUMN IF EXISTS attachment_links")
