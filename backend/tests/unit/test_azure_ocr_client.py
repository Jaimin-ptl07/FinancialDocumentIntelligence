"""AzureOcrClient wraps the Azure SDK. The SDK is monkeypatched -- no live Azure call."""

import pytest

from fdi.platform import ocr
from fdi.platform.config import Settings
from tests.conftest import TEST_SIGNING_SECRET


class _Result:
    def as_dict(self) -> dict[str, object]:
        return {"content": "hi"}


class _Poller:
    def result(self) -> _Result:
        return _Result()


class _FakeDocumentIntelligenceClient:
    def __init__(self, *args: object, **kwargs: object) -> None:
        self.init_args = args
        self.init_kwargs = kwargs

    def begin_analyze_document(self, model: str, body: bytes) -> _Poller:
        self.analyze_call = (model, body)
        return _Poller()


def test_analyze_calls_sdk_and_returns_as_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_client_holder: dict[str, _FakeDocumentIntelligenceClient] = {}

    def _fake_client_factory(*args: object, **kwargs: object) -> _FakeDocumentIntelligenceClient:
        client = _FakeDocumentIntelligenceClient(*args, **kwargs)
        fake_client_holder["client"] = client
        return client

    monkeypatch.setattr(ocr, "DocumentIntelligenceClient", _fake_client_factory)
    monkeypatch.setattr(ocr, "AzureKeyCredential", lambda key: key)

    client = ocr.AzureOcrClient(
        endpoint="e",
        key="k",
        model="prebuilt-layout",
        api_version="2024-11-30",
        polling_timeout_s=600,
    )

    result = client.analyze(b"pdf")

    assert result == {"content": "hi"}
    fake_client = fake_client_holder["client"]
    assert fake_client.analyze_call == ("prebuilt-layout", b"pdf")


def test_from_settings_returns_none_without_endpoint_or_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for k, v in {
        "FDI_DATABASE_URL": "postgresql+asyncpg://u:p@h:5432/db",
        "FDI_BROKER_URL": "amqp://guest:guest@h:5672/%2F",
        "FDI_RESULT_BACKEND": "redis://h:6379/0",
        "FDI_S3_ENDPOINT": "h:9000",
        "FDI_S3_ACCESS_KEY": "ak",
        "FDI_S3_SECRET_KEY": "sk",
        "FDI_AUTH_SECRET": TEST_SIGNING_SECRET,
    }.items():
        monkeypatch.setenv(k, v)
    settings = Settings()

    assert settings.azure_di_endpoint is None
    assert ocr.AzureOcrClient.from_settings(settings) is None


def test_from_settings_returns_client_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in {
        "FDI_DATABASE_URL": "postgresql+asyncpg://u:p@h:5432/db",
        "FDI_BROKER_URL": "amqp://guest:guest@h:5672/%2F",
        "FDI_RESULT_BACKEND": "redis://h:6379/0",
        "FDI_S3_ENDPOINT": "h:9000",
        "FDI_S3_ACCESS_KEY": "ak",
        "FDI_S3_SECRET_KEY": "sk",
        "FDI_AUTH_SECRET": TEST_SIGNING_SECRET,
        "FDI_AZURE_DI_ENDPOINT": "https://example.cognitiveservices.azure.com",
        "FDI_AZURE_DI_KEY": "secret-key",
    }.items():
        monkeypatch.setenv(k, v)
    settings = Settings()

    client = ocr.AzureOcrClient.from_settings(settings)

    assert isinstance(client, ocr.AzureOcrClient)
    assert client._endpoint == "https://example.cognitiveservices.azure.com"
    assert client._key == "secret-key"
    assert client._model == "prebuilt-layout"
    assert client._api_version == "2024-11-30"
    assert client._polling_timeout_s == 600
