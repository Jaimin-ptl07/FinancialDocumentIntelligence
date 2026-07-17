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
    # Host-reachable endpoint used ONLY for presigning download URLs, distinct from
    # s3_endpoint (the in-network address put/get connect to). A presigned URL embeds
    # the endpoint its signing client was built with; under Compose that is
    # `minio:9000`, a name only the Docker network resolves, so the URL fails to
    # resolve in a host browser -- the one field a UI needs (the download link) is
    # dead. Set this to e.g. `localhost:9000` to sign URLs a host can fetch. Defaults
    # to None: where the in-network address IS reachable by clients (single-host prod,
    # tests), presigning falls back to s3_endpoint and this is a no-op.
    s3_public_endpoint: str | None = None
    s3_access_key: str
    s3_secret_key: str
    s3_bucket: str = "artifacts"
    s3_secure: bool = False

    worker_concurrency: int = 2
    log_level: str = "INFO"
    artifact_url_ttl_seconds: int = 3600
    max_upload_mb: int = 50

    # Azure Document Intelligence. Endpoint/key default to None (not required) so
    # the stack boots without Azure configured; the OCR stage using them is wired
    # separately. See docker/compose.yml x-backend-env and .env.example.
    azure_di_endpoint: str | None = None
    azure_di_key: str | None = None
    azure_di_model: str = "prebuilt-layout"
    azure_di_api_version: str = "2024-11-30"
    azure_di_polling_timeout_s: int = 600

    # Azure OpenAI (GPT). Endpoint/key default to None so the stack boots without
    # it configured; the semantic stage using them is wired separately. See
    # docker/compose.yml x-backend-env and .env.example. Real values come from the
    # sibling D:\FinancialDocIntelligence\.env (there named bare GPT_*), renamed
    # on the way in: GPT_SUBSCRIPTION_KEY -> FDI_GPT_KEY, GPT_ENDPOINT ->
    # FDI_GPT_ENDPOINT, GPT_DEPLOYMENT -> FDI_GPT_DEPLOYMENT, GPT_MODEL_NAME ->
    # FDI_GPT_MODEL, GPT_API_VERSION -> FDI_GPT_API_VERSION.
    gpt_endpoint: str | None = None
    gpt_key: str | None = None
    gpt_deployment: str = "gpt-4o"
    gpt_model: str = "gpt-4o"
    gpt_api_version: str = "2024-10-21"

    # Azure OpenAI embeddings. Endpoint/key default to None so the stack boots
    # without it configured; the embedding stage using them is wired separately.
    # See docker/compose.yml x-backend-env and .env.example. Unlike azure_di_*/
    # gpt_*, there is no pre-existing bare-named source in the sibling .env for
    # these -- an operator provisioning a real embeddings deployment sets these
    # directly here. embed_dim is passed to the SDK call as `dimensions=`
    # (text-embedding-3-* models only); it must match a dimension the deployed
    # model actually supports.
    embed_endpoint: str | None = None
    embed_key: str | None = None
    embed_deployment: str = "text-embedding-3-small"
    embed_api_version: str = "2024-10-21"
    embed_dim: int = 1536
    # Neo4j (graph loader, SP-9b). URI/password default to None so the stack boots
    # without Neo4j configured (the `graph` Compose profile is opt-in, never part
    # of `core`); the graph_load stage using them is wired separately. See
    # docker/compose.yml x-backend-env, the `neo4j` service's NEO4J_AUTH, and
    # .env.example. Unlike FDI_AZURE_DI_*/FDI_GPT_* (external SaaS, blank by
    # default), .env.example ships REAL working values here: this var also drives
    # the self-hosted `neo4j` Compose service's own bootstrap credential.
    neo4j_uri: str | None = None
    neo4j_user: str = "neo4j"
    neo4j_password: str | None = None

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
