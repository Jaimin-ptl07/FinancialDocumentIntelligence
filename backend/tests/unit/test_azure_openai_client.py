"""AzureOpenAIClient wraps the openai SDK. The SDK is monkeypatched -- no live
Azure OpenAI call."""

import pytest


def test_azure_client_calls_sdk_and_parses_label(monkeypatch: pytest.MonkeyPatch) -> None:
    from fdi.platform import semantic as sem

    calls: dict[str, object] = {}

    class _Msg:
        content = " balance_sheet \n"

    class _Choice:
        message = _Msg()

    class _Usage:
        prompt_tokens = 12
        completion_tokens = 3

    class _Resp:
        choices = [_Choice()]
        usage = _Usage()

    class _Completions:
        def create(self, **kwargs: object) -> _Resp:
            calls["kwargs"] = kwargs
            return _Resp()

    class _Chat:
        completions = _Completions()

    class _Client:
        def __init__(self, **kwargs: object) -> None:
            calls["init"] = kwargs

        chat = _Chat()

    monkeypatch.setattr(sem, "AzureOpenAI", _Client)
    c = sem.AzureOpenAIClient(
        endpoint="e", key="k", deployment="dep1", model="gpt-4o", api_version="2024-10-21"
    )
    result = c.classify("Balance Sheet", "Assets 100")
    assert result.label == "BALANCE_SHEET"
    assert result.usage is not None
    assert result.usage.input_tokens == 12
    assert result.usage.output_tokens == 3
    kwargs = calls["kwargs"]
    assert kwargs["model"] == "dep1"
    messages = kwargs["messages"]
    assert "Balance Sheet" in messages[1]["content"]


def test_from_settings_none_when_unconfigured() -> None:
    from fdi.platform.config import Settings
    from fdi.platform.semantic import AzureOpenAIClient

    s = Settings(
        database_url="postgresql+asyncpg://u:p@h:5432/db",
        broker_url="amqp://guest:guest@h:5672/%2F", result_backend="redis://h:6379/0",
        s3_endpoint="h:9000", s3_access_key="ak", s3_secret_key="sk",
    )
    assert AzureOpenAIClient.from_settings(s) is None
