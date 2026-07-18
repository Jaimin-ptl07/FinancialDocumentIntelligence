"""AzureOpenAIAgentClient wraps the openai SDK. The SDK is monkeypatched --
no live Azure OpenAI call. Mirrors test_azure_openai_client.py exactly."""

import json

import pytest


def _client_returning(monkeypatch: pytest.MonkeyPatch, msg: object) -> dict[str, object]:
    from fdi.platform import agent as ag

    calls: dict[str, object] = {}

    class _Choice:
        message = msg

    class _Resp:
        choices = [_Choice()]

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

    monkeypatch.setattr(ag, "AzureOpenAI", _Client)
    return calls


def test_agent_client_parses_tool_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    from fdi.platform.agent import AzureOpenAIAgentClient

    class _Fn:
        name = "search"
        arguments = json.dumps({"query": "revenue"})

    class _TC:
        id = "call_1"
        function = _Fn()

    class _Msg:
        content = None
        tool_calls = [_TC()]

    calls = _client_returning(monkeypatch, _Msg())
    c = AzureOpenAIAgentClient(
        endpoint="e", key="k", deployment="dep1", model="gpt-4o", api_version="2024-10-21"
    )
    resp = c.complete([{"role": "user", "content": "hi"}], [{"type": "function"}])
    assert resp.tool_calls == [
        __import__("fdi.core.agent", fromlist=["ToolCall"]).ToolCall(
            id="call_1", name="search", arguments={"query": "revenue"}
        )
    ]
    assert calls["kwargs"]["tool_choice"] == "auto"


def test_agent_client_parses_final_content(monkeypatch: pytest.MonkeyPatch) -> None:
    from fdi.platform.agent import AzureOpenAIAgentClient

    class _Msg:
        content = "the answer"
        tool_calls = None

    _client_returning(monkeypatch, _Msg())
    c = AzureOpenAIAgentClient(
        endpoint="e", key="k", deployment="dep1", model="gpt-4o", api_version="2024-10-21"
    )
    resp = c.complete([], [])
    assert resp.content == "the answer" and resp.tool_calls is None


def test_agent_client_malformed_tool_json_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    from fdi.platform.agent import AzureOpenAIAgentClient

    class _Fn:
        name = "search"
        arguments = "{not valid json"

    class _TC:
        id = "call_1"
        function = _Fn()

    class _Msg:
        content = None
        tool_calls = [_TC()]

    _client_returning(monkeypatch, _Msg())
    c = AzureOpenAIAgentClient(
        endpoint="e", key="k", deployment="dep1", model="gpt-4o", api_version="2024-10-21"
    )
    resp = c.complete([], [])
    assert resp.tool_calls is not None
    assert "_malformed_json" in resp.tool_calls[0].arguments


def test_from_settings_none_when_unconfigured() -> None:
    from fdi.platform.agent import AzureOpenAIAgentClient
    from fdi.platform.config import Settings

    s = Settings(
        database_url="postgresql+asyncpg://u:p@h:5432/db",
        broker_url="amqp://guest:guest@h:5672/%2F", result_backend="redis://h:6379/0",
        s3_endpoint="h:9000", s3_access_key="ak", s3_secret_key="sk",
    )
    assert AzureOpenAIAgentClient.from_settings(s) is None
