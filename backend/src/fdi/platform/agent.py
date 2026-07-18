"""AzureOpenAIAgentClient -- AgentClient backed by Azure OpenAI (GPT).
Lives in platform (does I/O), not pipeline: the AgentClient protocol and
FakeAgentClient live in fdi.pipeline.agent. This module structurally
satisfies that protocol without importing it -- platform may not import
pipeline per the layered architecture contract. AzureOpenAI is imported at
module level so tests can monkeypatch fdi.platform.agent.AzureOpenAI -- no
live Azure OpenAI call in the default suite. Reuses the SAME FDI_GPT_*
settings the semantic stage (fdi.platform.semantic) already uses -- no new
config."""

import json

from openai import AzureOpenAI

from fdi.core.agent import AgentResponse, ToolCall
from fdi.platform.config import Settings


class AzureOpenAIAgentClient:
    """AgentClient backed by Azure OpenAI. Lives in platform (does I/O)."""

    def __init__(
        self, *, endpoint: str, key: str, deployment: str, model: str, api_version: str
    ) -> None:
        self._sdk = AzureOpenAI(azure_endpoint=endpoint, api_key=key, api_version=api_version)
        self._deployment = deployment
        self._model = model

    def complete(
        self, messages: list[dict[str, object]], tools: list[dict[str, object]]
    ) -> AgentResponse:
        # The generated overloads declare `messages`/`tools` against the SDK's
        # own closed TypedDict param shapes, not the generic
        # list[dict[str, object]] this seam's Protocol (fdi.pipeline.agent.
        # AgentClient) uses so pipeline code never imports an openai type --
        # the same "SDK stub is narrower than what the wire protocol actually
        # accepts" mismatch fdi.platform.ocr.py's begin_analyze_document call
        # already carries a type: ignore[call-overload] for.
        resp = self._sdk.chat.completions.create(  # type: ignore[call-overload]
            model=self._deployment, messages=messages, tools=tools,
            tool_choice="auto", temperature=0,
        )
        msg = resp.choices[0].message
        if msg.tool_calls:
            calls = []
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    # Malformed model output. Do NOT crash the loop --
                    # produce a ToolCall whose args execute_tool's own
                    # pydantic validation will reject the normal way (the
                    # expected fields are simply absent from this dict).
                    args = {"_malformed_json": tc.function.arguments}
                calls.append(ToolCall(id=tc.id, name=tc.function.name, arguments=args))
            return AgentResponse(tool_calls=calls)
        return AgentResponse(content=msg.content or "")

    @staticmethod
    def from_settings(settings: "Settings") -> "AzureOpenAIAgentClient | None":
        if not settings.gpt_endpoint or not settings.gpt_key:
            return None
        return AzureOpenAIAgentClient(
            endpoint=settings.gpt_endpoint, key=settings.gpt_key,
            deployment=settings.gpt_deployment, model=settings.gpt_model,
            api_version=settings.gpt_api_version,
        )
