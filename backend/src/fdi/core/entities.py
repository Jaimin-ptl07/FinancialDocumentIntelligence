"""Cross-document entity retrieval assembly (pure; no I/O, config, clock, RNG).

build_entity_timeline reshapes fdi.core.crossdoc.consolidate_entity's existing
output into a doc_key-scoped, version-ordered timeline -- REUSING consolidate_
entity's version-ordering and concept-presence math rather than reimplementing
them (design spec docs/superpowers/specs/2026-07-18-rt7-crossdoc-entity-
retrieval-design.md, decision 2). build_concept_comparison (added alongside
this function) assembles a cross-version concept comparison from a list of
per-document fdi.pipeline.tables.concept_lookup results, preserving concept_
lookup's None ("no structure artifact yet") vs [] ("structure present,
concept not found") distinction per version rather than collapsing both to an
empty row list (decision 3). Reads inputs as plain Mapping/list -- no import
of fdi.core.ontology or any fdi.pipeline module.
"""

from collections.abc import Mapping

from fdi.core.crossdoc import consolidate_entity


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _version_sort_key(entry: Mapping[str, object]) -> tuple[int, int, str]:
    """Version ascending, None-version last, doc_id tie-break -- the SAME
    ordering consolidate_entity uses internally. Duplicated (not imported):
    consolidate_entity's sort_key is a local closure; reaching into fdi.core.
    crossdoc's private internals from a sibling module is worse than these
    five lines."""
    doc_id = entry.get("doc_id")
    doc_id_s = doc_id if isinstance(doc_id, str) else ""
    v = _int_or_none(entry.get("version"))
    return (0, v, doc_id_s) if v is not None else (1, 0, doc_id_s)


def build_entity_timeline(
    doc_key: str, members: list[Mapping[str, object]]
) -> dict[str, object]:
    """`members`: [{doc_id, doc_key, version, knowledge}], the same shape
    fdi.pipeline.crossdoc.resolve_entities' `stash` values use. A missing/
    malformed `knowledge` degrades per consolidate_entity's own rules -- this
    function adds no new validation on top of it."""
    entity = consolidate_entity(list(members))

    member_views_raw = entity["members"]
    quality_trend_raw = entity["quality_trend"]
    concept_presence_raw = entity["concept_presence"]
    member_views = member_views_raw if isinstance(member_views_raw, list) else []
    quality_trend = quality_trend_raw if isinstance(quality_trend_raw, list) else []
    concept_presence = concept_presence_raw if isinstance(concept_presence_raw, dict) else {}

    timeline: list[dict[str, object]] = []
    for mv_raw, q_raw in zip(member_views, quality_trend, strict=True):
        mv = mv_raw if isinstance(mv_raw, Mapping) else {}
        q = q_raw if isinstance(q_raw, Mapping) else {}
        version = mv.get("version")
        presence_key: object = version if version is not None else mv.get("doc_id")
        concepts = sorted(
            c for c, versions in concept_presence.items()
            if isinstance(versions, list) and presence_key in versions
        )
        timeline.append({
            "doc_id": mv.get("doc_id"),
            "doc_key": mv.get("doc_key"),
            "version": version,
            "section_count": mv.get("section_count"),
            "concept_count": mv.get("concept_count"),
            "quality_ok": mv.get("quality_ok"),
            "concepts": concepts,
            "quality": {
                "ok": q.get("ok"),
                "error_count": q.get("error_count"),
                "warning_count": q.get("warning_count"),
            },
        })

    return {
        "schema_version": 1,
        "doc_key": doc_key,
        "member_count": entity["member_count"],
        "timeline": timeline,
        "concepts": entity["concepts"],
        "concept_presence": concept_presence,
    }


def build_concept_comparison(
    doc_key: str, concept: str, member_lookups: list[Mapping[str, object]]
) -> dict[str, object]:
    """`member_lookups`: [{doc_id, doc_key, version, rows: list[Mapping] | None}]
    -- `rows` is exactly fdi.pipeline.tables.concept_lookup's return value for
    that member, UNCHANGED. `status` makes concept_lookup's None ("no
    structure artifact yet") vs [] ("structure present, concept not found")
    distinction explicit and durable through this assembly step rather than
    collapsing both to an indistinguishable empty `rows` list. Defensively
    re-sorts by version (mirrors consolidate_entity's own posture) regardless
    of input order."""
    versions: list[dict[str, object]] = []
    for ml in sorted(member_lookups, key=_version_sort_key):
        rows_raw = ml.get("rows")
        status: str
        rows: list[dict[str, object]]
        if isinstance(rows_raw, list) and rows_raw:
            status = "matched"
            rows = [dict(r) for r in rows_raw if isinstance(r, Mapping)]
        elif isinstance(rows_raw, list):
            status, rows = "no_match", []
        else:
            status, rows = "no_structure", []  # None, or malformed -> degrade safely

        versions.append({
            "doc_id": ml.get("doc_id"),
            "doc_key": ml.get("doc_key"),
            "version": ml.get("version"),
            "status": status,
            "rows": rows,
        })

    return {
        "schema_version": 1,
        "doc_key": doc_key,
        "concept": concept,
        "versions": versions,
    }
