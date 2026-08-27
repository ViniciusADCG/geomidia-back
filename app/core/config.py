from functools import lru_cache
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


class Settings(BaseSettings):
    environment: str = "development"
    database_url: str = "postgresql+asyncpg://geomidia:geomidia@localhost:5432/geomidia"
    database_direct_url: str | None = None
    cors_origins: str = "http://localhost:3000,http://localhost:5173"
    create_tables: bool = False
    jwt_secret: str = "development-only-change-me"
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 30
    bootstrap_admin_username: str | None = None
    bootstrap_admin_password: str | None = None
    bootstrap_admin_name: str = "Administrador GeoMidia"
    city_min_latitude: float = -20.65
    city_max_latitude: float = -20.30
    city_min_longitude: float = -54.80
    city_max_longitude: float = -54.40
    public_form_origins: str = "http://localhost:5173"
    public_submission_rate_limit: int = 5
    supabase_url: str | None = None
    supabase_service_role_key: str | None = None
    supabase_storage_bucket: str = "application-form-attachments"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @field_validator("database_url", "database_direct_url", mode="before")
    @classmethod
    def normalize_database_url(cls, value: str | None) -> str | None:
        if not isinstance(value, str):
            return value

        if value.startswith("postgres://"):
            value = "postgresql+asyncpg://" + value[len("postgres://") :]

        elif value.startswith("postgresql://"):
            value = "postgresql+asyncpg://" + value[len("postgresql://") :]

        parts = urlsplit(value)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        ssl_mode = query.pop("sslmode", None)
        query.pop("channel_binding", None)
        if ssl_mode and "ssl" not in query:
            query["ssl"] = ssl_mode

        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))

    @property
    def migration_database_url(self) -> str:
        return self.database_direct_url or self.database_url

    @property
    def uses_transaction_pooler(self) -> bool:
        return make_url(self.database_url).port == 6543

    @property
    def cors_origin_list(self) -> list[str]:
        origins = [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]
        if len(origins) == 1 and origins[0] == "*":
            return ["*"]
        return origins

    @property
    def cors_allow_credentials(self) -> bool:
        return self.cors_origin_list != ["*"]

    @property
    def public_form_origin_list(self) -> list[str]:
        return [origin.strip().rstrip("/") for origin in self.public_form_origins.split(",") if origin.strip()]

    @model_validator(mode="after")
    def validate_production_security(self) -> "Settings":
        if self.bootstrap_admin_password and len(self.bootstrap_admin_password) < 12:
            raise ValueError("BOOTSTRAP_ADMIN_PASSWORD deve ter pelo menos 12 caracteres.")
        if self.environment.lower() == "production":
            if self.jwt_secret == "development-only-change-me" or len(self.jwt_secret) < 32:
                raise ValueError("JWT_SECRET deve ter pelo menos 32 caracteres em producao.")
            if self.create_tables:
                raise ValueError("CREATE_TABLES deve ser false em producao; use Alembic.")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
