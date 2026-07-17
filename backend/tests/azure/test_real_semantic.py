"""Opt-in smoke test against real Azure OpenAI.

Skipped unless FDI_GPT_ENDPOINT and FDI_GPT_KEY are set. Never runs in CI or in
an unconfigured environment. The skipif marker is evaluated at collection
time and does not construct Settings or touch the network, so collection is
always safe.
"""

import os

import pytest

from fdi.core.semantic import SectionType
from fdi.platform.config import get_settings
from fdi.platform.semantic import AzureOpenAIClient

pytestmark = [
    pytest.mark.azure,
    pytest.mark.skipif(
        not (os.getenv("FDI_GPT_ENDPOINT") and os.getenv("FDI_GPT_KEY")),
        reason="Azure OpenAI not configured",
    ),
]


def test_real_classification_coerces_to_section_type() -> None:
    client = AzureOpenAIClient.from_settings(get_settings())
    assert client is not None
    label = client.classify(
        "Balance Sheet", "Total Assets 1,00,000\nTotal Liabilities 1,00,000"
    )
    assert SectionType(label) in set(SectionType)
