from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg://chat:chat@localhost:5432/chat"
    jwt_secret: str = "change-me"
    access_token_minutes: int = 1440
    cors_origins: str = "http://localhost:5173"
    admin_email: str = "admin@example.com"
    admin_password: str = "change-me-now"
    translation_provider: str = "local"
    translation_api_url: str | None = None
    translation_api_key: str | None = None
    translation_model: str | None = None

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, extra="ignore")

    @property
    def allowed_origins(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
