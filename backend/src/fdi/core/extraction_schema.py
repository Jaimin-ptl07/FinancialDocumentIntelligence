"""Extraction schemas as data (S3, ADR-0004) -- the pure half.

An Extraction schema is a list of Fields an Invitee wants read out of
Reports. This module owns what a Field *is*: how one is parsed from a request
or a stored row (`field_from_mapping`), what makes a set of Fields valid
(`validate_fields`), which part of a Field changes what the model reads
(`definition_hash`), how a Field becomes the engine's `FieldSpec`
(`to_field_spec`), what changed between two versions (`diff_fields`), and how
the FDI standard v4.3 document becomes version 1 of the standard schema
(`compile_standard_schema`).

Pure (`fdi.core` law): no I/O, no clock, no RNG. Field ids for NEW user
Fields are minted by the caller; the standard schema's ids are derived from
its tags (uuid5), so re-importing a later standard version keeps each Field's
identity and therefore its cached reads.

Retrieval groups. The engine retrieves ONCE per group and asks the model for
every stale Field of the group in one call (the standard schema's
"retrieve-once, extract-many"). A Field names its group explicitly, or is
grouped automatically: by its statement when it has one (query
"<Statement> statement"), else on its own (query built from its own name,
meaning and aliases). An automatic group never depends on its SIBLINGS --
adding a Field to a schema must not change any other Field's definition hash,
or story 11 ("adding one Field re-extracts only that Field") would be false.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

from fdi.core.extractor import FieldSpec

DATA_TYPES: Final = frozenset(
    {"string", "integer", "decimal", "date", "enum", "text", "list", "boolean"}
)
NUMERIC_DATA_TYPES: Final = frozenset({"decimal", "integer"})
BASIS_PREFERENCES: Final = frozenset({"standalone", "consolidated", "both"})
ORIGINS: Final = frozenset({"extracted", "derived"})
DERIVATION_METHODS: Final = frozenset({"sum", "diff"})
VALIDATION_RULES: Final = frozenset({"min", "max", "non_negative", "pattern", "allowed_values"})

STATEMENT_LABELS: Final = {
    "profit_and_loss": "Profit and Loss",
    "assets": "Assets",
    "liabilities": "Liabilities",
    "equity": "Equity",
    "cash_flow": "Cash Flow",
    "balance_sheet": "Balance Sheet",
    "notes": "Notes to Accounts",
}

MAX_FIELDS: Final = 2000
MAX_GROUPS: Final = 200
_MAX_NAME: Final = 200
_MAX_TEXT: Final = 4000
_MAX_LIST: Final = 50
_TAG_RE: Final = re.compile(r"^[A-Za-z0-9_./-]{1,64}$")
_GROUP_KEY_RE: Final = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")

# uuid5 namespace for the FDI standard schema's Field ids. NEVER change it:
# every cached read of a standard Field is keyed by the id derived from it.
STANDARD_FIELD_NAMESPACE: Final = uuid.UUID("5b0f2c1e-4d6a-4c55-9a3f-0f4d1e2a7c11")

DEFAULT_REVIEW_THRESHOLD: Final = 0.60
"""Confidence below which a value is marked `needs_review` unless the schema
sets its own -- `fdi.core.confidence.REVIEW`, today's engine's band edge."""


@dataclass(frozen=True)
class Derivation:
    """`sum`: Σ weight·input. `diff`: first input minus the rest. Inputs are
    Field tags of the same schema version."""

    method: str
    inputs: tuple[tuple[str, float], ...]


@dataclass(frozen=True)
class GroupDefinition:
    """One retrieval group: one retrieval and one model call per run."""

    key: str
    title: str
    retrieval_query: str
    locator_keywords: tuple[str, ...] = ()
    basis_preference: str = "standalone"
    statement: str | None = None


@dataclass(frozen=True)
class FieldDefinition:
    field_id: str
    name: str
    data_type: str
    instructions: str
    description: str = ""
    unit: str | None = None
    statement: str | None = None
    group: str | None = None
    tag: str | None = None
    aliases: tuple[str, ...] = ()
    note_aliases: tuple[str, ...] = ()
    periods: tuple[str, ...] = ()
    allowed_values: tuple[str, ...] = ()
    origin: str = "extracted"
    derivation: Derivation | None = None
    required: bool = False
    validation: Mapping[str, object] = field(default_factory=dict)
    extra: Mapping[str, object] = field(default_factory=dict)


class SchemaInputError(ValueError):
    """A Field or group as submitted cannot be accepted. `errors` holds one
    path-prefixed message per problem."""

    def __init__(self, errors: Sequence[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = list(errors)


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def _text(raw: Mapping[str, object], key: str, where: str, errors: list[str], *,
          required: bool = False, limit: int = _MAX_TEXT) -> str:
    value = raw.get(key)
    if value is None:
        if required:
            errors.append(f'{where}."{key}" is required')
        return ""
    if not isinstance(value, str):
        errors.append(f'{where}."{key}" must be a string')
        return ""
    value = value.strip()
    if required and not value:
        errors.append(f'{where}."{key}" must not be blank')
    if len(value) > limit:
        errors.append(f'{where}."{key}" is longer than {limit} characters')
    return value


def _optional_text(raw: Mapping[str, object], key: str, where: str,
                   errors: list[str]) -> str | None:
    value = _text(raw, key, where, errors, limit=_MAX_NAME)
    return value or None


def _str_list(raw: Mapping[str, object], key: str, where: str,
              errors: list[str]) -> tuple[str, ...]:
    value = raw.get(key)
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        errors.append(f'{where}."{key}" must be a list of strings')
        return ()
    if len(value) > _MAX_LIST:
        errors.append(f'{where}."{key}" holds more than {_MAX_LIST} entries')
    out: list[str] = []
    for v in value:
        v = v.strip()
        if v and v not in out:
            out.append(v[:_MAX_NAME])
    return tuple(out)


def _finite_number(value: object) -> bool:
    return (isinstance(value, int | float) and not isinstance(value, bool)
            and math.isfinite(float(value)))


def _validation(raw: Mapping[str, object], where: str, errors: list[str]) -> dict[str, object]:
    value = raw.get("validation")
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        errors.append(f'{where}."validation" must be an object')
        return {}
    out: dict[str, object] = {}
    for key, rule in value.items():
        path = f'{where}."validation"."{key}"'
        if key not in VALIDATION_RULES:
            errors.append(f"{path} is not one of {sorted(VALIDATION_RULES)}")
            continue
        if key in ("min", "max"):
            if not _finite_number(rule):
                errors.append(f"{path} must be a finite number")
                continue
        elif key == "non_negative":
            if not isinstance(rule, bool):
                errors.append(f"{path} must be true or false")
                continue
        elif key == "pattern":
            if not isinstance(rule, str) or not rule or len(rule) > 500:
                errors.append(f"{path} must be a regular expression of at most 500 characters")
                continue
            try:
                re.compile(rule)
            except re.error as exc:
                errors.append(f"{path} is not a valid regular expression: {exc}")
                continue
        elif key == "allowed_values" and (
            not isinstance(rule, list) or not rule
            or not all(isinstance(v, str) for v in rule)
        ):
            errors.append(f"{path} must be a non-empty list of strings")
            continue
        out[str(key)] = rule
    lo, hi = out.get("min"), out.get("max")
    if _finite_number(lo) and _finite_number(hi) and float(lo) > float(hi):  # type: ignore[arg-type]
        errors.append(f'{where}."validation" has min greater than max')
    return out


def _derivation(raw: Mapping[str, object], where: str, errors: list[str]) -> Derivation | None:
    value = raw.get("derivation")
    if value is None:
        return None
    path = f'{where}."derivation"'
    if not isinstance(value, Mapping):
        errors.append(f"{path} must be an object")
        return None
    method = value.get("method", "sum")
    if method not in DERIVATION_METHODS:
        errors.append(f'{path}."method" must be one of {sorted(DERIVATION_METHODS)}')
        return None
    inputs_raw = value.get("inputs")
    if not isinstance(inputs_raw, list):
        errors.append(f'{path}."inputs" must be a list')
        return None
    inputs: list[tuple[str, float]] = []
    for i, item in enumerate(inputs_raw):
        if isinstance(item, str) and item:
            inputs.append((item, 1.0))
        elif (isinstance(item, Mapping) and isinstance(item.get("tag"), str)
              and item.get("tag")):
            weight = item.get("weight", 1)
            if not _finite_number(weight):
                errors.append(f'{path}."inputs"[{i}]."weight" must be a finite number')
                continue
            inputs.append((str(item["tag"]), float(weight)))  # type: ignore[arg-type,unused-ignore]
        else:
            errors.append(f'{path}."inputs"[{i}] must be a tag or {{"tag", "weight"}}')
    return Derivation(method=str(method), inputs=tuple(inputs))


def default_instructions(name: str, description: str, statement: str | None) -> str:
    """What the model is told when a Field states no instructions of its own."""
    where = f" from the {STATEMENT_LABELS.get(statement, statement)} statement" if statement else ""
    text = (
        f"Extract the value of '{name}'{where}. Report the current reporting-year "
        "figure exactly as printed in the document's own reporting unit; return "
        "null if it is not present."
    )
    return f"{text} Meaning: {description}" if description else text


def field_from_mapping(raw: object, *, field_id: str, where: str = "field") -> FieldDefinition:
    """One Field from a request body or a stored draft entry. Raises
    `SchemaInputError` naming every problem at once. `field_id` is the
    caller's: an existing Field keeps its id, a new one gets a fresh one."""
    if not isinstance(raw, Mapping):
        raise SchemaInputError([f"{where} must be an object"])
    errors: list[str] = []
    name = _text(raw, "name", where, errors, required=True, limit=_MAX_NAME)
    description = _text(raw, "description", where, errors)
    data_type = raw.get("data_type", "decimal")
    if data_type not in DATA_TYPES:
        errors.append(f'{where}."data_type" must be one of {sorted(DATA_TYPES)}')
        data_type = "decimal"
    origin = raw.get("origin", "extracted")
    if origin not in ORIGINS:
        errors.append(f'{where}."origin" must be one of {sorted(ORIGINS)}')
        origin = "extracted"
    statement = _optional_text(raw, "statement", where, errors)
    group = _optional_text(raw, "group", where, errors)
    if group is not None and not _GROUP_KEY_RE.match(group):
        errors.append(f'{where}."group" must be 1-64 letters, digits or _.:-')
    tag = _optional_text(raw, "tag", where, errors)
    if tag is not None and not _TAG_RE.match(tag):
        errors.append(f'{where}."tag" must be 1-64 letters, digits or _./-')
    derivation = _derivation(raw, where, errors)
    if origin == "derived" and derivation is None:
        derivation = Derivation(method="sum", inputs=())
    if origin == "extracted" and derivation is not None:
        errors.append(f'{where}."derivation" is only allowed on a derived Field')
    periods = _str_list(raw, "periods", where, errors)
    if periods and set(periods) - {"current", "prior"}:
        errors.append(f'{where}."periods" may only hold "current" and "prior"')
    required = raw.get("required", False)
    if not isinstance(required, bool):
        errors.append(f'{where}."required" must be true or false')
        required = False
    extra = raw.get("extra", {})
    if not isinstance(extra, Mapping):
        errors.append(f'{where}."extra" must be an object')
        extra = {}
    unit = _optional_text(raw, "unit", where, errors)
    validation = _validation(raw, where, errors)
    aliases = _str_list(raw, "aliases", where, errors)
    note_aliases = _str_list(raw, "note_aliases", where, errors)
    allowed_values = _str_list(raw, "allowed_values", where, errors)
    if data_type == "enum" and not allowed_values:
        errors.append(f'{where}."allowed_values" is required for an enum Field')
    instructions = _text(raw, "instructions", where, errors)
    if errors:
        raise SchemaInputError(errors)
    if not instructions and origin == "extracted":
        instructions = default_instructions(name, description, statement)
    return FieldDefinition(
        field_id=field_id, name=name, description=description, data_type=str(data_type),
        unit=unit, instructions=instructions, statement=statement, group=group, tag=tag,
        aliases=aliases, note_aliases=note_aliases, periods=periods,
        allowed_values=allowed_values, origin=str(origin), derivation=derivation,
        required=required, validation=validation, extra=dict(extra),
    )


def field_to_mapping(f: FieldDefinition) -> dict[str, object]:
    """The stored/served shape of a Field; `field_from_mapping` reads it back."""
    out: dict[str, object] = {
        "field_id": f.field_id, "name": f.name, "description": f.description,
        "data_type": f.data_type, "unit": f.unit, "instructions": f.instructions,
        "statement": f.statement, "group": f.group, "tag": f.tag,
        "aliases": list(f.aliases), "note_aliases": list(f.note_aliases),
        "periods": list(f.periods), "allowed_values": list(f.allowed_values),
        "origin": f.origin, "required": f.required, "validation": dict(f.validation),
        "extra": dict(f.extra), "derivation": None,
    }
    if f.derivation is not None:
        out["derivation"] = {
            "method": f.derivation.method,
            "inputs": [{"tag": t, "weight": w} for t, w in f.derivation.inputs],
        }
    return out


def group_from_mapping(raw: object, *, where: str = "group") -> GroupDefinition:
    if not isinstance(raw, Mapping):
        raise SchemaInputError([f"{where} must be an object"])
    errors: list[str] = []
    key = _text(raw, "key", where, errors, required=True, limit=64)
    if key and not _GROUP_KEY_RE.match(key):
        errors.append(f'{where}."key" must be 1-64 letters, digits or _.:-')
    if key.startswith("auto:"):
        errors.append(f'{where}."key" may not start with "auto:" (reserved)')
    title = _text(raw, "title", where, errors, limit=_MAX_NAME) or key
    query = _text(raw, "retrieval_query", where, errors, required=True)
    keywords = _str_list(raw, "locator_keywords", where, errors)
    basis = raw.get("basis_preference", "standalone")
    if basis not in BASIS_PREFERENCES:
        errors.append(f'{where}."basis_preference" must be one of {sorted(BASIS_PREFERENCES)}')
        basis = "standalone"
    statement = _optional_text(raw, "statement", where, errors)
    if errors:
        raise SchemaInputError(errors)
    return GroupDefinition(key=key, title=title, retrieval_query=query,
                           locator_keywords=keywords, basis_preference=str(basis),
                           statement=statement)


def group_to_mapping(g: GroupDefinition) -> dict[str, object]:
    return {"key": g.key, "title": g.title, "retrieval_query": g.retrieval_query,
            "locator_keywords": list(g.locator_keywords),
            "basis_preference": g.basis_preference, "statement": g.statement}


# --------------------------------------------------------------------------
# Whole-schema rules
# --------------------------------------------------------------------------


def validate_fields(fields: Sequence[FieldDefinition],
                    groups: Sequence[GroupDefinition]) -> list[str]:
    """What makes a set of Fields publishable, one message per problem:
    unique ids and tags; named groups exist; derived inputs resolve to
    a tag of this set; no derivation cycle."""
    errors: list[str] = []
    if len(fields) > MAX_FIELDS:
        errors.append(f"a schema holds at most {MAX_FIELDS} Fields")
    if len(groups) > MAX_GROUPS:
        errors.append(f"a schema holds at most {MAX_GROUPS} groups")
    group_keys: set[str] = set()
    for g in groups:
        if g.key in group_keys:
            errors.append(f"group {g.key!r} is defined twice")
        group_keys.add(g.key)
    # Names may repeat (the standard schema has many "Others"): the model
    # answers each Field under its tag or id, never its name.
    ids: set[str] = set()
    tags: dict[str, FieldDefinition] = {}
    for f in fields:
        if f.field_id in ids:
            errors.append(f"Field id {f.field_id!r} appears twice")
        ids.add(f.field_id)
        if f.tag is not None:
            if f.tag in tags:
                errors.append(f"tag {f.tag!r} appears twice")
            tags[f.tag] = f
        if f.group is not None and f.group not in group_keys:
            errors.append(f"Field {f.name!r} names group {f.group!r}, which is not defined")
    for f in fields:
        if f.derivation is None:
            continue
        for tag, _w in f.derivation.inputs:
            if tag not in tags:
                errors.append(f"derived Field {f.name!r} uses tag {tag!r}, which no Field has")
    errors.extend(_cycle_errors(fields, tags))
    return errors


def _cycle_errors(fields: Sequence[FieldDefinition],
                  tags: Mapping[str, FieldDefinition]) -> list[str]:
    state: dict[str, int] = {}  # 1 = on the stack, 2 = done
    errors: list[str] = []

    def visit(f: FieldDefinition) -> bool:
        if state.get(f.field_id) == 1:
            return True
        if state.get(f.field_id) == 2 or f.derivation is None:
            return False
        state[f.field_id] = 1
        for tag, _w in f.derivation.inputs:
            child = tags.get(tag)
            if child is not None and visit(child):
                return True
        state[f.field_id] = 2
        return False

    for f in fields:
        if f.field_id not in state and visit(f):
            errors.append(f"derived Field {f.name!r} is part of a derivation cycle")
            # mark the rest of the cycle done so each cycle is reported once
            for k, v in list(state.items()):
                if v == 1:
                    state[k] = 2
    return errors


# --------------------------------------------------------------------------
# Groups, hashing, engine specs
# --------------------------------------------------------------------------


def resolve_group(f: FieldDefinition, groups: Mapping[str, GroupDefinition]) -> GroupDefinition:
    """The retrieval group `f` is read in: the one it names, else automatic
    (see module docstring -- never sibling-dependent)."""
    if f.group is not None and f.group in groups:
        return groups[f.group]
    if f.statement:
        label = STATEMENT_LABELS.get(f.statement, f.statement.replace("_", " ").title())
        return GroupDefinition(
            key=f"auto:statement:{f.statement}", title=label,
            retrieval_query=f"{label} statement", statement=f.statement,
        )
    query = " ".join(p for p in (f.name, f.description, " ".join(f.aliases)) if p)
    return GroupDefinition(key=f"auto:field:{f.field_id}", title=f.name,
                           retrieval_query=query[:_MAX_TEXT], locator_keywords=f.aliases)


def _canonical(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def definition_hash(f: FieldDefinition, group: GroupDefinition) -> str:
    """Hash of exactly what changes what the model reads for `f` (ADR-0004).
    Position, `required` and validation rules are judged after reading and
    are deliberately excluded."""
    body = {
        "name": f.name, "description": f.description, "data_type": f.data_type,
        "unit": f.unit, "instructions": f.instructions, "statement": f.statement,
        "tag": f.tag, "aliases": list(f.aliases), "note_aliases": list(f.note_aliases),
        "periods": list(f.periods), "allowed_values": list(f.allowed_values),
        "origin": f.origin,
        "derivation": None if f.derivation is None else {
            "method": f.derivation.method,
            "inputs": [[t, w] for t, w in f.derivation.inputs],
        },
        "group": {
            "title": group.title, "retrieval_query": group.retrieval_query,
            "locator_keywords": list(group.locator_keywords),
            "basis_preference": group.basis_preference,
        },
    }
    return _sha256(_canonical(body))


def schema_hash(fields: Sequence[FieldDefinition], groups: Sequence[GroupDefinition],
                review_threshold: float) -> str:
    """Hash of a whole definition, order included: what a version records,
    and how "the draft equals the latest version" is decided."""
    return _sha256(_canonical({
        "fields": [field_to_mapping(f) for f in fields],
        "groups": [group_to_mapping(g) for g in groups],
        "review_threshold": review_threshold,
    }))


def to_field_spec(f: FieldDefinition, group: GroupDefinition) -> FieldSpec:
    """The engine's view of an extracted Field. `field_path` (the key the
    model answers under) is the tag when there is one, else the Field id --
    both unique within a version."""
    keywords: list[str] = []
    for k in (*f.aliases, *group.locator_keywords):
        if k not in keywords:
            keywords.append(k)
    prompt = f.instructions
    if f.unit:
        prompt = f"{prompt} Unit: {f.unit}."
    if f.allowed_values:
        prompt = f"{prompt} Answer with one of: {', '.join(f.allowed_values)}."
    return FieldSpec(
        field_name=f.name, data_type=f.data_type, retrieval_query=group.retrieval_query,
        keywords=tuple(keywords), extraction_prompt=prompt,
        enum_values=f.allowed_values or None, module_id=-1,
        module_name=group.title, module_key=group.statement or "", unit_id=group.key,
        unit_title=group.title, statement_type=group.statement,
        field_path=f.tag or f.field_id, periods=f.periods,
        locator_keywords=group.locator_keywords, basis_preference=group.basis_preference,
        tag=f.tag or "", origin=f.origin, note_aliases=f.note_aliases,
    )


# --------------------------------------------------------------------------
# Version diff
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldChange:
    field_id: str
    name: str
    changed: tuple[str, ...]


@dataclass(frozen=True)
class FieldDiff:
    added: tuple[FieldDefinition, ...]
    removed: tuple[FieldDefinition, ...]
    changed: tuple[FieldChange, ...]


def diff_fields(old: Sequence[FieldDefinition], new: Sequence[FieldDefinition]) -> FieldDiff:
    """Fields added, removed and changed (which attributes) from `old` to
    `new`, matched by Field identity, in `new`'s order."""
    before = {f.field_id: field_to_mapping(f) for f in old}
    after_ids = {f.field_id for f in new}
    added: list[FieldDefinition] = []
    changed: list[FieldChange] = []
    for f in new:
        prev = before.get(f.field_id)
        if prev is None:
            added.append(f)
            continue
        cur = field_to_mapping(f)
        keys = tuple(sorted(k for k in cur if cur[k] != prev.get(k)))
        if keys:
            changed.append(FieldChange(field_id=f.field_id, name=f.name, changed=keys))
    removed = tuple(f for f in old if f.field_id not in after_ids)
    return FieldDiff(added=tuple(added), removed=removed, changed=tuple(changed))


# --------------------------------------------------------------------------
# The FDI standard schema (v4.3 document -> Fields)
# --------------------------------------------------------------------------


def standard_field_id(key: str) -> str:
    """Stable id of a standard Field: from its tag, or its path when it has
    none. Deterministic so a later standard version keeps the identity."""
    return str(uuid.uuid5(STANDARD_FIELD_NAMESPACE, key))


@dataclass(frozen=True)
class CompiledSchema:
    groups: tuple[GroupDefinition, ...]
    fields: tuple[FieldDefinition, ...]
    source_version: str


def _plain(value: object) -> object:
    """JSON-safe copy of standard-schema metadata kept under `extra`."""
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


_EXTRA_KEYS: Final = ("sr_no", "reconciliation", "classified_as", "reported",
                      "cash_flag", "waterfall", "sign_convention", "taxonomy_tag",
                      "legacy_tag", "role", "flow", "mirrors", "is_tax_effect")


def compile_standard_schema(doc: Mapping[str, object]) -> CompiledSchema:
    """The merged v4.3 standard document as groups + Fields.

    * one group per module retrieval unit (P&L, Assets, Liabilities, Equity,
      Cash Flow), queried "<Module> statement" -- the v4.2 precedent;
    * every TAGGED node becomes a Field with its own origin: a parent whose
      total is printed (origin `extracted`, e.g. `TOTEXPR`) is read, a
      `derived` node is computed from its `derivation` (never read);
    * an `extracted` parent's own `derivation` is a cross-check, not a
      formula: it is kept under `extra.reconciliation_inputs`;
    * a derived formula naming a tag no Field carries is emptied (see
      `_without_dangling`);
    * an untagged leaf is still a Field (identity from its path); an
      untagged, origin-less parent is only a container.
    Raises `SchemaInputError` when the document holds no Field at all."""
    groups: list[GroupDefinition] = []
    fields: list[FieldDefinition] = []
    modules = doc.get("modules")
    for m in modules if isinstance(modules, list) else []:
        if not isinstance(m, Mapping):
            continue
        module_name = str(m.get("name") or m.get("key") or "Statement")
        statement = m.get("statement")
        statement = statement if isinstance(statement, str) and statement else None
        units = m.get("retrieval_units")
        for u in units if isinstance(units, list) else []:
            if not isinstance(u, Mapping):
                continue
            key = str(u.get("unit_id") or module_name)
            basis = u.get("basis_preference")
            group = GroupDefinition(
                key=key, title=module_name,
                retrieval_query=str(u.get("retrieval_query") or f"{module_name} statement"),
                locator_keywords=tuple(
                    k for k in u.get("locator_keywords", []) if isinstance(k, str) and k
                ) if isinstance(u.get("locator_keywords"), list) else (),
                basis_preference=basis if basis in BASIS_PREFERENCES else "standalone",
                statement=statement,
            )
            groups.append(group)
            nodes = u.get("fields")
            _walk_standard(nodes if isinstance(nodes, list) else [], group=group,
                           module_name=module_name, path=key, out=fields)
    if not fields:
        raise SchemaInputError(["the standard schema document holds no Field"])
    version = doc.get("version")
    # Every Field must read back through the parser that loads stored rows:
    # a standard Field the API could not re-read would break at first use,
    # long after the import "succeeded". Refuse it here instead.
    errors: list[str] = []
    checked: list[FieldDefinition] = []
    for f in _without_dangling(fields):
        try:
            checked.append(field_from_mapping(field_to_mapping(f), field_id=f.field_id,
                                              where=f"field {f.tag or f.name!r}"))
        except SchemaInputError as exc:
            errors.extend(exc.errors)
    if errors:
        raise SchemaInputError(errors)
    return CompiledSchema(groups=tuple(groups), fields=tuple(checked),
                          source_version=version if isinstance(version, str) else "")


def _without_dangling(fields: Sequence[FieldDefinition]) -> list[FieldDefinition]:
    """A derived Field whose formula names a tag no Field carries cannot be
    computed. Summing the inputs that DO exist would print a confident wrong
    total, so its inputs are emptied (never computable) and the missing tags
    are kept under `extra.unresolved_inputs` for whoever fixes the source."""
    tags = {f.tag for f in fields if f.tag}
    out: list[FieldDefinition] = []
    for f in fields:
        if f.derivation is not None:
            missing = [t for t, _w in f.derivation.inputs if t not in tags]
            if missing:
                f = FieldDefinition(**{
                    **f.__dict__,
                    "derivation": Derivation(method=f.derivation.method, inputs=()),
                    "extra": {**f.extra, "unresolved_inputs": missing},
                })
        out.append(f)
    return out


def _walk_standard(nodes: Sequence[object], *, group: GroupDefinition, module_name: str,
                   path: str, out: list[FieldDefinition]) -> None:
    for i, node in enumerate(nodes):
        if not isinstance(node, Mapping):
            continue
        name = node.get("field_name")
        name = name.strip() if isinstance(name, str) else ""
        tag = node.get("tag")
        tag = tag if isinstance(tag, str) and tag else None
        children = node.get("fields")
        has_children = isinstance(children, list) and bool(children)
        origin = node.get("origin")
        node_path = f"{path}/{tag or name or i}"
        is_container = has_children and tag is None and origin not in ORIGINS
        if name and not is_container:
            out.append(_standard_field(node, name=name, tag=tag, path=node_path,
                                       group=group, module_name=module_name))
        if has_children:
            _walk_standard(children, group=group, module_name=module_name,  # type: ignore[arg-type]
                           path=node_path, out=out)


def _standard_field(node: Mapping[str, object], *, name: str, tag: str | None, path: str,
                    group: GroupDefinition, module_name: str) -> FieldDefinition:
    origin = "derived" if node.get("origin") == "derived" else "extracted"
    description = node.get("definition")
    description = description.strip() if isinstance(description, str) else ""
    data_type = node.get("data_type")
    data_type = data_type if data_type in DATA_TYPES else "decimal"
    extra: dict[str, object] = {k: _plain(node[k]) for k in _EXTRA_KEYS if k in node}
    derivation: Derivation | None = None
    raw_derivation = node.get("derivation")
    if isinstance(raw_derivation, Mapping):
        parsed_errors: list[str] = []
        parsed = _derivation({"derivation": raw_derivation}, "derivation", parsed_errors)
        if origin == "derived":
            derivation = parsed
        elif parsed is not None:
            extra["reconciliation_inputs"] = [
                {"tag": t, "weight": w} for t, w in parsed.inputs
            ]
    if origin == "derived" and derivation is None:
        derivation = Derivation(method="sum", inputs=())
    instructions = ""
    if origin == "extracted":
        instructions = (
            f"Extract the value of '{name}' from the {module_name} statement. Report "
            "the current reporting-year figure exactly as printed in the document's "
            "own reporting unit; return null if the line item is absent."
        )
        if description:
            instructions = f"{instructions} Meaning: {description}"
    aliases = node.get("aliases")
    note_aliases = node.get("note_aliases")
    return FieldDefinition(
        field_id=standard_field_id(tag or path), name=name, description=description,
        data_type=str(data_type), instructions=instructions, statement=group.statement,
        group=group.key, tag=tag,
        aliases=tuple(a for a in aliases if isinstance(a, str) and a)
        if isinstance(aliases, list) else (),
        note_aliases=tuple(a for a in note_aliases if isinstance(a, str) and a)
        if isinstance(note_aliases, list) else (),
        origin=origin, derivation=derivation, extra=extra,
    )
