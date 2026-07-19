"""Knowledge consolidation: fold a document's derived knowledge artifacts --
SP-10's section classification (`semantic`), SP-11's concept mappings
(`ontology`), SP-17b's quality report (`quality`) -- into ONE unified
per-document knowledge record. Pure, deterministic; no I/O; degrades on
missing/malformed input, never raises. SP-16's cross-document layer consumes
this record as its per-document input."""

from collections.abc import Mapping

# Phase 4: quality.py's three reconciliation-validator issue codes -- see
# fdi.core.quality.RECONCILIATION_VALIDATORS. Named here (not imported) to
# keep this module's only dependency a plain Mapping/list read, matching its
# existing "no I/O, no cross-core-module import" posture.
_RECONCILIATION_ISSUE_CODES = {
    "quality.balance_sheet_imbalance",
    "quality.profit_and_loss_does_not_foot",
    "quality.cash_flow_does_not_tie",
}


def consolidate(
    semantic: Mapping[str, object],
    ontology: Mapping[str, object],
    quality: Mapping[str, object],
) -> dict[str, object]:
    """Fold `semantic`, `ontology`, and `quality` artifacts into one record:
    `{schema_version, section_count, section_types, concepts, concept_count,
    concept_values_count, reconciles, quality}`.

    - `section_count`/`section_types`: every `Mapping` entry in
      `semantic["sections"]` counts toward `section_count`; those with a
      `str` `type` also bucket into `section_types`. Non-`Mapping` entries
      are skipped entirely (do not count as a section).
    - `concepts`/`concept_count`: the sorted, DEDUPLICATED set of
      `ontology["mappings"][].concept` values that are `str` and not
      `"UNKNOWN"`. Malformed/non-`str`/`UNKNOWN` entries contribute nothing.
    - `concept_values_count` (Phase 4): the length of
      `ontology["concept_values"]` (`0` if missing/non-`list`) -- the raw
      count of matched (row x column) numeric records, unfiltered/
      undeduplicated, matching `fdi.core.concept_values.map_concept_values`'s
      own "never dedup" contract.
    - `reconciles` (Phase 4): `True` unless `quality["issues"]` contains an
      entry whose `code` is one of the three reconciliation codes
      (`quality.balance_sheet_imbalance`, `quality.profit_and_loss_does_not_foot`,
      `quality.cash_flow_does_not_tie`). Defaults `True` when there is
      nothing to reconcile against (no matching concepts anywhere) --
      mirrors `validate_all`'s own `ok = error_count == 0` posture (absence
      of a detected problem reads as "fine," not "unknown"); a caller
      wanting to distinguish "verified reconciling" from "nothing to check"
      should read `concept_values_count` alongside it.
    - `quality`: a three-key echo (`ok`, `error_count`, `warning_count`) of
      the quality report; each field independently degrades to its zero
      value (`False`/`0`/`0`) if missing or the wrong type -- never coerced.

    Every input is read defensively (`.get`, `isinstance` checks); a
    missing/malformed key degrades that field to zero/empty, never raises.
    `consolidate({}, {}, {})` is a fully defined, zeroed/emptied record.
    """
    section_types: dict[str, int] = {}
    section_count = 0
    sections_raw = semantic.get("sections")
    if isinstance(sections_raw, list):
        for section in sections_raw:
            if not isinstance(section, Mapping):
                continue
            section_count += 1
            type_ = section.get("type")
            if isinstance(type_, str):
                section_types[type_] = section_types.get(type_, 0) + 1

    concepts_set: set[str] = set()
    mappings_raw = ontology.get("mappings")
    if isinstance(mappings_raw, list):
        for mapping in mappings_raw:
            if not isinstance(mapping, Mapping):
                continue
            concept = mapping.get("concept")
            if isinstance(concept, str) and concept != "UNKNOWN":
                concepts_set.add(concept)
    concepts = sorted(concepts_set)

    cv_raw = ontology.get("concept_values")
    concept_values_count = len(cv_raw) if isinstance(cv_raw, list) else 0

    issues_raw = quality.get("issues")
    issues = issues_raw if isinstance(issues_raw, list) else []
    reconciles = not any(
        isinstance(i, Mapping) and i.get("code") in _RECONCILIATION_ISSUE_CODES
        for i in issues
    )

    ok_raw = quality.get("ok")
    error_count_raw = quality.get("error_count")
    warning_count_raw = quality.get("warning_count")

    return {
        "schema_version": 1,
        "section_count": section_count,
        "section_types": section_types,
        "concepts": concepts,
        "concept_count": len(concepts),
        "concept_values_count": concept_values_count,
        "reconciles": reconciles,
        "quality": {
            "ok": ok_raw if isinstance(ok_raw, bool) else False,
            "error_count": error_count_raw if isinstance(error_count_raw, int) else 0,
            "warning_count": warning_count_raw if isinstance(warning_count_raw, int) else 0,
        },
    }
