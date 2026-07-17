"""AzureOpenAIClient -- SemanticClient backed by Azure OpenAI (GPT).

Lives in `platform` (does I/O), not `pipeline`: the `SemanticClient` protocol,
`SemanticStage`, and `FakeSemanticClient` live in `fdi.pipeline.semantic`. This
module structurally satisfies that protocol without importing it -- `platform`
may not import `pipeline` per the layered architecture contract.

`AzureOpenAI` is imported at module level (rather than inside `classify`) so
tests can monkeypatch `fdi.platform.semantic.AzureOpenAI` with a fake -- no
live Azure OpenAI call in the suite.
"""

from openai import AzureOpenAI

from fdi.platform.config import Settings

_SYSTEM_PROMPT = (
    "You classify a section of a financial statement by its heading and text. "
    "Respond with exactly one of these labels, uppercase, nothing else: "
    "BALANCE_SHEET, INCOME_STATEMENT, CASH_FLOW, NOTES, MDNA, AUDITORS_REPORT, "
    "DIRECTORS_REPORT, OTHER."
)


class AzureOpenAIClient:
    """SemanticClient backed by Azure OpenAI. Lives in platform (does I/O)."""

    def __init__(
        self, *, endpoint: str, key: str, deployment: str, model: str, api_version: str
    ) -> None:
        self._sdk = AzureOpenAI(azure_endpoint=endpoint, api_key=key, api_version=api_version)
        self._deployment = deployment
        self._model = model

    def classify(self, heading: str, text: str) -> str:
        resp = self._sdk.chat.completions.create(
            model=self._deployment,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": f"Heading: {heading}\n\n{text}"},
            ],
            temperature=0,
        )
        content = resp.choices[0].message.content
        return (content or "").strip().upper()

    @staticmethod
    def from_settings(settings: "Settings") -> "AzureOpenAIClient | None":
        if not settings.gpt_endpoint or not settings.gpt_key:
            return None
        return AzureOpenAIClient(
            endpoint=settings.gpt_endpoint,
            key=settings.gpt_key,
            deployment=settings.gpt_deployment,
            model=settings.gpt_model,
            api_version=settings.gpt_api_version,
        )
