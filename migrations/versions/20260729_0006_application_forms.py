"""Add application forms linked to media assets."""

from alembic import op

revision = "20260729_0006"
down_revision = "20260729_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS application_forms (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          asset_id uuid NOT NULL UNIQUE REFERENCES media_assets(id) ON DELETE CASCADE,
          company_responsible varchar(120) NOT NULL,
          municipal_registration varchar(60) NOT NULL,
          property_registration varchar(60) NOT NULL,
          street varchar(180) NOT NULL,
          number varchar(30) NOT NULL,
          district varchar(120) NOT NULL,
          postal_code varchar(9) NOT NULL,
          latitude double precision NOT NULL,
          longitude double precision NOT NULL,
          media_type varchar(32) NOT NULL,
          area_m2 double precision NOT NULL,
          bottom_height_m double precision NOT NULL,
          requester_email varchar(160) NOT NULL,
          attachment_links text,
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          CONSTRAINT ck_application_forms_media_type CHECK (
            media_type IN ('outdoor', 'front light', 'triface', 'painel de led',
              'painel eletronico modular', 'empena', 'empena de led')
          ),
          CONSTRAINT ck_application_forms_latitude CHECK (latitude BETWEEN -90 AND 90),
          CONSTRAINT ck_application_forms_longitude CHECK (longitude BETWEEN -180 AND 180),
          CONSTRAINT ck_application_forms_area CHECK (area_m2 > 0),
          CONSTRAINT ck_application_forms_bottom_height CHECK (bottom_height_m >= 0)
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_application_forms_asset_id ON application_forms (asset_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_application_forms_company ON application_forms (company_responsible)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_application_forms_municipal_registration "
        "ON application_forms (municipal_registration)"
    )
    op.execute("DROP TRIGGER IF EXISTS trg_application_forms_updated_at ON application_forms")
    op.execute(
        """
        CREATE TRIGGER trg_application_forms_updated_at BEFORE UPDATE ON application_forms
          FOR EACH ROW EXECUTE FUNCTION set_updated_at()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_application_forms_updated_at ON application_forms")
    op.execute("DROP TABLE IF EXISTS application_forms")
