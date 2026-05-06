from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration loaded from environment.

    Phase 1 only needs PORT, DATABASE_URL, DB_ADMIN_URL, INGEST_API_TOKEN.
    Later phases extend this — never delete fields, only add.
    """

    port: int = 3200
    database_url: str = "postgresql://placeholder@localhost/affine_ingest"
    db_admin_url: str | None = None
    ingest_api_token: str = "dev-token"
    version: str = "0.1.0"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)


settings = Settings()
