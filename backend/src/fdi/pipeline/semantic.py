"""The SemanticClient seam: a `classify(heading, text) -> ClassifyResult`
protocol.

`AzureOpenAIClient` (platform, does I/O) structurally satisfies this without
importing it -- `platform` may not import `pipeline`. `FakeSemanticClient`
lives here for tests: no live Azure OpenAI call anywhere in the suite.
"""

from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from fdi.core.semantic import ClassifyResult, SectionType


@runtime_checkable
class SemanticClient(Protocol):
    def classify(self, heading: str, text: str) -> ClassifyResult: ...


class FakeSemanticClient:
    def __init__(self, labels: Mapping[str, str]) -> None:
        self._labels = labels
        self.calls = 0
        self.seen: list[tuple[str, str]] = []

    def classify(self, heading: str, text: str) -> ClassifyResult:
        self.calls += 1
        self.seen.append((heading, text))
        return ClassifyResult(label=self._labels.get(heading, SectionType.OTHER.value))
