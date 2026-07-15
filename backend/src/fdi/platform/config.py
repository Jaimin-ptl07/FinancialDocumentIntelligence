"""Configuration. Environment only — Compose reads .env and injects it."""

import os
from functools import lru_cache

from pydantic import model_validator
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

    @model_validator(mode="after")
    def _reject_unknown_fdi_env_vars(self) -> "Settings":
        """Fail loudly on a typo'd FDI_ variable.

        `extra="forbid"` does NOT cover this. pydantic-settings resolves env vars
        by PULL -- for each declared field it looks up that field's env var -- so
        it never sees an FDI_ key that maps to no field, and `extra="forbid"` only
        polices the merged dict, which such a key never enters. Verified against
        pydantic_settings/sources/base.py. `extra="forbid"` therefore only guards
        constructor kwargs, and this app always constructs Settings() with none.

        Without this scan, `FDI_WORKER_CONCURRENCE=8` is silently ignored and the
        default of 2 is used -- the exact silent misbehaviour this class exists to
        prevent. (A typo on a REQUIRED field happens to fail anyway via
        `Field required`, which is easy to mistake for proof that this is covered.)
        """
        known = {f"FDI_{name}".upper() for name in type(self).model_fields}
        stray = sorted(
            k for k in os.environ
            if k.upper().startswith("FDI_") and k.upper() not in known
        )
        if stray:
            raise ValueError(
                f"Unknown FDI_ environment variable(s): {stray}. "
                f"Known: {sorted(known)}. Fix the typo, or remove the variable."
            )
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
