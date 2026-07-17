"""AzureOcrClient -- OcrClient backed by Azure Document Intelligence.

Lives in `platform` (does I/O), not `pipeline`: the `OcrClient` protocol,
`OcrStage`, and `FakeOcrClient` live in `fdi.pipeline.ocr`. This module
structurally satisfies that protocol without importing it -- `platform` may
not import `pipeline` per the layered architecture contract.

The SDK client and credential are imported at module level (rather than
inside `analyze`) so tests can monkeypatch `fdi.platform.ocr.
DocumentIntelligenceClient` / `AzureKeyCredential` with fakes -- no live
Azure call in the suite.
"""

from collections.abc import Mapping

from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.core.credentials import AzureKeyCredential

from fdi.platform.config import Settings


class AzureOcrClient:
    """OcrClient backed by Azure Document Intelligence. Lives in platform (does I/O)."""

    def __init__(
        self,
        *,
        endpoint: str,
        key: str,
        model: str,
        api_version: str,
        polling_timeout_s: int,
    ) -> None:
        self._endpoint = endpoint
        self._key = key
        self._model = model
        self._api_version = api_version
        self._polling_timeout_s = polling_timeout_s

    def analyze(self, pdf: bytes) -> Mapping[str, object]:
        client = DocumentIntelligenceClient(
            self._endpoint, AzureKeyCredential(self._key), api_version=self._api_version
        )
        # The generated overloads only declare AnalyzeDocumentRequest / JSON /
        # IO[bytes] for `body`, but the implementation explicitly special-cases
        # raw `bytes` (see _operations.py: `isinstance(body, (IOBase, bytes))`)
        # and streams it as the request content -- passing bytes directly is
        # correct at runtime, just outside what the stub overloads express.
        poller = client.begin_analyze_document(self._model, body=pdf)  # type: ignore[call-overload]
        result = poller.result()
        return dict(result.as_dict())

    @staticmethod
    def from_settings(settings: "Settings") -> "AzureOcrClient | None":
        if not settings.azure_di_endpoint or not settings.azure_di_key:
            return None
        return AzureOcrClient(
            endpoint=settings.azure_di_endpoint,
            key=settings.azure_di_key,
            model=settings.azure_di_model,
            api_version=settings.azure_di_api_version,
            polling_timeout_s=settings.azure_di_polling_timeout_s,
        )
