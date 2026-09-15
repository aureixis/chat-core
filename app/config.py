from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg://chat:chat@localhost:5432/chat"
    jwt_secret: str = "change-me"
    access_token_minutes: int = 1440
    cors_origins: str = "http://localhost:5173,https://lamya.aureixis.com"
    admin_email: str = "admin@example.com"
    admin_password: str = "change-me-now"
    translation_provider: str = "local"
    translation_api_url: str | None = None
    translation_api_key: str | None = None
    translation_model: str | None = None

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, extra="ignore")

    @property
    def allowed_origins(self) -> list[str]:
        return [origin.strip().rstrip("/") for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def sqlalchemy_database_url(self) -> str:
        if not self.database_url.strip():
            raise ValueError("DATABASE_URL is empty. Add a Railway reference to the PostgreSQL DATABASE_URL variable.")
        url = self.database_url.strip()
        if url.startswith("postgres://"):
            return "postgresql+psycopg://" + url.removeprefix("postgres://")
        if url.startswith("postgresql://"):
            return "postgresql+psycopg://" + url.removeprefix("postgresql://")
        return url


@lru_cache
def get_settings() -> Settings:
    return Settings()
