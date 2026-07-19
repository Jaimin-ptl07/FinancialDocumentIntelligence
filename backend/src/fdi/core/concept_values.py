"""Concept -> numeric value extraction over a document's `structure` artifact.

Companion to `fdi.core.ontology.map_document` (which maps AST labels, never
numbers) -- reuses `fdi.core.tables.find_concept_rows` (RT-6) to do the actual
row-matching/parsing; this module only flattens + tags the results with
page/table_index citations.

Pure -- no I/O. Lives in its OWN module, not inside `ontology.py` or
`tables.py`, specifically to avoid the ontology<->tables import cycle a shared
home would create (`tables.py` already imports `ontology.py` for
`CanonicalConcept`/`normalize_label`/`synonyms_for`). See design spec
`docs/superpowers/specs/2026-07-19-ph4-values-reconciliation-design.md` §2
decision 1 for the full rationale.
"""

from collections.abc import Mapping
from typing import cast

from fdi.core.ontology import CanonicalConcept
from fdi.core.tables import find_concept_rows


def _first_page(table: Mapping[str, object]) -> int | None:
    regions = table.get("bounding_regions")
    if not isinstance(regions, list) or not regions:
        return None
    first = regions[0]
    if not isinstance(first, Mapping):
        return None
    page = first.get("page_number")
    return page if isinstance(page, int) and not isinstance(page, bool) else None


def map_concept_values(structure: Mapping[str, object]) -> dict[str, object]:
    """For every real `CanonicalConcept` (`UNKNOWN` excluded -- `synonyms_for`
    is always `[]` for it, so `find_concept_rows` would trivially return `[]`)
    and every table in `structure["tables"]`, run
    `find_concept_rows(table["grid"], concept)` and flatten every
    `{row_label, values: [{column_header, raw, value}]}` match into one FLAT
    record per (row, column): `{concept, row_label, value, raw,
    column_header, column_index, page, table_index}`.

    `concept` is `CanonicalConcept.value` (a plain `str`), matching
    `map_document`'s `mappings[].concept` convention. `column_index` is the
    0-based position of this value among the matched row's DATA columns.
    `page` is the table's own `bounding_regions[0]["page_number"]`.
    `table_index` is the position in `structure["tables"]`.

    NEVER deduplicates or picks "the" match for an ambiguous concept (RT-6
    decision 5's "Cost of revenue" false positive is preserved verbatim,
    tagged and returned, not filtered) -- every consumer sees `row_label` and
    can judge for itself.

    Degrades to `{"schema_version": 1, "concept_values": []}` on missing/
    malformed `structure`/`tables`/a table entry that isn't a `Mapping`/a
    missing or non-`Mapping` `grid`, never raises.
    """
    tables_raw = structure.get("tables")
    tables = tables_raw if isinstance(tables_raw, list) else []
    out: list[dict[str, object]] = []
    for table_index, table in enumerate(tables):
        if not isinstance(table, Mapping):
            continue
        grid = table.get("grid")
        if not isinstance(grid, Mapping):
            continue
        page = _first_page(table)
        for concept in CanonicalConcept:
            if concept is CanonicalConcept.UNKNOWN:
                continue
            for match in find_concept_rows(grid, concept):
                row_label = cast(str, match.get("row_label", ""))
                values = cast(list[dict[str, object]], match.get("values", []))
                for column_index, v in enumerate(values):
                    out.append({
                        "concept": concept.value,
                        "row_label": row_label,
                        "value": v.get("value"),
                        "raw": v.get("raw"),
                        "column_header": v.get("column_header"),
                        "column_index": column_index,
                        "page": page,
                        "table_index": table_index,
                    })
    return {"schema_version": 1, "concept_values": out}
