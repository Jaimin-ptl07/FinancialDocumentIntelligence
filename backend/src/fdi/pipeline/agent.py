"""The AgentClient seam: complete(messages, tools) -> AgentResponse.
AzureOpenAIAgentClient (fdi.platform.agent, does I/O) structurally satisfies
this without importing it -- platform may not import pipeline. FakeAgentClient
lives here for tests: no live Azure OpenAI call anywhere in the default suite.
complete() is SYNCHRONOUS, matching SemanticClient.classify /
EmbeddingClient.embed -- the established convention for external-API client
protocols in this codebase (design spec decision 4)."""

from typing import Protocol, runtime_checkable

from fdi.core.agent import AgentResponse


@runtime_checkable
class AgentClient(Protocol):
    def complete(
        self, messages: list[dict[str, object]], tools: list[dict[str, object]]
    ) -> AgentResponse: ...


class FakeAgentClient:
    """Replays a pre-scripted list of AgentResponses, one per call, in
    order. Raises AssertionError (not IndexError) if answer_question calls
    it more times than scripted -- a loud, test-diagnosable failure rather
    than a confusing exception deep in dict/list machinery."""

    def __init__(self, responses: list[AgentResponse]) -> None:
        self._responses = responses
        self._next = 0
        self.calls: list[tuple[list[dict[str, object]], list[dict[str, object]]]] = []

    def complete(
        self, messages: list[dict[str, object]], tools: list[dict[str, object]]
    ) -> AgentResponse:
        self.calls.append((messages, tools))
        if self._next >= len(self._responses):
            raise AssertionError(
                f"FakeAgentClient exhausted: {len(self._responses)} scripted "
                f"response(s), call {len(self.calls)} requested"
            )
        resp = self._responses[self._next]
        self._next += 1
        return resp
