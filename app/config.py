from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    environment: str = "development"
    database_url: str = "postgresql+psycopg://chat:chat@localhost:5432/chat"
    jwt_secret: str = "change-me"
    jwt_issuer: str = "lamya"
    access_token_minutes: int = 60
    cors_origins: str = "http://localhost:5173,https://lamya.aureixis.com"
    admin_email: str = "admin@example.com"
    admin_password: str = "change-me-now"
    translation_provider: str = "local"
    translation_api_url: str | None = None
    translation_api_key: str | None = None
    translation_model: str | None = None
    ai_provider: str = "local"
    ai_api_url: str | None = None
    ai_api_key: str | None = None
    ai_model: str = "local-safe"
    ai_max_output_tokens: int = 1000
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = 384
    embeddings_enabled: bool = True
    secrets_encryption_key: str | None = None
    secret_file_root: str = "/run/secrets"
    moderation_fail_closed: bool = False
    agent_lease_seconds: int = 300
    max_pending_actions_per_bot: int = 1000
    max_pending_actions_per_organization: int = 10_000
    metrics_token: str | None = None
    audit_export_url: str | None = None
    audit_export_secret_ref: str | None = None
    audit_export_batch_size: int = 100
    agent_worker_enabled: bool = True
    agent_poll_seconds: float = 2.0
    agent_batch_size: int = 10
    auto_create_schema: bool = True
    max_request_bytes: int = 1_000_000

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, extra="ignore")

    @property
    def allowed_origins(self) -> list[str]:
        return [
            origin.strip().rstrip("/") for origin in self.cors_origins.split(",") if origin.strip()
        ]

    @property
    def sqlalchemy_database_url(self) -> str:
        if not self.database_url.strip():
            raise ValueError(
                "DATABASE_URL is empty. Add a Railway reference to the PostgreSQL DATABASE_URL variable."
            )
        url = self.database_url.strip()
        if url.startswith("postgres://"):
            return "postgresql+psycopg://" + url.removeprefix("postgres://")
        if url.startswith("postgresql://"):
            return "postgresql+psycopg://" + url.removeprefix("postgresql://")
        return url

    @property
    def is_production(self) -> bool:
        return self.environment.lower() == "production"

    def validate_vector_configuration(self) -> None:
        from .vector_config import VECTOR_DIMENSIONS

        if self.embedding_dimensions != VECTOR_DIMENSIONS:
            raise ValueError(
                f"EMBEDDING_DIMENSIONS must be {VECTOR_DIMENSIONS}; changing vector size requires a schema migration"
            )

        if self.is_production and self.moderation_fail_closed is False:
            raise ValueError("MODERATION_FAIL_CLOSED must be true in production")
        if self.is_production and (not self.metrics_token or len(self.metrics_token) < 24):
            raise ValueError("METRICS_TOKEN must contain at least 24 characters in production")
        if self.is_production and self.auto_create_schema:
            raise ValueError("AUTO_CREATE_SCHEMA must be false in production; use Alembic")
        if self.is_production and "*" in self.allowed_origins:
            raise ValueError("Wildcard CORS origins are not allowed in production")
        for name, url in (
            ("AI_API_URL", self.ai_api_url),
            ("TRANSLATION_API_URL", self.translation_api_url),
            ("AUDIT_EXPORT_URL", self.audit_export_url),
        ):
            if self.is_production and url and not url.startswith("https://"):
                raise ValueError(f"{name} must use HTTPS in production")
        if self.agent_lease_seconds < 120:
            raise ValueError("AGENT_LEASE_SECONDS must be at least 120")
        if self.secrets_encryption_key and len(self.secrets_encryption_key) < 32:
            raise ValueError("SECRETS_ENCRYPTION_KEY must contain at least 32 characters")
        if self.max_pending_actions_per_bot < 1:
            raise ValueError("MAX_PENDING_ACTIONS_PER_BOT must be positive")
        if self.max_pending_actions_per_organization < 1:
            raise ValueError("MAX_PENDING_ACTIONS_PER_ORGANIZATION must be positive")


@lru_cache
def get_settings() -> Settings:
    return Settings()
