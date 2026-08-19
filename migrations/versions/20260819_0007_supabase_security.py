"""Protect application tables from Supabase Data API roles."""

from alembic import op

revision = "20260819_0007"
down_revision = "20260729_0006"
branch_labels = None
depends_on = None

APPLICATION_TABLES = (
    "users",
    "process_counters",
    "media_rules",
    "media_assets",
    "application_forms",
    "activity_logs",
)


def upgrade() -> None:
    op.execute(
        """
        DO $$
        DECLARE
          postgis_schema text;
          database_role text := current_user;
          database_name text := current_database();
        BEGIN
          SELECT namespace.nspname INTO postgis_schema
          FROM pg_extension extension
          JOIN pg_namespace namespace ON namespace.oid = extension.extnamespace
          WHERE extension.extname = 'postgis';

          IF postgis_schema IS NOT NULL THEN
            EXECUTE format(
              'ALTER ROLE %I IN DATABASE %I SET search_path TO public, %I',
              database_role,
              database_name,
              postgis_schema
            );
          END IF;
        END $$
        """
    )

    for table in APPLICATION_TABLES:
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
              FOREACH app_table IN ARRAY ARRAY[
                'users',
                'process_counters',
                'media_rules',
                'media_assets',
                'application_forms',
                'activity_logs'
              ] LOOP
                EXECUTE format('REVOKE ALL PRIVILEGES ON TABLE public.%I FROM %I', app_table, app_role);
              END LOOP;
            END IF;
          END LOOP;
        END $$
        """
    )


def downgrade() -> None:
    for table in APPLICATION_TABLES:
        op.execute(f'ALTER TABLE public."{table}" DISABLE ROW LEVEL SECURITY')
