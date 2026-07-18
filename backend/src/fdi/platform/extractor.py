"""AzureOpenAIExtractorClient -- ExtractorClient backed by Azure OpenAI
(GPT), JSON mode. Lives in platform (does I/O), not pipeline: the
ExtractorClient protocol and FakeExtractorClient live in
fdi.pipeline.extractor. This module structurally satisfies that protocol
without importing it -- platform may not import pipeline per the layered
architecture contract. AzureOpenAI is imported at module level so tests
can monkeypatch fdi.platform.extractor.AzureOpenAI -- no live Azure OpenAI
call in the default suite. Reuses the SAME FDI_GPT_* settings the semantic
stage (fdi.platform.semantic) and the agent (fdi.platform.agent) already
use -- no new config."""

import json

from openai import AzureOpenAI

from fdi.platform.config import Settings


class AzureOpenAIExtractorClient:
    """ExtractorClient backed by Azure OpenAI, JSON mode. Lives in platform
    (does I/O)."""

    def __init__(
        self, *, endpoint: str, key: str, deployment: str, model: str, api_version: str
    ) -> None:
        self._sdk = AzureOpenAI(azure_endpoint=endpoint, api_key=key, api_version=api_version)
        self._deployment = deployment
        self._model = model

    def extract(self, *, system: str, user: str) -> dict[str, object]:
        resp = self._sdk.chat.completions.create(
            model=self._deployment,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            response_format={"type": "json_object"},
            temperature=0,
        )
        content = resp.choices[0].message.content
        if not content:
            return {}
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def from_settings(settings: "Settings") -> "AzureOpenAIExtractorClient | None":
        if not settings.gpt_endpoint or not settings.gpt_key:
            return None
        return AzureOpenAIExtractorClient(
            endpoint=settings.gpt_endpoint, key=settings.gpt_key,
            deployment=settings.gpt_deployment, model=settings.gpt_model,
            api_version=settings.gpt_api_version,
        )
