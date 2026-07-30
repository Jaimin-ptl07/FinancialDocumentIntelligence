"""Mapping memory: remember how a report label was resolved, so the next report
does not pay for it again.

WHY THE KEY IS NOT JUST THE LABEL
---------------------------------
"Computer equipment" under PPE is a fixed asset; "Computer software" under
intangibles is not. "Investments" means something different in the current and
non-current sections. A memory keyed on the label alone would confidently carry
the wrong answer across contexts, which is worse than having no memory at all.
The key is therefore (label, section, basis) -- and `section` is exactly what
the note resolution in `fdi.core.notes` already establishes.

WHAT IT BUYS
------------
* Cost -- a remembered label skips the semantic pass entirely.
* DETERMINISM -- the same document processed twice yields the same tags. That is
  a product property for a bank, not an optimisation: an auditor re-running a
  filing must not get a different answer.
* Compounding accuracy -- every human correction is written back, so report N+1
  benefits from the corrections made on reports 1..N.

Lookup order is memory -> lexical -> semantic, cheapest and most certain first.

Pure (`fdi.core` law): this module holds the DECISIONS -- what to trust, when to
auto-accept, what to promote. Storage is the platform layer's job; `MemoryStore`
is the seam, and `InMemoryStore` implements it for tests.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Protocol, runtime_checkable

from fdi.core.concepts import normalize

# A mapping seen and confirmed this many times is treated as settled and applied
# without review. Below it, the mapping is used but still surfaced for checking.
AUTO_ACCEPT_CONFIRMATIONS = 3

# Confidence floor for a remembered mapping to be used at all. A mapping that
# was never confident enough to trust once should not become trusted by repetition.
MIN_TRUSTED_CONFIDENCE = 0.5


@dataclass(frozen=True)
class MemoryKey:
    """What makes a mapping decision reusable -- and safe to reuse."""

    label: str
    section: str = ""
    basis: str = ""

    @staticmethod
    def of(label: object, section: object = "", basis: object = "") -> MemoryKey:
        return MemoryKey(normalize(label), normalize(section), normalize(basis))

    def as_str(self) -> str:
        return f"{self.basis}\x00{self.section}\x00{self.label}"


@dataclass(frozen=True)
class MemoryEntry:
    """A remembered resolution and how much it has earned trust."""

    key: MemoryKey
    tag: str
    confidence: float = 0.0
    method: str = "semantic"
    times_seen: int = 1
    times_confirmed: int = 0
    last_doc: str = ""

    @property
    def trusted(self) -> bool:
        """Usable without a fresh semantic pass."""
        return (
            bool(self.tag)
            and self.confidence >= MIN_TRUSTED_CONFIDENCE
            and self.times_confirmed >= 1
        )

    @property
    def settled(self) -> bool:
        """Confirmed often enough to apply without surfacing for review."""
        return self.times_confirmed >= AUTO_ACCEPT_CONFIRMATIONS


@runtime_checkable
class MemoryStore(Protocol):
    """Persistence seam. The platform layer supplies the Postgres-backed one."""

    def get(self, key: MemoryKey) -> MemoryEntry | None: ...
    def put(self, entry: MemoryEntry) -> None: ...
    def all(self) -> Sequence[MemoryEntry]: ...


class InMemoryStore:
    """Dict-backed store -- used by tests and by a single-process run."""

    def __init__(self, entries: Iterable[MemoryEntry] = ()) -> None:
        self._by_key: dict[str, MemoryEntry] = {e.key.as_str(): e for e in entries}

    def get(self, key: MemoryKey) -> MemoryEntry | None:
        return self._by_key.get(key.as_str())

    def put(self, entry: MemoryEntry) -> None:
        self._by_key[entry.key.as_str()] = entry

    def all(self) -> Sequence[MemoryEntry]:
        return list(self._by_key.values())


def recall(store: MemoryStore, key: MemoryKey) -> MemoryEntry | None:
    """The trusted mapping for `key`, or None. Never raises: a broken store
    degrades to "no memory", which costs a semantic call but is never wrong."""
    try:
        entry = store.get(key)
    except Exception:  # noqa: BLE001 - a memory miss must never fail extraction
        return None
    if entry is None or not entry.trusted:
        return None
    return entry


def observe(
    store: MemoryStore,
    key: MemoryKey,
    tag: str,
    *,
    confidence: float,
    method: str,
    doc_id: str = "",
    confirmed: bool = False,
) -> MemoryEntry:
    """Record that `key` resolved to `tag`.

    A DIFFERENT tag for a known key resets the confirmation count rather than
    merging: the two observations disagree, and averaging a disagreement would
    manufacture confidence that neither observation supports."""
    existing = None
    try:
        existing = store.get(key)
    except Exception:  # noqa: BLE001
        existing = None

    if existing is None or existing.tag != tag:
        entry = MemoryEntry(
            key=key, tag=tag, confidence=confidence, method=method,
            times_seen=1, times_confirmed=1 if confirmed else 0, last_doc=doc_id,
        )
    else:
        entry = replace(
            existing,
            confidence=max(existing.confidence, confidence),
            method=method if confirmed else existing.method,
            times_seen=existing.times_seen + 1,
            times_confirmed=existing.times_confirmed + (1 if confirmed else 0),
            last_doc=doc_id or existing.last_doc,
        )
    with contextlib.suppress(Exception):  # a write failure must not fail extraction
        store.put(entry)
    return entry


def confirm(
    store: MemoryStore, key: MemoryKey, tag: str, *, doc_id: str = ""
) -> MemoryEntry:
    """Record a human (or reconciliation) confirmation -- the signal that makes
    a mapping compound."""
    return observe(store, key, tag, confidence=1.0, method="confirmed",
                   doc_id=doc_id, confirmed=True)


def promotable_aliases(
    store: MemoryStore, *, min_confirmations: int = AUTO_ACCEPT_CONFIRMATIONS
) -> dict[str, list[str]]:
    """Settled mappings worth promoting into the schema's own alias list, as
    {tag: [report labels]}. This is how the taxonomy LEARNS: a wording confirmed
    across several filings stops needing memory at all."""
    out: dict[str, list[str]] = {}
    try:
        entries = list(store.all())
    except Exception:  # noqa: BLE001
        return out
    for entry in sorted(entries, key=lambda e: (e.tag, e.key.label)):
        if not entry.tag or entry.times_confirmed < min_confirmations:
            continue
        labels = out.setdefault(entry.tag, [])
        if entry.key.label and entry.key.label not in labels:
            labels.append(entry.key.label)
    return out


def stats(store: MemoryStore) -> dict[str, object]:
    """Coverage of the memory, for reporting -- never a silent black box."""
    try:
        entries = list(store.all())
    except Exception:  # noqa: BLE001
        entries = []
    return {
        "entries": len(entries),
        "trusted": sum(1 for e in entries if e.trusted),
        "settled": sum(1 for e in entries if e.settled),
        "tags": len({e.tag for e in entries if e.tag}),
    }


def key_for(record: Mapping[str, object]) -> MemoryKey:
    """Build a key from a mapped record (see fdi.core.mapping.to_records)."""
    return MemoryKey.of(
        record.get("source_label"), record.get("section"), record.get("basis")
    )
