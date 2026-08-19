from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool, text
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.core.config import get_settings
from app.db.models import Base

config = context.config
settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.migration_database_url.replace("%", "%%"))
if config.config_file_name is not None:
    fileConfig(config.config_file_name)
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    postgis_schema = connection.execute(
        text(
            """
            SELECT namespace.nspname
            FROM pg_extension extension
            JOIN pg_namespace namespace ON namespace.oid = extension.extnamespace
            WHERE extension.extname = 'postgis'
            """
        )
    ).scalar_one_or_none()
    if postgis_schema:
        quoted_schema = connection.dialect.identifier_preparer.quote(postgis_schema)
        connection.execute(text(f"SET search_path TO public, {quoted_schema}"))

    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    # The PostGIS lookup in do_run_migrations starts SQLAlchemy's implicit
    # transaction before Alembic enters its own transaction context. Own that
    # outer transaction here so successful migrations are committed instead of
    # being rolled back when the connection closes.
    async with connectable.begin() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    import asyncio

    asyncio.run(run_async_migrations())
