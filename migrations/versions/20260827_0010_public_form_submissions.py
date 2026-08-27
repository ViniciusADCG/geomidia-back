"""Add public form drafts and private attachment metadata."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260827_0010"
down_revision = "20260820_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "application_forms",
        sa.Column("number_of_faces", sa.String(length=30), nullable=True),
        schema="public",
    )
    op.create_table(
        "application_form_attachments",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("application_form_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("category", sa.String(length=40), nullable=False),
        sa.Column("object_path", sa.String(length=500), nullable=False),
        sa.Column("original_filename", sa.String(length=180), nullable=False),
        sa.Column("content_type", sa.String(length=120), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "size_bytes > 0 and size_bytes <= 10485760",
            name="ck_application_form_attachments_size",
        ),
        sa.ForeignKeyConstraint(
            ["application_form_id"],
            ["public.application_forms.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("object_path"),
        schema="public",
    )
    op.create_index(
        "ix_application_form_attachments_application_form_id",
        "application_form_attachments",
        ["application_form_id"],
        schema="public",
    )
    op.create_table(
        "public_submission_drafts",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("client_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("attachments", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("process_code", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        schema="public",
    )
    op.create_index(
        "ix_public_submission_drafts_client_fingerprint",
        "public_submission_drafts",
        ["client_fingerprint"],
        schema="public",
    )
    op.create_index(
        "ix_public_submission_drafts_rate_limit",
        "public_submission_drafts",
        ["client_fingerprint", "created_at"],
        schema="public",
    )

    for table in ("application_form_attachments", "public_submission_drafts"):
        op.execute(f'ALTER TABLE public."{table}" ENABLE ROW LEVEL SECURITY')

    op.execute(
        """
        DO $$
        DECLARE
          app_role text;
          app_table text;
        BEGIN
          FOREACH app_role IN ARRAY ARRAY['anon', 'authenticated'] LOOP
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = app_role) THEN
              FOREACH app_table IN ARRAY ARRAY['application_form_attachments', 'public_submission_drafts'] LOOP
                EXECUTE format('REVOKE ALL PRIVILEGES ON TABLE public.%I FROM %I', app_table, app_role);
              END LOOP;
            END IF;
          END LOOP;
        END $$
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'storage') THEN
            INSERT INTO storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
            VALUES (
              'application-form-attachments',
              'application-form-attachments',
              false,
              10485760,
              ARRAY['application/pdf', 'image/jpeg', 'image/png', 'image/gif', 'image/webp', 'image/bmp', 'image/heic', 'image/heif']
            )
            ON CONFLICT (id) DO UPDATE SET
              public = false,
              file_size_limit = EXCLUDED.file_size_limit,
              allowed_mime_types = EXCLUDED.allowed_mime_types;
          END IF;
        END $$
        """
    )


def downgrade() -> None:
    op.drop_index("ix_public_submission_drafts_rate_limit", table_name="public_submission_drafts", schema="public")
    op.drop_index(
        "ix_public_submission_drafts_client_fingerprint",
        table_name="public_submission_drafts",
        schema="public",
    )
    op.drop_table("public_submission_drafts", schema="public")
    op.drop_index(
        "ix_application_form_attachments_application_form_id",
        table_name="application_form_attachments",
        schema="public",
    )
    op.drop_table("application_form_attachments", schema="public")
    op.drop_column("application_forms", "number_of_faces", schema="public")
