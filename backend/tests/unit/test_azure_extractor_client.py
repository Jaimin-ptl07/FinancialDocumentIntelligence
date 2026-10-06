"""AzureOpenAIExtractorClient wraps the openai SDK in JSON mode. The SDK is
monkeypatched -- no live Azure OpenAI call. Mirrors
test_azure_agent_client.py's _client_returning idiom exactly."""

import json

import pytest


def _client_returning(monkeypatch: pytest.MonkeyPatch, content: str | None) -> dict[str, object]:
    from fdi.platform import extractor as ex

    calls: dict[str, object] = {}
    # A separate name, not `content` -- inside a class body defined here, an
    # assignment target named `content` would shadow the enclosing
    # function's `content` parameter for the WHOLE class body (Python
    # resolves it as a not-yet-defined class-local rather than closing over
    # the parameter), raising NameError on the very line that tries to read
    # it. Verified: `def f(content):\n class M: content = content` raises
    # NameError even though `f`'s `content` is clearly in scope.
    _content = content

    class _Msg:
        content = _content

    class _Choice:
        message = _Msg()

    class _Usage:
        prompt_tokens = 30
        completion_tokens = 9

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

    monkeypatch.setattr(ex, "AzureOpenAI", _Client)
    return calls


def test_extract_parses_json_content(monkeypatch: pytest.MonkeyPatch) -> None:
    from fdi.platform.extractor import AzureOpenAIExtractorClient

    payload = {
        "value": 42, "source": {"page": 3, "snippet": "s", "evidence_ids": ["e1"]},
        "confidence": 0.8,
    }
    calls = _client_returning(monkeypatch, json.dumps(payload))
    c = AzureOpenAIExtractorClient(
        endpoint="e", key="k", deployment="dep1", model="gpt-4o", api_version="2024-10-21"
    )
    result = c.extract(system="sys", user="usr")
    assert result.data == payload
    assert result.usage is not None
    assert result.usage.input_tokens == 30
    assert result.usage.output_tokens == 9
    assert calls["kwargs"]["response_format"] == {"type": "json_object"}
    assert calls["kwargs"]["temperature"] == 0


def test_extract_malformed_json_returns_empty_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    from fdi.platform.extractor import AzureOpenAIExtractorClient

    _client_returning(monkeypatch, "{not valid json")
    c = AzureOpenAIExtractorClient(
        endpoint="e", key="k", deployment="dep1", model="gpt-4o", api_version="2024-10-21"
    )
    assert c.extract(system="sys", user="usr").data == {}


def test_extract_empty_content_returns_empty_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    from fdi.platform.extractor import AzureOpenAIExtractorClient

    _client_returning(monkeypatch, None)
    c = AzureOpenAIExtractorClient(
        endpoint="e", key="k", deployment="dep1", model="gpt-4o", api_version="2024-10-21"
    )
    assert c.extract(system="sys", user="usr").data == {}


def test_extract_non_dict_json_returns_empty_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    from fdi.platform.extractor import AzureOpenAIExtractorClient

    _client_returning(monkeypatch, json.dumps([1, 2, 3]))
    c = AzureOpenAIExtractorClient(
        endpoint="e", key="k", deployment="dep1", model="gpt-4o", api_version="2024-10-21"
    )
    assert c.extract(system="sys", user="usr").data == {}


def test_from_settings_none_when_unconfigured() -> None:
    from fdi.platform.config import Settings
    from fdi.platform.extractor import AzureOpenAIExtractorClient

    s = Settings(
        database_url="postgresql+asyncpg://u:p@h:5432/db",
        broker_url="amqp://guest:guest@h:5672/%2F", result_backend="redis://h:6379/0",
        s3_endpoint="h:9000", s3_access_key="ak", s3_secret_key="sk",
        auth_secret="test-signing-secret-0123456789abcdef0123456789abcdef",
    )
    assert AzureOpenAIExtractorClient.from_settings(s) is None
