"""Add administrator-managed media business rules."""

from alembic import op

revision = "20260723_0002"
down_revision = "20260715_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS media_rules (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          media_type varchar(32) NOT NULL UNIQUE,
          name varchar(120) NOT NULL,
          base_radius_meters integer NOT NULL,
          area_threshold_m2 double precision,
          radius_above_threshold_meters integer,
          description text,
          is_active boolean NOT NULL DEFAULT true,
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          CONSTRAINT ck_media_rules_media_type CHECK (
            media_type IN ('outdoor', 'front light', 'triface', 'painel de led',
              'painel eletronico modular', 'empena', 'empena de led')
          ),
          CONSTRAINT ck_media_rules_base_radius CHECK (base_radius_meters > 0),
          CONSTRAINT ck_media_rules_area_threshold CHECK (area_threshold_m2 IS NULL OR area_threshold_m2 > 0),
          CONSTRAINT ck_media_rules_threshold_radius CHECK (
            radius_above_threshold_meters IS NULL OR radius_above_threshold_meters > 0
          ),
          CONSTRAINT ck_media_rules_threshold_pair CHECK (
            (area_threshold_m2 IS NULL) = (radius_above_threshold_meters IS NULL)
          )
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_media_rules_media_type ON media_rules (media_type)")
    op.execute(
        """
        INSERT INTO media_rules (
          media_type, name, base_radius_meters, area_threshold_m2,
          radius_above_threshold_meters, description
        ) VALUES
          ('outdoor', 'Outdoor', 80, NULL, NULL, 'Distancia minima para veiculos do tipo outdoor.'),
          ('front light', 'Painel Iluminado - Front Light', 80, NULL, NULL, 'Distancia minima para paineis iluminados do tipo Front Light.'),
          ('triface', 'Painel Iluminado - Triface', 80, NULL, NULL, 'Distancia minima para paineis iluminados do tipo Triface.'),
          ('painel de led', 'Painel Eletrônico Modular - Pequeno Porte', 250, 5, 1000, 'Acima de 5 m2 aplica-se o raio ampliado.'),
          ('painel eletronico modular', 'Painel Eletrônico Modular', 1000, NULL, NULL, 'Distancia minima de 1000 m para paineis eletronicos modulares.'),
          ('empena', 'Empena', 80, NULL, NULL, 'Distancia minima para empenas.'),
          ('empena de led', 'Empena Eletrônica', 1000, NULL, NULL, 'Distancia minima para empenas eletronicas.')
        ON CONFLICT (media_type) DO NOTHING
        """
    )
    op.execute("DROP TRIGGER IF EXISTS trg_media_rules_updated_at ON media_rules")
    op.execute(
        """
        CREATE TRIGGER trg_media_rules_updated_at BEFORE UPDATE ON media_rules
          FOR EACH ROW EXECUTE FUNCTION set_updated_at()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_media_rules_updated_at ON media_rules")
    op.execute("DROP TABLE IF EXISTS media_rules")
