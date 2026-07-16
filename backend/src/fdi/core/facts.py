"""Pure value objects shared across layers. No I/O — belongs in core."""

from dataclasses import dataclass


@dataclass(frozen=True)
class RunFacts:
    version: int
    input_hash: str
    output_hash: str
