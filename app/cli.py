import argparse
import asyncio

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.bootstrap import ensure_admin
from app.core.config import get_settings


async def bootstrap_admin() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.migration_database_url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    try:
        async with session_factory() as session:
            created = await ensure_admin(session, settings)
    finally:
        await engine.dispose()

    print("Administrador criado." if created else "Administrador ja existente; nenhuma alteracao realizada.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Operacoes administrativas do GeoMidia.")
    parser.add_argument("command", choices=("bootstrap-admin",))
    args = parser.parse_args()

    if args.command == "bootstrap-admin":
        asyncio.run(bootstrap_admin())


if __name__ == "__main__":
    main()
