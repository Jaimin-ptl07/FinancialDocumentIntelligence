"""Opt-in smoke test against real Azure Document Intelligence.

Skipped unless BOTH:
  (a) FDI_AZURE_DI_ENDPOINT and FDI_AZURE_DI_KEY are set, AND
  (b) a sample PDF exists at backend/tests/azure/sample.pdf

Never runs in CI or in an unconfigured environment -- see README.md in this
directory. The skipif markers below are evaluated at collection time and do
not construct Settings or touch the network, so collection is always safe.
"""

import os
from pathlib import Path

import pytest

_SAMPLE = Path(__file__).parent / "sample.pdf"
_HAVE_CREDS = bool(os.getenv("FDI_AZURE_DI_ENDPOINT") and os.getenv("FDI_AZURE_DI_KEY"))

pytestmark = [
    pytest.mark.azure,
    pytest.mark.skipif(
        not _HAVE_CREDS, reason="Azure DI not configured (FDI_AZURE_DI_ENDPOINT/KEY unset)"
    ),
    pytest.mark.skipif(not _SAMPLE.exists(), reason="no backend/tests/azure/sample.pdf present"),
]


def test_real_azure_ocr_extracts_content() -> None:
    from fdi.core.ocr_schema import normalize_ocr
    from fdi.platform.config import get_settings
    from fdi.platform.ocr import AzureOcrClient

    client = AzureOcrClient.from_settings(get_settings())
    assert client is not None
    raw = client.analyze(_SAMPLE.read_bytes())
    out = normalize_ocr(raw)
    assert isinstance(out["content"], str) and out["content"].strip()
    assert len(out["pages"]) >= 1
