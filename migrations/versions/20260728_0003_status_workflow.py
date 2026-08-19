"""Update media asset status workflow."""

from alembic import op

revision = "20260728_0003"
down_revision = "20260723_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE media_assets DROP CONSTRAINT IF EXISTS ck_media_assets_status")
    op.execute(
        """
        UPDATE media_assets
        SET status = CASE status
          WHEN 'Aprovado' THEN 'aprovado'
          WHEN 'Reprovado' THEN 'irregular'
          WHEN 'Pendente' THEN 'análise'
          ELSE status
        END
        """
    )
    op.execute(
        """
        ALTER TABLE media_assets ADD CONSTRAINT ck_media_assets_status CHECK (
          status IN ('aprovado', 'irregular', 'análise', 'exigência', 'vencido', 'cartografia', 'jurídico', 'vistoria')
        )
        """
    )
    op.execute("ALTER TABLE media_assets ALTER COLUMN status SET DEFAULT 'análise'")


def downgrade() -> None:
    op.execute("ALTER TABLE media_assets DROP CONSTRAINT IF EXISTS ck_media_assets_status")
    op.execute(
        """
        UPDATE media_assets
        SET status = CASE status
          WHEN 'aprovado' THEN 'Aprovado'
          WHEN 'irregular' THEN 'Reprovado'
          WHEN 'análise' THEN 'Pendente'
          WHEN 'exigência' THEN 'Pendente'
          WHEN 'vencido' THEN 'Pendente'
          WHEN 'cartografia' THEN 'Pendente'
          WHEN 'jurídico' THEN 'Pendente'
          WHEN 'vistoria' THEN 'Pendente'
          ELSE status
        END
        """
    )
    op.execute(
        """
        ALTER TABLE media_assets ADD CONSTRAINT ck_media_assets_status CHECK (
          status IN ('Aprovado', 'Reprovado', 'Pendente')
        )
        """
    )
    op.execute("ALTER TABLE media_assets ALTER COLUMN status SET DEFAULT 'Pendente'")