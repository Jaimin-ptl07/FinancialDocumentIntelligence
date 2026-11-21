"""Pure validation of artifact bodies and of the field-extraction schema
document. No I/O — belongs in fdi.core."""

import math
import re
from collections.abc import Mapping
from typing import Final


class NonFiniteFloatError(ValueError):
    """A float in an artifact body is NaN or +/-Inf, so the body is not valid JSON."""


_PRIMITIVES: Final = (str, bool, int, type(None))


def validate_finite(obj: object, _path: str = "$") -> None:
    """Raise NonFiniteFloatError at the first non-finite float, else return None.

    bool is checked before int/float since bool is a subclass of int; it is always
    finite. int is exact. Only float can be NaN/Inf.
    """
    if isinstance(obj, bool) or obj is None or isinstance(obj, (str, int)):
        return
    if isinstance(obj, float):
        if not math.isfinite(obj):
            raise NonFiniteFloatError(f"{_path} is non-finite: {obj!r}")
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            validate_finite(v, f"{_path}.{k}")
        return
    if isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            validate_finite(v, f"{_path}[{i}]")
        return
    # Anything else (bytes, sets, custom objects) is not JSON-serialisable anyway.
    raise NonFiniteFloatError(f"{_path} is not a JSON value: {type(obj).__name__}")


_DATA_TYPES: Final = frozenset(
    {"string", "integer", "decimal", "date", "enum", "text", "list", "boolean"}
)
_VERSION_RE: Final = re.compile(r"\d+\.\d+\.\d+")


def bump_patch_version(version: str) -> str:
    """"3.1.0" -> "3.1.1". A version that is not exactly three dot-separated
    integers is returned unchanged -- unreachable from PUT /schema (the
    validator rejects a non-semver `version` before the bump ever runs), but
    total rather than raising, like everything else in fdi.core."""
    parts = version.split(".")
    if len(parts) != 3:
        return version
    try:
        major, minor, patch = (int(p) for p in parts)
    except ValueError:
        return version
    return f"{major}.{minor}.{patch + 1}"


def _walk_fields(node: object) -> "list[Mapping[str, object]]":
    """Every field node in a schema, depth-first through nested groups."""
    out: list[Mapping[str, object]] = []
    if isinstance(node, Mapping):
        if "tag" in node or "field_name" in node:
            out.append(node)
        for key in ("modules", "retrieval_units", "fields"):
            out.extend(_walk_fields(node.get(key)))
    elif isinstance(node, list):
        for item in node:
            out.extend(_walk_fields(item))
    return out


def _is_concept_schema(doc: Mapping[str, object]) -> bool:
    """Does this document carry TAGS -- the v4.3 shape the FOG consumes?"""
    return any(
        isinstance(f.get("tag"), str) and f.get("tag") for f in _walk_fields(doc)
    )


def _validate_concept_schema(doc: Mapping[str, object]) -> list[str]:
    """Gate a v4.3 concept schema on what ITS consumer actually needs.

    MEASURED: `PUT /schema` -- the only runtime way to install a schema --
    rejected the merged v4.3 document with hundreds of
    '"extraction_prompt" must be a non-empty string'. That field was
    DELIBERATELY deleted in v4.3 ("it carried no information the schema does
    not already hold, and one of them actively caused the x10 unit bug"), so
    the gate built for the superseded path made the schema the FOG requires
    impossible to deploy. The seed then stayed at v3.2.0, which carries ZERO
    tags, and every basis mapped nothing: 839 labels unmapped, 0 facts, no
    failure anywhere.

    The gate's PURPOSE is unchanged -- refuse a schema that parses to nothing
    usable. For a concept schema "nothing usable" means no tagged concept,
    because `fdi.core.concepts.all_concepts` is what reads it. Pure and
    total."""
    errors: list[str] = []
    version = doc.get("version")
    if not isinstance(version, str) or not version:
        errors.append('"version" must be a non-empty string')
    modules = doc.get("modules")
    if not isinstance(modules, list) or not modules:
        errors.append('"modules" must be a non-empty list')
        return errors
    tagged = [f for f in _walk_fields(doc) if isinstance(f.get("tag"), str) and f["tag"]]
    if not tagged:
        errors.append("schema declares no tagged concepts; it would map nothing")
    seen: set[str] = set()
    for field in tagged:
        tag = str(field["tag"])
        # A duplicate tag is unreachable: whichever loads second wins, and the
        # taxonomy already shipped one (WCL used at two different sr_no).
        if tag in seen:
            errors.append(f'duplicate tag {tag!r}')
        seen.add(tag)
        name = field.get("field_name")
        if not isinstance(name, str) or not name:
            errors.append(f'tag {tag!r} has no "field_name"')
    return errors


def validate_extraction_schema(doc: object) -> list[str]:
    """Structural validation of a v3.1 field-extraction schema document.

    Returns one human-readable error string per problem, each prefixed with
    the JSON path it was found at; an empty list means valid. Pure and
    total: never raises, whatever `doc` is.

    This is the gate on PUT /schema, and it is deliberately structural
    rather than semantic: the failure it exists to prevent is a saved schema
    that PARSES to zero fields, which turns POST /extract/document into a
    silent no-op with a 200 response. So it checks exactly what
    parse_field_specs needs -- the modules -> retrieval_units -> fields
    nesting, a non-blank unit retrieval_query, and each field's
    field_name/data_type/extraction_prompt -- plus unit_id uniqueness (unit
    ids are how a UI addresses a unit) and a known data_type. It does NOT
    police prompt wording, aliases, examples, or the validation.rules
    strings: those are editorial, and refusing a save over them would make
    the endpoint unusable.
    """
    if not isinstance(doc, Mapping):
        return ["schema must be a JSON object"]

    # A CONCEPT-SHAPED (v4.3) document is validated against what ITS consumer
    # needs, not against the superseded one's.
    if _is_concept_schema(doc):
        return _validate_concept_schema(doc)

    errors: list[str] = []
    version = doc.get("version")
    if not isinstance(version, str) or not _VERSION_RE.fullmatch(version):
        errors.append('"version" must be a semantic version string like "3.1.0"')

    modules = doc.get("modules")
    if not isinstance(modules, list) or not modules:
        errors.append('"modules" must be a non-empty list')
        return errors

    seen_unit_ids: set[str] = set()
    for mi, module in enumerate(modules):
        where = f"modules[{mi}]"
        if not isinstance(module, Mapping):
            errors.append(f"{where} must be an object")
            continue
        for key in ("key", "name"):
            value = module.get(key)
            if not isinstance(value, str) or not value:
                errors.append(f'{where}."{key}" must be a non-empty string')

        units = module.get("retrieval_units")
        if not isinstance(units, list) or not units:
            errors.append(f'{where}."retrieval_units" must be a non-empty list')
            continue

        for ui, unit in enumerate(units):
            uwhere = f"{where}.retrieval_units[{ui}]"
            if not isinstance(unit, Mapping):
                errors.append(f"{uwhere} must be an object")
                continue

            unit_id = unit.get("unit_id")
            if not isinstance(unit_id, str) or not unit_id:
                errors.append(f'{uwhere}."unit_id" must be a non-empty string')
            elif unit_id in seen_unit_ids:
                errors.append(f'{uwhere}."unit_id" {unit_id!r} is duplicated')
            else:
                seen_unit_ids.add(unit_id)

            query = unit.get("retrieval_query")
            if not isinstance(query, str) or not query.strip():
                errors.append(f'{uwhere}."retrieval_query" must be a non-blank string')

            fields = unit.get("fields")
            if not isinstance(fields, list) or not fields:
                errors.append(f'{uwhere}."fields" must be a non-empty list')
                continue

            for fi, field in enumerate(fields):
                _validate_field_node(field, f"{uwhere}.fields[{fi}]", errors)

    return errors


_ORIGINS: Final = frozenset({"extracted", "derived"})


def _validate_field_node(field: object, where: str, errors: list[str]) -> None:
    """Validate one field node, recursing into v4 group nodes (a field carrying
    its own non-empty `fields` list). The rules are ADDITIVE so a v3.1 flat leaf
    (no `origin`, no `tag`, no children) is judged exactly as before:

    * every node needs a non-empty `field_name`;
    * a group node (has children) needs only that, then recurses;
    * a leaf's `origin`, if present, must be "extracted" or "derived";
    * a v4 leaf (origin present) needs a non-empty `tag`;
    * a derived leaf needs neither `extraction_prompt` nor `data_type`;
    * every other leaf (extracted, or a v3.1 leaf with no origin) needs
      `data_type` and `extraction_prompt`, as before;
    * any `data_type` that is present must be one of the known types.
    """
    if not isinstance(field, Mapping):
        errors.append(f"{where} must be an object")
        return

    name = field.get("field_name")
    if not isinstance(name, str) or not name:
        errors.append(f'{where}."field_name" must be a non-empty string')

    children = field.get("fields")
    if isinstance(children, list) and children:
        for ci, child in enumerate(children):
            _validate_field_node(child, f"{where}.fields[{ci}]", errors)
        return

    origin = field.get("origin")
    if origin is not None and (not isinstance(origin, str) or origin not in _ORIGINS):
        errors.append(f'{where}."origin" {origin!r} is not one of {sorted(_ORIGINS)}')

    data_type = field.get("data_type")
    if isinstance(data_type, str) and data_type and data_type not in _DATA_TYPES:
        errors.append(
            f'{where}."data_type" {data_type!r} is not one of {sorted(_DATA_TYPES)}'
        )

    if origin is not None:  # v4 leaf
        tag = field.get("tag")
        if not isinstance(tag, str) or not tag:
            errors.append(f'{where}."tag" must be a non-empty string')

    if origin != "derived":  # extracted leaf, or a v3.1 leaf with no origin
        for key in ("data_type", "extraction_prompt"):
            value = field.get(key)
            if not isinstance(value, str) or not value:
                errors.append(f'{where}."{key}" must be a non-empty string')


# --- v4.3 composition invariants -------------------------------------------
#
# The v4.3 design has one law that makes a schema self-validating: a parent's
# `derivation.inputs` must be exactly the tags of its own child concepts. If the
# two can drift, the schema states a formula it does not structurally hold, and
# nothing catches it -- which is how `AUTOPL001` came to sit as a SIBLING of the
# three concepts it was supposed to sum.
#
# Three formula kinds, and only `derivation` is bound to children:
#   derivation      composition -- inputs ARE the children
#   waterfall       inputs are PEERS elsewhere in the statement
#   reconciliation  a reported value checked against a formula
_FORMULA_KINDS: Final = ("derivation", "waterfall", "reconciliation")


def _child_concepts(node: Mapping[str, object]) -> list[str]:
    """Direct child CONCEPTS, seeing through untagged presentation groups.

    A grouping node with no tag is layout, not meaning: its tagged children are
    the parent's children for composition purposes."""
    out: list[str] = []
    children = node.get("fields")
    if not isinstance(children, list):
        return out
    for child in children:
        if not isinstance(child, Mapping):
            continue
        tag = child.get("tag")
        if isinstance(tag, str) and tag:
            out.append(tag)
        else:
            out.extend(_child_concepts(child))
    return out


def _input_tags(formula: object) -> list[str] | None:
    """`inputs` as plain tags, accepting both the bare and signed-weight forms."""
    if not isinstance(formula, Mapping):
        return None
    raw = formula.get("inputs")
    if not isinstance(raw, list):
        return None
    out: list[str] = []
    for item in raw:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, Mapping) and isinstance(item.get("tag"), str):
            out.append(str(item["tag"]))
    return out


def validate_composition(doc: object) -> list[str]:
    """Check the v4.3 composition law across a whole schema document.

    Returns one error per violation; empty means the schema holds. Pure and
    total. Deliberately silent about `waterfall` and `reconciliation` inputs --
    those legitimately name peers and are NOT constrained to children."""
    errors: list[str] = []
    modules = doc.get("modules") if isinstance(doc, Mapping) else None
    if not isinstance(modules, list):
        return errors
    seen: dict[str, str] = {}

    def walk(node: Mapping[str, object], where: str) -> None:
        tag = node.get("tag")
        if isinstance(tag, str) and tag:
            if tag in seen:
                errors.append(f"{where}: tag {tag!r} already defined at {seen[tag]}")
            else:
                seen[tag] = where

        kinds = [k for k in _FORMULA_KINDS if isinstance(node.get(k), Mapping)]
        if len(kinds) > 1:
            errors.append(f"{where}: a concept may carry one formula, found {kinds}")

        derivation = node.get("derivation")
        if isinstance(derivation, Mapping):
            inputs = _input_tags(derivation)
            children = _child_concepts(node)
            if inputs is None:
                errors.append(f'{where}."derivation.inputs" must be a list')
            elif not children:
                errors.append(
                    f"{where}: derivation declared but the concept has no child "
                    f"concepts to compose ({len(inputs)} input(s) named)"
                )
            elif sorted(inputs) != sorted(children):
                missing = sorted(set(children) - set(inputs))
                extra = sorted(set(inputs) - set(children))
                detail = []
                if missing:
                    detail.append(f"children not summed: {missing}")
                if extra:
                    detail.append(f"inputs that are not children: {extra}")
                errors.append(f"{where}: derivation.inputs != child concepts -- "
                              + "; ".join(detail))

        nested = node.get("fields")
        if isinstance(nested, list):
            for i, child in enumerate(nested):
                if isinstance(child, Mapping):
                    walk(child, f"{where}.fields[{i}]")

    for mi, module in enumerate(modules):
        if not isinstance(module, Mapping):
            continue
        units = module.get("retrieval_units")
        if not isinstance(units, list):
            continue
        for ui, unit in enumerate(units):
            if not isinstance(unit, Mapping):
                continue
            fields = unit.get("fields")
            if not isinstance(fields, list):
                continue
            for fi, field in enumerate(fields):
                if isinstance(field, Mapping):
                    walk(field, f"$.modules[{mi}].retrieval_units[{ui}].fields[{fi}]")
    return errors
