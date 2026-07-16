"""The Stage contract. A stage is a near-pure async transform; the framework owns
all I/O, hashing, and persistence around it."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

ReadArtifact = Callable[[str, str], Awaitable[object]]


@dataclass(frozen=True)
class StageContext:
    """What a stage may touch: its document id, and a reader for upstream bodies."""

    doc_id: str
    read_artifact: ReadArtifact

    async def read(self, stage_name: str) -> object:
        return await self.read_artifact(self.doc_id, stage_name)


@runtime_checkable
class Stage(Protocol):
    name: str
    version: int
    deps: tuple[str, ...]

    async def run(self, ctx: StageContext) -> object: ...
