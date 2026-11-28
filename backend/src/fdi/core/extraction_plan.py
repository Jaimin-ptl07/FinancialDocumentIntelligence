"""Which Field x Report pairs an Extraction run must send to the model (S3).

The one place staleness is decided (ADR-0004). Given the Fields asked for,
the Reports, the reads already stored and the engine's current versions,
`plan_extraction` says for every pair whether to read it again, reuse a read,
or compute it -- and WHY, so a run can explain what it paid for.

A read is identified by its cache key: Field identity x definition hash x
retrieval configuration version x prompt version x model deployment (the
Report is the other half of the key). A live read whose key matches and which
did not fail is fresh. Anything else is planned with the first reason that
applies, in this order: forced, missing, previously failed (same key, failed),
definition changed, prompt changed, retrieval changed, model changed.

Pure (`fdi.core` law): no I/O, deterministic, total over its inputs.
"""

from __future__ import annotations

import hashlib
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal

Action = Literal["extract", "reuse", "derive"]
Reason = Literal[
    "fresh", "derived", "forced", "missing", "previously_failed",
    "definition_changed", "prompt_changed", "retrieval_changed", "model_changed",
]

FAILED: Final = "failed"


@dataclass(frozen=True)
class EngineVersions:
    """The parts of the cache key that belong to the engine, not the Field."""

    prompt_version: str
    retrieval_version: str
    model: str


@dataclass(frozen=True)
class PlannedField:
    field_id: str
    definition_hash: str
    origin: str = "extracted"


@dataclass(frozen=True)
class StoredRead:
    """One live (not superseded) read already stored for a Field x Report."""

    read_id: str
    doc_id: str
    field_id: str
    definition_hash: str
    prompt_version: str
    retrieval_version: str
    model: str
    status: str


@dataclass(frozen=True)
class PlanItem:
    doc_id: str
    field_id: str
    action: Action
    reason: Reason
    read_id: str | None = None


@dataclass(frozen=True)
class Plan:
    items: tuple[PlanItem, ...]

    def to_extract(self) -> list[PlanItem]:
        return [i for i in self.items if i.action == "extract"]

    def reused(self) -> list[PlanItem]:
        return [i for i in self.items if i.action == "reuse"]


def cache_key(field_id: str, definition_hash: str, engine: EngineVersions) -> str:
    """The read's identity within one Report (ADR-0004)."""
    body = "\x1f".join((field_id, definition_hash, engine.retrieval_version,
                        engine.prompt_version, engine.model))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _matches(read: StoredRead, f: PlannedField, engine: EngineVersions) -> bool:
    return (read.definition_hash == f.definition_hash
            and read.prompt_version == engine.prompt_version
            and read.retrieval_version == engine.retrieval_version
            and read.model == engine.model)


def _why_stale(reads: Sequence[StoredRead], f: PlannedField,
               engine: EngineVersions) -> Reason:
    if not reads:
        return "missing"
    if any(_matches(r, f, engine) for r in reads):
        return "previously_failed"  # the matching read exists but failed
    latest = reads[0]
    if latest.definition_hash != f.definition_hash:
        return "definition_changed"
    if latest.prompt_version != engine.prompt_version:
        return "prompt_changed"
    if latest.retrieval_version != engine.retrieval_version:
        return "retrieval_changed"
    return "model_changed"


def plan_extraction(
    *,
    fields: Sequence[PlannedField],
    doc_ids: Sequence[str],
    reads: Sequence[StoredRead],
    engine: EngineVersions,
    forced: Collection[str] = (),
) -> Plan:
    """One PlanItem per (Report, Field), Reports in `doc_ids` order and Fields
    in `fields` order. `reads` are the stored live reads for these pairs,
    newest first per pair (only the newest decides a "changed" reason).
    `forced` names Field ids to read again even when fresh. A derived Field
    is never read: it is computed from its inputs when shown."""
    by_pair: dict[tuple[str, str], list[StoredRead]] = {}
    for r in reads:
        by_pair.setdefault((r.doc_id, r.field_id), []).append(r)
    forced_ids = frozenset(forced)
    items: list[PlanItem] = []
    seen: set[tuple[str, str]] = set()
    for doc_id in doc_ids:
        for f in fields:
            pair = (doc_id, f.field_id)
            if pair in seen:
                continue
            seen.add(pair)
            if f.origin == "derived":
                items.append(PlanItem(doc_id, f.field_id, "derive", "derived"))
                continue
            pair_reads = by_pair.get(pair, [])
            if f.field_id in forced_ids:
                items.append(PlanItem(doc_id, f.field_id, "extract", "forced"))
                continue
            fresh = next(
                (r for r in pair_reads if _matches(r, f, engine) and r.status != FAILED),
                None,
            )
            if fresh is not None:
                items.append(PlanItem(doc_id, f.field_id, "reuse", "fresh", fresh.read_id))
                continue
            items.append(PlanItem(doc_id, f.field_id, "extract",
                                  _why_stale(pair_reads, f, engine)))
    return Plan(items=tuple(items))


def summarize(plan: Plan) -> Mapping[str, int]:
    """Counts by reason -- what a run reports it paid for and why."""
    out: dict[str, int] = {}
    for item in plan.items:
        out[item.reason] = out.get(item.reason, 0) + 1
    return out
