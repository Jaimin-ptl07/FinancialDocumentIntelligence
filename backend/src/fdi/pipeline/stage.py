"""The Stage contract. A stage is a near-pure async transform; the framework owns
all I/O, hashing, and persistence around it."""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Protocol, runtime_checkable

ReadArtifact = Callable[[str, str], Awaitable[object]]
SourceReader = Callable[[], Awaitable[bytes]]


async def _no_source() -> bytes:
    raise ValueError("this stage context has no document source")


@dataclass(frozen=True)
class StageContext:
    """What a stage may touch: its document id, a reader for upstream bodies, a
    reader for the source PDF, and a bag of injected services."""

    doc_id: str
    read_artifact: ReadArtifact
    read_source: SourceReader = _no_source
    services: Mapping[str, object] = field(default_factory=lambda: MappingProxyType({}))

    async def read(self, stage_name: str) -> object:
        return await self.read_artifact(self.doc_id, stage_name)


@runtime_checkable
class Stage(Protocol):
    name: str
    version: int
    deps: tuple[str, ...]

    async def run(self, ctx: StageContext) -> object: ...

    def key_material(self, services: Mapping[str, object]) -> str:
        """Extra cache-key material from INJECTED SERVICES, or "".

        `run_stage` keys an artifact on its declared deps' output hashes plus
        `version`. A stage whose output also depends on something handed to it
        through `services` -- the taxonomy schema, a prompt template -- is
        outside that key, so changing it changes the answer while the stale
        artifact is served forever. Returning a digest of that input closes
        the hole without making services a dependency edge."""
        return ""
