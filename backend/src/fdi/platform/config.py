"""Configuration. Environment only — Compose reads .env and injects it."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FDI_",
        env_file=None,      # already the default; explicit so the intent is unmissable
        case_sensitive=False,
        extra="forbid",     # a typo'd env var must fail loudly, not be ignored
    )

    database_url: str
    broker_url: str
    result_backend: str

    s3_endpoint: str
    s3_access_key: str
    s3_secret_key: str
    s3_bucket: str = "artifacts"
    s3_secure: bool = False

    worker_concurrency: int = 2
    log_level: str = "INFO"
    artifact_url_ttl_seconds: int = 3600


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
