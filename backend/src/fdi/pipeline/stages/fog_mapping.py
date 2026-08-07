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
    MAX_CANDIDATES,
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
    row_ids,
    units_from_fog,
    unmapped_labels,
)
from fdi.core.hashing import sha256_hex
from fdi.core.memory import MemoryStore
from fdi.core.serialization import canonical_json_bytes
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
) -> tuple[dict[str, Resolution], int, int, int, str]:
    """One unit through all three tiers.

    Returns (resolutions, llm_calls, llm_calls_billed, dropped, reason).
    `llm_calls` is every model call ATTEMPTED (success or failure);
    `llm_calls_billed` is the subset that returned `.usage` -- the two used
    to be conflated under one `llm_calls` count, so the artifact and
    `GET /documents/{doc_id}/cost` disagreed about how many calls were
    actually billed. `dropped` is how many candidates `build_map_prompt`
    capped out of the prompt (0 when no call was made). `reason` explains
    what happened to whatever stayed UNMAPPED. "model answered unmapped" and
    "the model call raised" previously produced byte-identical bodies AND
    diagnostics -- an honest refusal and a swallowed outage were literally
    indistinguishable, which is the failure this graph exists to prevent."""
    resolved: dict[str, Resolution] = {}
    if store is not None:
        resolved.update(resolve_from_memory(unit, store))
    for row_id, resolution in resolve_lexical(unit).items():
        resolved.setdefault(row_id, resolution)

    # `resolved` and `unmapped_labels`'s return are both keyed by
    # `row_ids(unit)` (fdi.core.fog_mapping) -- the item id, not the label,
    # so two rows sharing one caption ("Lease liabilities" under both
    # Non-current and Current liabilities) are tracked as two rows here.
    residue = unmapped_labels(unit, resolved)
    if not residue:
        return resolved, 0, 0, 0, ""
    if client is None:
        return resolved, 0, 0, 0, "no_client"

    # Carry the residual rows' ids, kinds AND sections into the follow-up
    # unit -- not just their labels. Losing any of the three here is exactly
    # how the second pass would lose the information this fix adds: without
    # ids the reply couldn't be keyed back to a row, without sections the
    # model would see "Lease liabilities" twice with nothing to tell the
    # rows apart.
    residue_keys = {row_id for row_id, _label in residue}
    row_index = {
        row_id: index for index, row_id in enumerate(row_ids(unit))
    }
    positions = sorted(
        row_index[row_id] for row_id in residue_keys if row_id in row_index
    )
    ask = MapUnit(
        unit_id=unit.unit_id, kind=unit.kind, title=unit.title,
        labels=tuple(unit.labels[i] for i in positions),
        # Tags already claimed cannot be claimed twice.
        candidates=tuple(
            c for c in unit.candidates
            if c.tag not in {r.tag for r in resolved.values()}
        ),
        kinds=tuple(
            unit.kinds[i] if i < len(unit.kinds) else "unknown"
            for i in positions
        ),
        basis=unit.basis,
        sections=tuple(
            unit.sections[i] if i < len(unit.sections) else () for i in positions
        ),
        item_ids=tuple(row_ids(unit)[i] for i in positions),
    )
    if not ask.candidates:
        return resolved, 0, 0, 0, "no_candidate_left"

    system, user, dropped = build_map_prompt(ask)
    if dropped:
        log.info(
            "fog_mapping.candidates_capped", unit=unit.unit_id,
            dropped=dropped, cap=MAX_CANDIDATES,
        )
    raw: object = {}
    try:
        result = client.extract(system=system, user=user)  # type: ignore[attr-defined]
        raw = result.data
        # Read INSIDE the same guard as the call itself (Decision 10): a
        # client returning a malformed result -- `.data` present, `.usage`
        # missing entirely -- must degrade this unit, not raise past it.
        result_usage = result.usage
    except Exception:  # noqa: BLE001 - decision 10
        log.info("fog_mapping.unit_failed", unit=unit.unit_id)
        return resolved, 1, 0, dropped, "model_call_failed"

    billed = 0
    if result_usage is not None:
        billed = 1
        # Usage bookkeeping gets its own guard, separate from the model call
        # above: a failed accumulator write must never lose `raw`, the
        # result the call already paid for (see module docstring for the
        # measured scar).
        if usage is not None:
            try:
                usage.record_llm("fog_mapping", result_usage)
            except Exception:  # noqa: BLE001 - decision 10
                log.info("fog_mapping.usage_record_failed", unit=unit.unit_id)

    ask_labels = dict(zip(row_ids(ask), ask.labels, strict=True))
    for row_id, tag in read_map_reply(raw, ask).items():
        resolved[row_id] = Resolution(ask_labels.get(row_id, row_id), tag, "llm", 0.0)
    return resolved, 1, billed, dropped, "model_said_unmapped"


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
            region, face_candidates=face_candidates, note_candidates=note_candidates,
            basis=basis,
        )
        resolutions: dict[str, dict[str, Resolution]] = {}
        # row id -> (label, why). Keyed by ROW, not by label: two rows can
        # share one caption ("Lease liabilities" under both Non-current and
        # Current liabilities), and a label key would let one row's reason
        # silently stand in for the other's -- exactly the collapse this
        # phase exists to fix.
        unmapped: dict[str, tuple[str, str]] = {}
        calls = 0
        billed_calls = 0
        dropped_candidates = 0
        for unit in units:
            resolved, made, billed, dropped, reason = _resolve_unit(
                unit, client=client, store=store, usage=usage
            )
            resolutions[unit.unit_id] = resolved
            calls += made
            billed_calls += billed
            dropped_candidates += dropped
            for row_id, label in unmapped_labels(unit, resolved):
                unmapped.setdefault(row_id, (label, reason or "no_candidate"))

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
            # Sorted by (label, row id): label-first keeps the common case --
            # no two unresolved rows sharing a caption -- reading exactly as
            # before; the row id is only a tiebreaker so two genuinely
            # distinct rows with the same label never collapse into one
            # artifact entry.
            # `line_item` is EMITTED, not merely used to sort. It was a
            # tiebreaker that got dropped, which made two genuinely distinct
            # unresolved rows byte-identical in the artifact -- the exact
            # collapse the tiebreaker existed to prevent, just moved one step
            # later. MEASURED on TCS consolidated: 93 of 379 unmapped records
            # share their (label, reason) with another, and a React list keyed
            # on the label crashed on the duplicate. It also matches `proof`,
            # so an unmapped row can be located on the page like any fact.
            "unmapped": [
                {"label": label, "reason": reason, "line_item": row_id}
                for label, row_id, reason in sorted(
                    (label, row_id, reason)
                    for row_id, (label, reason) in unmapped.items()
                )
            ],
        }
        diagnostics[basis] = {
            "units": len(units),
            "labels": sum(len(u.labels) for u in units),
            "by_memory": tally["memory"],
            "by_lexical": tally["lexical"],
            "by_llm": tally["llm"],
            "unmapped": len(unmapped),
            # `llm_calls` is every attempt; `llm_calls_billed` is the subset
            # that returned `.usage` -- a failed call is attempted but never
            # billed, and conflating the two made this artifact disagree with
            # GET /documents/{doc_id}/cost about how many calls were paid for.
            "llm_calls": calls,
            "llm_calls_billed": billed_calls,
            # How many candidates `build_map_prompt`'s MAX_CANDIDATES cap cut
            # out of a prompt in this basis -- 0 means nothing was capped.
            "prompt_candidates_dropped": dropped_candidates,
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
        # v4: `resolve_lexical` refuses a kind-mismatched candidate (a count,
        # per-share figure or ratio can never satisfy a currency concept) and
        # a match below `LEXICAL_FLOOR`, so `proof` and `unmapped` both change.
        # v5: diagnostics split `llm_calls` (attempted) from `llm_calls_billed`
        # (returned usage) and gained `prompt_candidates_dropped`; memory
        # lookups are now basis-scoped -- Task 6, the Phase 3-4 review minors.
        # v6: units now carry each label's `section_path` and `build_map_prompt`
        # renders it, so a document with two rows sharing one caption -- both
        # labelled identically -- now asks the model to distinguish them.
        # v7: every resolution table is keyed by ROW (the FOG line item id),
        # not by label -- two rows sharing one caption ("Lease liabilities"
        # under both Non-current and Current liabilities) used to collapse
        # into a single label-keyed entry, so the second overwrote the
        # first. The prompt now numbers each row and the reply is read back
        # by that number; `proof` and `unmapped` both change shape as a
        # result (still `{"label", "reason"}` per unmapped entry, but no
        # longer collapsed across rows sharing a label).
        "schema_version": 7,
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
    # v4: kind-mismatched and sub-floor lexical matches are now refused.
    # v5: billed-call count, prompt candidate cap, and basis-scoped memory.
    # v6: units carry each label's `section_path` and the prompt renders it.
    # v7: resolution tables are keyed by row (item id), not by label.
    # v8: the section subset must clear _SECTION_CONFIDENT to take first
    #     refusal, and a reply may only name a candidate the prompt SHOWED.
    # v9: a statement FACE drops the GROSS twin of any name a NET twin
    #     shares -- the face reports net carrying amounts.
    # v10: an unmapped record carries its `line_item`, so two rows sharing
    #      a caption stay distinct in the artifact.
    version = 10
    deps: tuple[str, ...] = ("fog_objects",)

    async def run(self, ctx: StageContext) -> object:
        fog = cast(Mapping[str, object], await ctx.read("fog_objects"))
        schema = cast(Mapping[str, object], ctx.services.get("schema") or {})
        client = ctx.services.get("extractor")
        # Guarded lookup: a missing "usage" key degrades to None rather than
        # raising -- this stage must never fail the DAG over bookkeeping.
        usage = cast(UsageAccumulator | None, ctx.services.get("usage"))
        return build_mapping(fog, schema, client=client, store=None, usage=usage)

    def key_material(self, services: Mapping[str, object]) -> str:
        """The taxonomy schema's digest. Canonical JSON so dict ordering cannot
        change the key and cause a spurious re-bill of the only paid stage."""
        schema = services.get("schema")
        if not isinstance(schema, Mapping):
            return ""
        try:
            return sha256_hex(canonical_json_bytes(schema))
        except Exception:  # noqa: BLE001 - a key is never worth failing a run
            return ""


REGISTRY.register(FogMappingStage())
