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
