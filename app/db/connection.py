from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool


def engine_options_for(database_url: str) -> dict[str, object]:
    options: dict[str, object] = {"pool_pre_ping": True}
    if make_url(database_url).port == 6543:
        # Supabase's transaction pooler cannot retain prepared statements.
        options.update(poolclass=NullPool, connect_args={"prepare_threshold": None})
    return options
