"""Rename media vehicles and add the modular electronic panel."""

from alembic import op

revision = "20260729_0005"
down_revision = "20260728_0004"
branch_labels = None
depends_on = None


MEDIA_TYPES = (
    "'outdoor', 'front light', 'triface', 'painel de led', "
    "'painel eletronico modular', 'empena', 'empena de led'"
)
LEGACY_MEDIA_TYPES = "'outdoor', 'front light', 'triface', 'painel de led', 'empena', 'empena de led'"


def upgrade() -> None:
    op.execute("ALTER TABLE media_assets DROP CONSTRAINT IF EXISTS ck_media_assets_media_type")
    op.execute("ALTER TABLE media_rules DROP CONSTRAINT IF EXISTS ck_media_rules_media_type")
    op.execute(
        f"ALTER TABLE media_assets ADD CONSTRAINT ck_media_assets_media_type CHECK (media_type IN ({MEDIA_TYPES}))"
    )
    op.execute(
        f"ALTER TABLE media_rules ADD CONSTRAINT ck_media_rules_media_type CHECK (media_type IN ({MEDIA_TYPES}))"
    )
    op.execute(
        """
        UPDATE media_rules
        SET name = CASE media_type
          WHEN 'front light' THEN 'Painel Iluminado - Front Light'
          WHEN 'triface' THEN 'Painel Iluminado - Triface'
          WHEN 'painel de led' THEN 'Painel Eletrônico Modular - Pequeno Porte'
          WHEN 'empena de led' THEN 'Empena Eletrônica'
          ELSE name
        END,
        description = CASE media_type
          WHEN 'front light' THEN 'Distancia minima para paineis iluminados do tipo Front Light.'
          WHEN 'triface' THEN 'Distancia minima para paineis iluminados do tipo Triface.'
          WHEN 'empena de led' THEN 'Distancia minima para empenas eletronicas.'
          ELSE description
        END
        WHERE media_type IN ('front light', 'triface', 'painel de led', 'empena de led')
        """
    )
    op.execute(
        """
        INSERT INTO media_rules (
          media_type, name, base_radius_meters, area_threshold_m2,
          radius_above_threshold_meters, description
        ) VALUES (
          'painel eletronico modular', 'Painel Eletrônico Modular', 1000, NULL,
          NULL, 'Distancia minima de 1000 m para paineis eletronicos modulares.'
        )
        ON CONFLICT (media_type) DO UPDATE SET
          name = EXCLUDED.name,
          base_radius_meters = EXCLUDED.base_radius_meters,
          area_threshold_m2 = EXCLUDED.area_threshold_m2,
          radius_above_threshold_meters = EXCLUDED.radius_above_threshold_meters,
          description = EXCLUDED.description,
          is_active = true
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE media_assets DROP CONSTRAINT IF EXISTS ck_media_assets_media_type")
    op.execute("ALTER TABLE media_rules DROP CONSTRAINT IF EXISTS ck_media_rules_media_type")
    op.execute(
        """
        UPDATE media_assets
        SET media_type = 'painel de led',
            radius_meters = CASE WHEN area_m2 > 5 THEN 1000 ELSE 250 END
        WHERE media_type = 'painel eletronico modular'
        """
    )
    op.execute("DELETE FROM media_rules WHERE media_type = 'painel eletronico modular'")
    op.execute(
        """
        UPDATE media_rules
        SET name = CASE media_type
          WHEN 'front light' THEN 'Front Light'
          WHEN 'triface' THEN 'Triface'
          WHEN 'painel de led' THEN 'Painel de LED'
          WHEN 'empena de led' THEN 'Empena de LED'
          ELSE name
        END,
        description = CASE media_type
          WHEN 'front light' THEN 'Distancia minima para veiculos front light.'
          WHEN 'triface' THEN 'Distancia minima para veiculos triface.'
          WHEN 'empena de led' THEN 'Distancia minima para empenas de LED.'
          ELSE description
        END
        WHERE media_type IN ('front light', 'triface', 'painel de led', 'empena de led')
        """
    )
    op.execute(
        f"ALTER TABLE media_assets ADD CONSTRAINT ck_media_assets_media_type "
        f"CHECK (media_type IN ({LEGACY_MEDIA_TYPES}))"
    )
    op.execute(
        f"ALTER TABLE media_rules ADD CONSTRAINT ck_media_rules_media_type "
        f"CHECK (media_type IN ({LEGACY_MEDIA_TYPES}))"
    )
