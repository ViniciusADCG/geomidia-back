"""Add the initial New Processes workflow status."""

from alembic import op

revision = "20260820_0008"
down_revision = "20260819_0007"
branch_labels = None
depends_on = None


STATUS_VALUES = (
    "'novos processos', 'aprovado', 'irregular', 'análise', 'exigência', "
    "'vencido', 'cartografia', 'jurídico', 'vistoria'"
)
PREVIOUS_STATUS_VALUES = (
    "'aprovado', 'irregular', 'análise', 'exigência', "
    "'vencido', 'cartografia', 'jurídico', 'vistoria'"
)


def upgrade() -> None:
    op.execute("ALTER TABLE public.media_assets DROP CONSTRAINT IF EXISTS ck_media_assets_status")
    op.execute(
        f"ALTER TABLE public.media_assets ADD CONSTRAINT ck_media_assets_status CHECK (status IN ({STATUS_VALUES}))"
    )
    op.execute("ALTER TABLE public.media_assets ALTER COLUMN status SET DEFAULT 'novos processos'")


def downgrade() -> None:
    op.execute("UPDATE public.media_assets SET status = 'análise' WHERE status = 'novos processos'")
    op.execute("ALTER TABLE public.media_assets DROP CONSTRAINT IF EXISTS ck_media_assets_status")
    op.execute(
        "ALTER TABLE public.media_assets ADD CONSTRAINT ck_media_assets_status "
        f"CHECK (status IN ({PREVIOUS_STATUS_VALUES}))"
    )
    op.execute("ALTER TABLE public.media_assets ALTER COLUMN status SET DEFAULT 'análise'")
