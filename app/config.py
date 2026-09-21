from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = "development"
    database_url: str = "sqlite:///./integration_gateway.db"
    target_webhook_url: str | None = None
    delivery_timeout_seconds: float = 5.0
    delivery_max_attempts: int = 3
    delivery_base_backoff_seconds: float = 0.2

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
