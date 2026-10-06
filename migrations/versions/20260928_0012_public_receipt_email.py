"""Track email delivery of public submission receipts."""

import sqlalchemy as sa
from alembic import op

revision = "20260928_0012"
down_revision = "20260919_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "public_submission_drafts",
        sa.Column("receipt_sent_at", sa.DateTime(timezone=True), nullable=True),
        schema="public",
    )


def downgrade() -> None:
    op.drop_column("public_submission_drafts", "receipt_sent_at", schema="public")
