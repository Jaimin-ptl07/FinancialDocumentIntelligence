import pytest

from fdi.core.agent import AgentResponse, ToolCall
from fdi.pipeline.agent import AgentClient, FakeAgentClient


def test_fake_agent_client_replays_scripted_responses_in_order() -> None:
    r1 = AgentResponse(tool_calls=[ToolCall(id="1", name="search", arguments={"query": "q"})])
    r2 = AgentResponse(content="final")
    fake = FakeAgentClient([r1, r2])
    assert isinstance(fake, AgentClient)  # runtime_checkable structural match
    assert fake.complete([], []) is r1
    assert fake.complete([], []) is r2
    assert len(fake.calls) == 2


def test_fake_agent_client_records_messages_and_tools_per_call() -> None:
    fake = FakeAgentClient([AgentResponse(content="ok")])
    messages = [{"role": "user", "content": "hi"}]
    tools = [{"type": "function", "function": {"name": "search"}}]
    fake.complete(messages, tools)
    assert fake.calls == [(messages, tools)]


def test_fake_agent_client_raises_a_diagnosable_error_on_overcall() -> None:
    fake = FakeAgentClient([AgentResponse(content="ok")])
    fake.complete([], [])
    with pytest.raises(AssertionError, match="exhausted"):
        fake.complete([], [])
