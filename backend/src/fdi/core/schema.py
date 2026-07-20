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
                fwhere = f"{uwhere}.fields[{fi}]"
                if not isinstance(field, Mapping):
                    errors.append(f"{fwhere} must be an object")
                    continue
                for key in ("field_name", "data_type", "extraction_prompt"):
                    value = field.get(key)
                    if not isinstance(value, str) or not value:
                        errors.append(f'{fwhere}."{key}" must be a non-empty string')
                data_type = field.get("data_type")
                if isinstance(data_type, str) and data_type and data_type not in _DATA_TYPES:
                    errors.append(
                        f'{fwhere}."data_type" {data_type!r} is not one of '
                        f"{sorted(_DATA_TYPES)}"
                    )

    return errors
