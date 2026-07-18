"""One real Azure OpenAI extraction round trip. Mirrors
test_real_semantic.py / test_real_agent.py exactly: reuses @pytest.mark.azure
(no new marker), skipped without FDI_GPT_ENDPOINT/FDI_GPT_KEY."""

import os

import pytest

from fdi.core.extractor import Candidate, FieldSpec, build_extraction_messages
from fdi.platform.config import get_settings
from fdi.platform.extractor import AzureOpenAIExtractorClient

pytestmark = [
    pytest.mark.azure,
    pytest.mark.skipif(
        not (os.getenv("FDI_GPT_ENDPOINT") and os.getenv("FDI_GPT_KEY")),
        reason="Azure OpenAI not configured",
    ),
]


def test_real_extractor_client_returns_json_shaped_result() -> None:
    client = AzureOpenAIExtractorClient.from_settings(get_settings())
    assert client is not None
    field = FieldSpec(
        field_name="Total revenue", data_type="decimal",
        retrieval_query="Total revenue from operations", keywords=("revenue",),
        extraction_prompt="Extract the total revenue value.", enum_values=None,
        module_id=1, module_name="INCOME_STATEMENT",
    )
    candidate = Candidate(
        evidence_id="s0", text="Total revenue from operations was 1,234.", page=5,
        score=1.0, source_tool="hybrid",
    )
    system, user = build_extraction_messages(field, [candidate])
    result = client.extract(system=system, user=user)
    assert isinstance(result, dict)
    # JSON-mode + this plan's assemble_result already tolerate a garbled or
    # empty dict -- this only needs to confirm the SDK round trip didn't raise.
