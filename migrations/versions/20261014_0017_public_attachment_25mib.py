"""Raise the public attachment size limit to 25 MiB."""

from alembic import op

revision = "20261014_0017"
down_revision = "20261007_0016"
branch_labels = None
depends_on = None

BUCKET_ID = "application-form-attachments"
CHECK_NAME = "ck_application_form_attachments_size"
OLD_LIMIT = 10 * 1024 * 1024
NEW_LIMIT = 25 * 1024 * 1024


def set_bucket_limit(size_bytes: int) -> None:
    # Local CI can run without the Supabase storage schema; never create a bucket.
    op.execute(
        f"""
        DO $$ BEGIN
          IF to_regclass('storage.buckets') IS NOT NULL THEN
            UPDATE storage.buckets
            SET file_size_limit = {size_bytes}
            WHERE id = '{BUCKET_ID}';
            IF NOT FOUND THEN
              RAISE EXCEPTION 'Public attachment Storage bucket is missing';
            END IF;
          END IF;
        END $$
        """
    )


def upgrade() -> None:
    op.drop_constraint(CHECK_NAME, "application_form_attachments", schema="public")
    op.create_check_constraint(
        CHECK_NAME,
        "application_form_attachments",
        f"size_bytes > 0 and size_bytes <= {NEW_LIMIT}",
        schema="public",
    )
    set_bucket_limit(NEW_LIMIT)


def downgrade() -> None:
    # Refuse to tighten the constraint while any 10–25 MiB attachment exists.
    op.execute(
        f"""
        DO $$ BEGIN
          IF EXISTS (
            SELECT 1 FROM public.application_form_attachments
            WHERE size_bytes > {OLD_LIMIT}
          ) THEN
            RAISE EXCEPTION 'Downgrade requires removing attachments larger than 10 MiB';
          END IF;
        END $$
        """
    )
    op.drop_constraint(CHECK_NAME, "application_form_attachments", schema="public")
    op.create_check_constraint(
        CHECK_NAME,
        "application_form_attachments",
        f"size_bytes > 0 and size_bytes <= {OLD_LIMIT}",
        schema="public",
    )
    set_bucket_limit(OLD_LIMIT)
