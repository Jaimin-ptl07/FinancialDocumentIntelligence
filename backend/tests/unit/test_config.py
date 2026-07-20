from pathlib import Path

import pytest
from pydantic import ValidationError

from fdi.platform.config import Settings

REQUIRED = {
    "FDI_DATABASE_URL": "postgresql+asyncpg://u:p@h:5432/db",
    "FDI_BROKER_URL": "amqp://guest:guest@h:5672/%2F",
    "FDI_RESULT_BACKEND": "redis://h:6379/0",
    "FDI_S3_ENDPOINT": "h:9000",
    "FDI_S3_ACCESS_KEY": "ak",
    "FDI_S3_SECRET_KEY": "sk",
}


def test_reads_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in REQUIRED.items():
        monkeypatch.setenv(k, v)
    s = Settings()
    assert s.database_url == REQUIRED["FDI_DATABASE_URL"]
    assert s.s3_bucket == "artifacts"
    assert s.worker_concurrency == 2


def test_missing_required_setting_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in REQUIRED:
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(ValidationError):
        Settings()


def test_unknown_setting_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in REQUIRED.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("FDI_TYPO_SETTING", "x")
    with pytest.raises(ValidationError):
        Settings()


def test_does_not_read_dotenv_files(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """12-factor: Compose reads .env and injects env vars. The app reads env only."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("FDI_DATABASE_URL=postgresql+asyncpg://leaked/db\n")
    for k in REQUIRED:
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(ValidationError):
        Settings()


def test_azure_di_settings_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Azure DI fields have safe defaults so the stack boots without Azure configured."""
    for k, v in REQUIRED.items():
        monkeypatch.setenv(k, v)
    s = Settings()
    assert s.azure_di_endpoint is None
    assert s.azure_di_key is None
    assert s.azure_di_model == "prebuilt-layout"
    assert s.azure_di_api_version == "2024-11-30"
    assert s.azure_di_polling_timeout_s == 600


def test_gpt_settings_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Azure OpenAI (GPT) fields have safe defaults so the stack boots without it."""
    for k, v in REQUIRED.items():
        monkeypatch.setenv(k, v)
    s = Settings()
    assert s.gpt_endpoint is None
    assert s.gpt_key is None
    assert s.gpt_deployment == "gpt-4o"
    assert s.gpt_model == "gpt-4o"
    assert s.gpt_api_version == "2024-10-21"


def test_embed_settings_default(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in REQUIRED.items():
        monkeypatch.setenv(k, v)
    s = Settings()
    assert s.embed_model == "BAAI/bge-small-en-v1.5"
    assert s.embed_dim == 384


def test_neo4j_settings_default(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in REQUIRED.items():
        monkeypatch.setenv(k, v)
    s = Settings()
    assert s.neo4j_uri is None
    assert s.neo4j_user == "neo4j"
    assert s.neo4j_password is None


def test_milvus_settings_default(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in REQUIRED.items():
        monkeypatch.setenv(k, v)
    s = Settings()
    assert s.milvus_uri is None
    assert s.milvus_collection == "fdi_sections"


def test_entity_resolution_settings_default(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in REQUIRED.items():
        monkeypatch.setenv(k, v)
    s = Settings()
    assert s.entity_sim_threshold == 0.85


def test_extraction_schema_path_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The default must resolve to the committed schema file regardless of
    cwd -- it is computed from this package's own on-disk location
    (backend/src/fdi/platform/config.py), not left bare-relative (which only
    ever resolved from a repo-root cwd, neither of the project's two real
    execution contexts: `uv run --directory backend` or the Docker image's
    `WORKDIR /app`). Proven here by chdir-ing somewhere else first."""
    for k, v in REQUIRED.items():
        monkeypatch.setenv(k, v)
    monkeypatch.chdir(tmp_path)
    s = Settings()
    p = Path(s.extraction_schema_path)
    assert p.is_absolute()
    assert p.as_posix().endswith("docs/architecture/field-extraction-schema.json")
    assert p.exists()


def test_cors_origins_default(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in REQUIRED.items():
        monkeypatch.setenv(k, v)
    assert Settings().cors_origins == ["http://localhost:3000"]


def test_cors_origins_from_environment_is_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """pydantic-settings parses a complex-typed field from the environment as
    JSON, so the var is a JSON array -- not a comma-separated string."""
    for k, v in REQUIRED.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv(
        "FDI_CORS_ORIGINS", '["http://localhost:3000", "https://fdi.example"]'
    )
    assert Settings().cors_origins == ["http://localhost:3000", "https://fdi.example"]
