"""FogMappingStage: the ONLY billed stage in the pipeline.

Isolating the model here is most of the operational value of moving to stages.
A validation-rule change, a new ratio, a confidence re-tune or a taxonomy edit
re-runs `fog_reason` at zero marginal cost; under the old synchronous route
every one of those re-paid for the whole document.

Three tiers, cheapest first: memory (free, deterministic) -> lexical (free) ->
one LLM call per unit for whatever is left. A unit whose free tiers settle
everything is never sent, so a second run with warm memory can cost nothing.

Never raises (decision 10): each unit's model call is guarded individually, so
one failure degrades THAT unit's labels to `unmapped` -- visible, with a reason
-- and never the document.

Costing (this stage is the only one that spends money): a real
ExtractorClient.extract() call's `.usage` is reported to an injected
`UsageAccumulator` (fdi.pipeline.usage), the same pattern SemanticStage uses,
so `GET /documents/{doc_id}/cost` accounts for it. The accumulator is threaded
through as an optional keyword, defaulting to None, so every existing caller
(including this module's own tests) is unaffected. Its lookup from
`ctx.services` is a plain `Mapping.get`, which cannot raise on a missing key,
and recording a call is guarded SEPARATELY from the model call that produced
it -- a measured scar (fdi.pipeline.extractor's `_record_llm_usage` guard)
showed a failed usage write poisoning a shared DB session and silently
killing retrieval for every later unit in a document. Costing is bookkeeping
and must never be able to take extraction down with it.
"""

from collections.abc import Mapping
from typing import cast

import structlog

from fdi.core.concepts import (
    ConceptCandidate,
    all_concepts,
    build_concept_index,
)
from fdi.core.fog_mapping import (
    MapUnit,
    Resolution,
    build_map_prompt,
    mapping_edges,
    read_map_reply,
    resolve_from_memory,
    resolve_lexical,
    units_from_fog,
    unmapped_labels,
)
from fdi.core.memory import MemoryStore
from fdi.pipeline.registry import REGISTRY
from fdi.pipeline.stage import StageContext
from fdi.pipeline.usage import UsageAccumulator

log = structlog.get_logger(__name__)


def _note_candidates(schema: Mapping[str, object]) -> tuple[ConceptCandidate, ...]:
    """Every tagged concept -- note rows are DETAIL and match descendants."""
    index = build_concept_index(schema)
    seen: dict[str, ConceptCandidate] = {}
    for group in index.groups:
        for candidate in group.candidates:
            seen.setdefault(candidate.tag, candidate)
    return tuple(seen.values()) or all_concepts(schema)


def _resolve_unit(
    unit: MapUnit,
    *,
    client: object,
    store: MemoryStore | None,
    usage: UsageAccumulator | None = None,
) -> tuple[dict[str, Resolution], int, str]:
    """One unit through all three tiers.

    Returns (resolutions, llm_calls, reason) where `reason` explains what
    happened to whatever stayed UNMAPPED. "model answered unmapped" and
    "the model call raised" previously produced byte-identical bodies AND
    diagnostics -- an honest refusal and a swallowed outage were literally
    indistinguishable, which is the failure this graph exists to prevent."""
    resolved: dict[str, Resolution] = {}
    if store is not None:
        resolved.update(resolve_from_memory(unit, store))
    for label, resolution in resolve_lexical(unit).items():
        resolved.setdefault(label, resolution)

    # unmapped_labels only checks label membership (fdi.core.fog_mapping) --
    # the Mapping[str, str] it's typed for is a stricter shape than the
    # Resolution values this stage actually keys by label.
    residue = unmapped_labels(unit, cast(Mapping[str, str], resolved))
    if not residue:
        return resolved, 0, ""
    if client is None:
        return resolved, 0, "no_client"

    ask = MapUnit(
        unit_id=unit.unit_id, kind=unit.kind, title=unit.title,
        labels=tuple(residue),
        # Tags already claimed cannot be claimed twice.
        candidates=tuple(
            c for c in unit.candidates
            if c.tag not in {r.tag for r in resolved.values()}
        ),
    )
    if not ask.candidates:
        return resolved, 0, "no_candidate_left"

    system, user = build_map_prompt(ask)
    raw: object = {}
    try:
        result = client.extract(system=system, user=user)  # type: ignore[attr-defined]
        raw = result.data
    except Exception:  # noqa: BLE001 - decision 10
        log.info("fog_mapping.unit_failed", unit=unit.unit_id)
        return resolved, 1, "model_call_failed"

    # Usage bookkeeping gets its own guard, separate from the model call
    # above: a failed accumulator write must never lose `raw`, the result
    # the call already paid for (see module docstring for the measured scar).
    if usage is not None and result.usage is not None:
        try:
            usage.record_llm("fog_mapping", result.usage)
        except Exception:  # noqa: BLE001 - decision 10
            log.info("fog_mapping.usage_record_failed", unit=unit.unit_id)

    for label, tag in read_map_reply(raw, ask).items():
        resolved[label] = Resolution(label, tag, "llm", 0.0)
    return resolved, 1, "model_said_unmapped"


def build_mapping(
    fog: Mapping[str, object],
    schema: Mapping[str, object],
    *,
    client: object,
    store: MemoryStore | None,
    usage: UsageAccumulator | None = None,
) -> dict[str, object]:
    """Map every basis of a FOG. Pure orchestration over the three tiers."""
    face_candidates = all_concepts(schema)
    note_candidates = _note_candidates(schema)

    bases_raw = fog.get("bases") if isinstance(fog, Mapping) else None
    bases = bases_raw if isinstance(bases_raw, Mapping) else {}

    out_bases: dict[str, object] = {}
    diagnostics: dict[str, object] = {}

    for basis in sorted(bases):
        region = bases[basis]
        if not isinstance(region, Mapping):
            continue
        units = units_from_fog(
            region, face_candidates=face_candidates, note_candidates=note_candidates
        )
        resolutions: dict[str, dict[str, Resolution]] = {}
        # label -> why. First reason wins: a label unresolved in one unit and
        # resolved in another is not unmapped at all, and is dropped below.
        unmapped: dict[str, str] = {}
        calls = 0
        for unit in units:
            resolved, made, reason = _resolve_unit(
                unit, client=client, store=store, usage=usage
            )
            resolutions[unit.unit_id] = resolved
            calls += made
            for label in unmapped_labels(unit, cast(Mapping[str, str], resolved)):
                unmapped.setdefault(label, reason or "no_candidate")

        edges, proof = mapping_edges(resolutions, region)
        tally = {"memory": 0, "lexical": 0, "llm": 0}
        for record in proof:
            # `resolve_lexical` qualifies its method as "lexical:name" or
            # "lexical:alias" (fdi.core.fog_mapping) -- the tier prefix, not
            # the exact string, is what diagnostics buckets on.
            method = str(record.get("method", "")).split(":", 1)[0]
            if method in tally:
                tally[method] += 1

        out_bases[basis] = {
            "edges": [
                {"type": e.type, "source": e.source, "target": e.target}
                for e in edges
            ],
            "proof": proof,
            "unmapped": [
                {"label": label, "reason": unmapped[label]}
                for label in sorted(unmapped)
            ],
        }
        diagnostics[basis] = {
            "units": len(units),
            "labels": sum(len(u.labels) for u in units),
            "by_memory": tally["memory"],
            "by_lexical": tally["lexical"],
            "by_llm": tally["llm"],
            "unmapped": len(unmapped),
            "llm_calls": calls,
        }

    # A schema that yields NO concepts makes every basis map nothing, and the
    # per-basis diagnostics of that are byte-identical to an honest "this
    # document discloses nothing" -- 0 edges, 0 facts, 0 cost, no failure.
    # MEASURED: the on-disk v3.2 seed `load_schema` serves by default carries
    # ZERO `tag` keys (v4.3 carries 262), so wiring it turned the whole of
    # Domain 1 into a silent no-op. Recording the concept count is what makes
    # "the schema is wrong" distinguishable from "the report is empty".
    if not face_candidates:
        log.warning(
            "fog_mapping.schema_has_no_concepts",
            doc_id=str(fog.get("doc_id", "")) if isinstance(fog, Mapping) else "",
            hint="load_schema is serving a schema with no `tag` keys "
                 "(the v3.2 seed); the FOG requires a v4.3-shaped document",
        )

    return {
        "schema_version": 3,
        "doc_id": str(fog.get("doc_id", "")) if isinstance(fog, Mapping) else "",
        "schema": {
            "face_concepts": len(face_candidates),
            "note_concepts": len(note_candidates),
            "usable": bool(face_candidates),
        },
        "bases": out_bases,
        "diagnostics": diagnostics,
    }


class FogMappingStage:
    name = "fog_mapping"
    version = 3
    deps: tuple[str, ...] = ("fog_objects",)

    async def run(self, ctx: StageContext) -> object:
        fog = cast(Mapping[str, object], await ctx.read("fog_objects"))
        schema = cast(Mapping[str, object], ctx.services.get("schema") or {})
        client = ctx.services.get("extractor")
        # Guarded lookup: a missing "usage" key degrades to None rather than
        # raising -- this stage must never fail the DAG over bookkeeping.
        usage = cast(UsageAccumulator | None, ctx.services.get("usage"))
        return build_mapping(fog, schema, client=client, store=None, usage=usage)


REGISTRY.register(FogMappingStage())
