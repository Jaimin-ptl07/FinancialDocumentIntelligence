"""One real Azure OpenAI tool-calling round trip. Mirrors
test_real_semantic.py exactly: reuses @pytest.mark.azure (no new marker,
design spec decision 14), skipped without FDI_GPT_ENDPOINT/FDI_GPT_KEY."""

import os

import pytest

from fdi.platform.agent import AzureOpenAIAgentClient
from fdi.platform.config import get_settings

pytestmark = [
    pytest.mark.azure,
    pytest.mark.skipif(
        not (os.getenv("FDI_GPT_ENDPOINT") and os.getenv("FDI_GPT_KEY")),
        reason="Azure OpenAI not configured",
    ),
]


def test_real_agent_client_produces_a_well_formed_response() -> None:
    from fdi.pipeline.agent_tools import TOOL_SCHEMAS

    client = AzureOpenAIAgentClient.from_settings(get_settings())
    assert client is not None
    resp = client.complete(
        [{"role": "user", "content": "What tools can you use? Answer in one sentence, "
                                      "do not call a tool for this question."}],
        TOOL_SCHEMAS,
    )
    # AgentResponse.__post_init__ already guarantees exactly one of
    # tool_calls/content is set -- this only needs to confirm the SDK round
    # trip didn't raise, and that a tool_calls branch (if hit) names a real
    # registered tool.
    if resp.tool_calls is not None:
        names = {s["function"]["name"] for s in TOOL_SCHEMAS}
        assert all(tc.name in names for tc in resp.tool_calls)
