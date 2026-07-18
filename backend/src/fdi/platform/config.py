"""Configuration. Environment only — Compose reads .env and injects it."""

import os
from functools import lru_cache
from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Repo root, computed from THIS file's own on-disk location
# (backend/src/fdi/platform/config.py -> platform -> fdi -> src -> backend ->
# repo root), not a bare relative string. A bare "docs/architecture/..."
# default only resolves when cwd happens to BE the repo root, which is
# neither of this project's two real execution contexts: `uv run --directory
# backend` (cwd backend/) or the built Docker image's `WORKDIR /app`. This
# is cwd-independent for local/test execution (the package installs
# editable, so __file__ is this source path); see backend/Dockerfile's COPY
# of the schema file for the deployed-image side of this.
_REPO_ROOT = Path(__file__).resolve().parents[4]


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

    # BGE-small embeddings (fastembed, local CPU/ONNX). Swapped in at SP-13b,
    # replacing SP-13's Azure OpenAI embedding backend -- BAAI/bge-small-en-v1.5
    # runs locally, so there is no endpoint/key/api_version to configure, unlike
    # azure_di_*/gpt_*. embed_dim is the model's native output size (384) and is
    # NOT sent to fastembed as a request parameter (unlike Azure's `dimensions=`
    # truncation option, which fastembed has no equivalent of) -- it is
    # provenance/documentation only, plus what MilvusVectorStore.from_settings
    # (SP-14) reads to size the Milvus collection. Change it only alongside
    # embed_model, and only to a value that model actually produces.
    embed_model: str = "BAAI/bge-small-en-v1.5"
    embed_dim: int = 384
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

    # Milvus (vector index, SP-14). URI defaults to None so the stack boots
    # without Milvus configured (the `vector` Compose profile is opt-in, never
    # part of `core`); the index stage and the /search route using it are wired
    # separately. See docker/compose.yml x-backend-env and .env.example. Like
    # FDI_NEO4J_* (self-hosted via Compose), .env.example ships a REAL working
    # value here -- but unlike Neo4j, Milvus standalone has no bootstrap
    # credential of its own to keep in sync, so there is no auth pairing to get
    # wrong. Dimension is deliberately NOT a separate Milvus-specific field: the
    # collection is created with FDI_EMBED_DIM (SP-13) -- the same
    # dimensionality the embedder actually produces -- so a second,
    # independently-set "Milvus dim" can never silently disagree with it.
    milvus_uri: str | None = None
    milvus_collection: str = "fdi_sections"

    # Cross-document entity resolution (SP-16). Cosine-similarity threshold for
    # single-linkage clustering of whole-document embedding vectors: two documents
    # join the same entity when their vectors are cosine-similar at or above this.
    # 0.85 balances recall (a company's filings across years cluster together)
    # against precision (distinct companies stay apart); tune against real data via
    # this var without a redeploy, or per-request via POST /entities/resolve's body.
    entity_sim_threshold: float = 0.85

    # Extractor pipeline (schema-driven RAG field extraction, POST
    # /extract/document). Always has a working default -- the schema ships
    # in the repo -- so, like azure_di_model/embed_model, this is inert
    # config: present in the trio for discoverability/override, not because
    # the stack needs it set to boot. No new LLM config: the extractor
    # reuses FDI_GPT_* (fdi.platform.extractor.AzureOpenAIExtractorClient).
    # The default is computed from THIS module's own location (_REPO_ROOT
    # above), not a bare repo-root-relative string -- see backend/
    # Dockerfile's COPY of this exact file into the image's WORKDIR for the
    # deployed-image side of making the default resolve.
    extraction_schema_path: str = str(
        _REPO_ROOT / "docs" / "architecture" / "field-extraction-schema.json"
    )

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
