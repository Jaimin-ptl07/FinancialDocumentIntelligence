"""Configuration. Environment only — Compose reads .env and injects it."""

import os
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic_settings.sources import EnvSettingsSource, PydanticBaseSettingsSource

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

_SCHEMA_RELPATH = Path("docs") / "architecture" / "field-extraction-schema.v4_1.json"


def _first_existing(*candidates: Path) -> Path:
    """The first candidate that is a real file, else the first candidate.

    Falling back to `candidates[0]` rather than raising keeps Settings
    constructible in a checkout that has not got the file, which the unit
    suite relies on; the caller that actually reads it reports the miss.
    """
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


def _default_schema_path() -> Path:
    """Where the seed extraction schema lives, in BOTH execution contexts.

    `_REPO_ROOT` is `parents[4]` of this file, which is the repo root only
    for the editable install (`backend/src/fdi/platform/config.py`). Inside
    the built image the package is at
    `/app/.venv/lib/python3.13/site-packages/fdi/platform/config.py`, so
    `parents[4]` is `/app/.venv/lib` and the default pointed at a file that
    has never existed there.

    That failed silently, which is the damaging part: `load_schema` returns
    `({}, "seed")` for an unreadable seed, so `GET /schema` answered 200 with
    zero modules and `POST /extract/document` extracted nothing at all, with
    no error anywhere. The Dockerfile COPYs the file to `<WORKDIR>/docs/...`,
    so cwd is the candidate that resolves in the image.
    """
    return _first_existing(
        _REPO_ROOT / _SCHEMA_RELPATH,
        Path.cwd() / _SCHEMA_RELPATH,
    )


class _EmptyIsUnsetEnvSource(EnvSettingsSource):
    """Treat `FDI_FOO=""` as "not set" rather than as the value `""`.

    `docker/compose.yml` passes every FDI_ variable through unconditionally
    (`FDI_EMBED_DIM: ${FDI_EMBED_DIM}`), so a variable the operator never put in
    their `.env` reaches the process as an empty string rather than being
    absent. Nothing typed can parse that: `embed_dim` raises `unable to parse
    string as an integer`, and a complex field like `cors_origins` fails even
    earlier, inside the JSON decoder, as a `SettingsError` that names no fix.

    The result was a stack that refused to boot over a dozen settings which all
    have working defaults -- and the operator's only clue was a pydantic
    traceback. Filtering here, in `_load_env_vars`, is what makes this work for
    complex fields too: it runs BEFORE the JSON decode that would otherwise
    raise.

    Deliberately `== ""` and not `.strip() == ""`: a variable set to a single
    space is someone doing something odd on purpose, and should still fail
    loudly rather than be silently discarded.
    """

    def _load_env_vars(self) -> Mapping[str, str | None]:
        return {k: v for k, v in super()._load_env_vars().items() if v != ""}


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
    # Schema v2 (RT-9): dense vector + BM25 sparse vector + stored text. The
    # BM25 Function cannot be added to an existing collection, so the cutover
    # is a NEW collection rather than an alter -- `fdi_sections` is left
    # in place, which makes rollback a one-line revert of this default.
    milvus_collection: str = "fdi_chunks_v2"

    # Cross-document entity resolution (SP-16). Cosine-similarity threshold for
    # single-linkage clustering of whole-document embedding vectors: two documents
    # join the same entity when their vectors are cosine-similar at or above this.
    # 0.85 balances recall (a company's filings across years cluster together)
    # against precision (distinct companies stay apart); tune against real data via
    # this var without a redeploy, or per-request via POST /entities/resolve's body.
    entity_sim_threshold: float = 0.85

    # Browser origins allowed to read this API's responses cross-origin. The
    # default is the Next.js dev server (SP-1/SP-18) -- the exact value that
    # was hardcoded in fdi.api.app until SP-19 -- so an unset environment
    # behaves identically to before.
    #
    # This is a REPLACEMENT, not an extension: setting it drops localhost:3000
    # unless it is listed again. An allowlist that only ever grows is not an
    # allowlist, and a production deployment must be able to say "only this
    # origin".
    #
    # pydantic-settings parses a complex-typed field from the environment as
    # JSON, so the variable is a JSON array -- `FDI_CORS_ORIGINS=["https://
    # fdi.example"]`, not a comma-separated string. Never default this to
    # ["*"]: a wildcard passes every happy-path CORS test while granting every
    # site on the internet read access to this API from a victim's browser
    # (see tests/unit/test_cors.py's unlisted-origin test).
    cors_origins: list[str] = ["http://localhost:3000"]

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
    extraction_schema_path: str = str(_default_schema_path())

    # Costing (docs/superpowers/specs/2026-07-28-costing-feature-design.md).
    # OCR is billed 1 INR/page flat. LLM is billed per Azure OpenAI's own
    # $/M-token pricing, converted to INR at READ time (never persisted per
    # event -- a rate change here recomputes every historical total, by
    # design: spec decision 2). usd_to_inr_rate is a static configured
    # value, not fetched from any FX API (spec sec 6, explicitly deferred).
    ocr_cost_per_page_inr: float = 1.0
    llm_input_cost_per_mtok_usd: float = 2.5
    llm_output_cost_per_mtok_usd: float = 10.0
    usd_to_inr_rate: float = 95.0

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Swap the env source for one that reads `FOO=""` as unset.

        Order is otherwise untouched, and `dotenv_settings` stays inert because
        `env_file=None` -- see this class's model_config.
        """
        return (
            init_settings,
            _EmptyIsUnsetEnvSource(settings_cls),
            dotenv_settings,
            file_secret_settings,
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
