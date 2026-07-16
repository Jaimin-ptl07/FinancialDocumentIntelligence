"""Deterministic JSON. PURE — no I/O, no config, no clock, no RNG."""

import json
from typing import Any


def canonical_json(obj: Any) -> str:
    """Serialize deterministically: identical input -> byte-identical output.

    - sort_keys: dict insertion order must not leak into bytes
    - separators: no incidental whitespace
    - ensure_ascii=False: emit real UTF-8 (annual reports carry ₹, é, etc.)
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_json_bytes(obj: object) -> bytes:
    """UTF-8 bytes of canonical_json — the exact bytes stored and hashed."""
    return canonical_json(obj).encode("utf-8")
