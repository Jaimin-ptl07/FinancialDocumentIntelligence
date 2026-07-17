from collections.abc import Mapping
from typing import Protocol, runtime_checkable


@runtime_checkable
class OcrClient(Protocol):
    def analyze(self, pdf: bytes) -> Mapping[str, object]: ...


class FakeOcrClient:
    def __init__(self, result: Mapping[str, object]) -> None:
        self._result = result
        self.calls = 0
        self.last_pdf: bytes | None = None

    def analyze(self, pdf: bytes) -> Mapping[str, object]:
        self.calls += 1
        self.last_pdf = pdf
        return self._result
