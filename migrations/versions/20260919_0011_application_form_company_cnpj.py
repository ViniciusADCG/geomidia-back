"""Add company CNPJ to application forms."""

import sqlalchemy as sa
from alembic import op

revision = "20260919_0011"
down_revision = "20260827_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "application_forms",
        sa.Column("company_cnpj", sa.String(length=14), nullable=True),
        schema="public",
    )
    op.create_check_constraint(
        "ck_application_forms_company_cnpj",
        "application_forms",
        "company_cnpj IS NULL OR company_cnpj ~ '^[0-9]{14}$'",
        schema="public",
    )
    op.create_index(
        "ix_application_forms_company_cnpj",
        "application_forms",
        ["company_cnpj"],
        unique=False,
        schema="public",
    )


def downgrade() -> None:
    op.drop_index("ix_application_forms_company_cnpj", table_name="application_forms", schema="public")
    op.drop_constraint(
        "ck_application_forms_company_cnpj",
        "application_forms",
        type_="check",
        schema="public",
    )
    op.drop_column("application_forms", "company_cnpj", schema="public")
