"""Pure build of the v4 field-extraction schema from the normalized taxonomy.

No I/O. Consumes `fdi.core.taxonomy.parse_taxonomy`'s output and emits a schema
document in the same `modules -> retrieval_units -> fields` shape the pipeline
already parses (`fdi.core.extractor.parse_field_specs`) and validates
(`fdi.core.schema.validate_extraction_schema`), plus v4 additions:

* module = statement (profit_and_loss / assets / liabilities / equity)
* retrieval_unit = BlockID (so ~500 tags become ~9 retrieval units, not 500)
* fields are NESTED: a section-header row becomes a `group` node whose `fields`
  are the leaves beneath it; every leaf carries a canonical `tag`, an `origin`
  ("extracted" | "derived"), and its `sr_no` so Milestone 2 can rebuild the exact
  calculation hierarchy from the taxonomy's own "Total of X to Y" numbering.

Derived leaves (the `do not tag` / `not tagged` subtotals) carry `origin:
"derived"` and a `derivation` stub with empty `inputs` -- populated for real in
Milestone 2. They are NOT extracted (no `extraction_prompt`), so
`parse_field_specs` skips them.
"""

from collections.abc import Mapping, Sequence
from typing import Final

_REPORTING_UNIT: Final = "INR Lakhs"

_STATEMENT_LABELS: Final = {
    "profit_and_loss": "Profit and Loss",
    "assets": "Assets",
    "liabilities": "Liabilities",
    "equity": "Equity",
}


def _statement_label(statement: str) -> str:
    return _STATEMENT_LABELS.get(statement, statement.replace("_", " ").title())


def _data_type(particulars: str) -> tuple[str, bool]:
    """(data_type, is_ratio). Default decimal (currency in INR Lakhs); a few
    keyword overrides stay inside the pipeline's known data-type set."""
    p = particulars.lower()
    if "%" in p or "percentage" in p or "ratio" in p or "margin" in p:
        return "decimal", True
    if "days" in p or "number of" in p:
        return "integer", False
    if "date" in p or "as at" in p or "year ended" in p:
        return "date", False
    if any(k in p for k in ("name", "address", "description", "policy")):
        return "text", False
    return "decimal", False


def _extraction_prompt(particulars: str, statement: str) -> str:
    return (
        f"Extract the value of '{particulars}' from the "
        f"{_statement_label(statement)} statement. Report the current-year and "
        f"prior-year figures in {_REPORTING_UNIT}. If the line item is not "
        f"present in the report, return null."
    )


def _leaf_node(
    row: Mapping[str, object], statement: str, field_path: str
) -> dict[str, object]:
    particulars = str(row.get("particulars") or "")
    tag = str(row.get("tag") or "")
    origin = str(row.get("origin") or "extracted")
    data_type, is_ratio = _data_type(particulars)
    node: dict[str, object] = {
        "field_name": particulars,
        "tag": tag,
        "origin": origin,
        "data_type": data_type,
        "sr_no": row.get("sr_no"),
        "field_path": field_path,
        "aliases": [particulars],
        "is_new": bool(row.get("is_new")),
        "is_ratio": is_ratio,
        "redirect": {
            "dataset1": row.get("redirect_dataset1"),
            "dataset2": row.get("redirect_dataset2"),
        },
    }
    if origin == "derived":
        # Structural stub only; Milestone 2 fills `inputs` from the "Total of
        # X to Y" numbering. Kept off the extracted path (no extraction_prompt).
        node["derivation"] = {"method": "sum", "inputs": []}
    else:
        node["extraction_prompt"] = _extraction_prompt(particulars, statement)
    return node


def _unique_path(base: str, seen: set[str]) -> str:
    if base not in seen:
        seen.add(base)
        return base
    n = 2
    while f"{base}#{n}" in seen:
        n += 1
    path = f"{base}#{n}"
    seen.add(path)
    return path


def _build_unit(
    block_id: str, rows: Sequence[Mapping[str, object]], statement: str, seen: set[str]
) -> dict[str, object]:
    """One retrieval unit for one BlockID. Section-header rows open `group`
    nodes; leaves/derived attach to the most recent open group (or the unit
    itself). Empty groups are dropped so a header with no children never
    masquerades as an untyped leaf."""
    top: list[dict[str, object]] = []
    current_fields: list[dict[str, object]] | None = None
    headers: list[str] = []

    def _emit(node: dict[str, object]) -> None:
        if current_fields is not None:
            current_fields.append(node)
        else:
            top.append(node)

    for row in rows:
        if row.get("excluded"):
            continue
        particulars = str(row.get("particulars") or "")
        if row.get("is_section_header"):
            if particulars:
                headers.append(particulars)
            group_fields: list[dict[str, object]] = []
            top.append({
                "field_name": particulars or block_id,
                "node_type": "group",
                "sr_no": row.get("sr_no"),
                "fields": group_fields,
            })
            current_fields = group_fields
            continue
        tag = row.get("tag")
        if not tag:
            continue  # a non-header row with no tag carries no field
        base = f"{statement}.{block_id}.{tag}"
        _emit(_leaf_node(row, statement, _unique_path(base, seen)))

    # Drop groups that ended up empty.
    fields = [
        n for n in top
        if n.get("node_type") != "group" or n.get("fields")
    ]

    unit_title = headers[0] if headers else block_id
    query = f"{unit_title} ({_statement_label(statement)}). " + "; ".join(headers[:6])
    return {
        "unit_id": block_id,
        "unit_title": unit_title,
        "statement_type": statement,
        "retrieval_query": query.strip(),
        "locator_keywords": [unit_title, *headers],
        "fields": fields,
    }


def _build_module(statement: Mapping[str, object], seen: set[str]) -> dict[str, object]:
    name = str(statement.get("statement") or "unknown")
    rows_obj = statement.get("rows")
    rows = [r for r in rows_obj if isinstance(r, Mapping)] if isinstance(rows_obj, list) else []

    # Group rows by BlockID, order-preserving on first appearance.
    order: list[str] = []
    by_block: dict[str, list[Mapping[str, object]]] = {}
    for r in rows:
        block = str(r.get("block_id") or "UNBLOCKED")
        if block not in by_block:
            by_block[block] = []
            order.append(block)
        by_block[block].append(r)

    units = [_build_unit(b, by_block[b], name, seen) for b in order]
    units = [u for u in units if u["fields"]]  # a block with no fields is no unit
    return {
        "key": f"module_{name}",
        "name": _statement_label(name),
        "statement": name,
        "retrieval_units": units,
    }


def build_schema(normalized: Mapping[str, object]) -> dict[str, object]:
    """Normalized taxonomy -> v4 field-extraction schema document.

    Pure and total. Field paths are made globally unique (a duplicate tag such
    as `WCL` yields `...WCL` and `...WCL#2`); the tag itself is left intact so
    downstream tagging keeps the customer's code.
    """
    seen_paths: set[str] = set()
    statements_obj = normalized.get("statements")
    statements = (
        [s for s in statements_obj if isinstance(s, Mapping)]
        if isinstance(statements_obj, list)
        else []
    )
    modules = [_build_module(s, seen_paths) for s in statements]
    modules = [m for m in modules if m["retrieval_units"]]

    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema-descriptor",
        "title": "Financial Document Intelligence - Taxonomy Field Schema (v4)",
        "version": "4.0.0",
        "reporting_unit": _REPORTING_UNIT,
        "taxonomy_source": normalized.get("source"),
        "origin_model": {
            "extracted": "read from the report; provenance is a bounding box",
            "derived": "computed; provenance is a formula over input tags (Milestone 2)",
        },
        "modules": modules,
    }
