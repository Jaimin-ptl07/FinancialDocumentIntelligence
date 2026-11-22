"""The answer-quality benchmark: hand-keyed questions with gold answers.

The benchmark is the retrieval/agent counterpart of `docs/groundtruth/`: it is
truth, never generated from pipeline or agent output. (`fact_lookup` items ARE
generated -- mechanically, from the hand-keyed ground truth, by
`fact_lookup_benchmark` below -- so they carry no more opinion than the ground
truth itself.) A benchmark file holds
one item kind; an item is `id`, `kind`, `question`, an optional scope
(`doc_keys`) and `gold`.

Each kind names the fields it cannot be scored without (`_check_kind`):
`fact_lookup`, `cross_company`, `calculation` and `multi_year` are answerable
with at least one gold value; `cross_company` also needs two or more
`doc_keys`, one per Company compared; `calculation` records the hand
`gold.formula` its gold was derived by; `research` is answerable with at
least one gold evidence location (the note or narrative page the answer is
printed on) and at least one `gold.required_claims`; `unanswerable` has no
gold values, no gold evidence and no required claims.

A malformed item is REJECTED at load with a precise reason. `parse_groundtruth`
skips a bad file instead, but there the cost is a smaller denominator that the
report still shows; here a typo in a gold value would silently score a correct
answer as a miss, and nothing downstream could tell the two apart.

Pure (`fdi.core` law): no I/O, deterministic.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from fdi.core.scoring import parse_groundtruth

KINDS = frozenset({
    "fact_lookup", "cross_company", "calculation", "research", "unanswerable", "multi_year",
})

# Gold units, in rupees per unit. "INR" is a per-share figure (EPS).
UNIT_SCALES: Mapping[str, float] = {"INR": 1.0, "INR lakh": 1e5, "INR crore": 1e7}
# Dimensionless gold, for `calculation` items: a margin or rate in per cent
# (18.22 means 18.22%), and a simple ratio as a bare number (2.23 times).
PERCENT = "percent"
RATIO = "ratio"
UNITS = frozenset({*UNIT_SCALES, PERCENT, RATIO})


class BenchmarkError(ValueError):
    """A benchmark file or item that cannot be scored, and exactly why."""


@dataclass(frozen=True)
class GoldValue:
    """A figure the answer must state, in `unit` (e.g. "INR crore"), matched
    within an absolute `tolerance` expressed in that unit."""

    value: float
    unit: str
    tolerance: float


@dataclass(frozen=True)
class Evidence:
    """Where the gold answer is printed: a Report and a 1-based PDF page."""

    doc_key: str
    page: int


@dataclass(frozen=True)
class Gold:
    """`expected_tools` names the answering endpoint's tools a good plan
    calls for this item; None (not empty) when the item does not say, so
    tool selection is not scored for it. The names are not checked against
    `/ask`'s tool registry: that lives outside `fdi.core`, and a later
    endpoint (S7) may name its tools differently.

    `formula` is how a `calculation` item's gold values were derived by
    hand from ground-truth values, so the arithmetic can be re-checked.

    `required_claims` are the free-text statements a complete `research`
    answer must make. Only the optional LLM judge (`fdi.core.judge`) scores
    them; the deterministic scorer never reads them."""

    answerable: bool
    values: tuple[GoldValue, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    expected_tools: tuple[str, ...] | None = None
    formula: str | None = None
    required_claims: tuple[str, ...] = ()


@dataclass(frozen=True)
class BenchmarkItem:
    id: str
    kind: str
    question: str
    gold: Gold
    doc_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class Benchmark:
    version: str
    items: tuple[BenchmarkItem, ...]


def parse_benchmark(body: Mapping[str, object]) -> Benchmark:
    """A benchmark file body as a `Benchmark`.

    Raises `BenchmarkError` naming the first malformed item (by id, or by
    position when it has none) and what is wrong with it."""
    if not isinstance(body, Mapping):
        raise BenchmarkError("benchmark must be a JSON object")
    version = body.get("version")
    if not _text(version):
        raise BenchmarkError("benchmark 'version' must be a non-empty string")
    raw_items = body.get("items")
    if not isinstance(raw_items, list):
        raise BenchmarkError("benchmark 'items' must be a list")

    items: list[BenchmarkItem] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_items):
        if not isinstance(raw, Mapping):
            raise BenchmarkError(f"item #{index}: must be a JSON object")
        item_id = raw.get("id")
        if not _text(item_id):
            raise BenchmarkError(f"item #{index}: 'id' must be a non-empty string")
        assert isinstance(item_id, str)
        if item_id in seen:
            raise BenchmarkError(f"item {item_id!r}: duplicate id")
        seen.add(item_id)
        try:
            items.append(_parse_item(item_id, raw))
        except BenchmarkError as exc:
            raise BenchmarkError(f"item {item_id!r}: {exc}") from None
    return Benchmark(version=str(version), items=tuple(items))


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _number(value: object) -> bool:
    # `bool` is an `int` subclass; True would otherwise read as 1.0.
    return isinstance(value, int | float) and not isinstance(value, bool)


def printed_precision(value: float) -> float:
    """Half a unit of the last decimal place `value` is printed to: 7.55 ->
    0.005, 255324.0 -> 0.5. The default tolerance of a gold value."""
    text = repr(float(value))
    decimals = 0 if text.endswith(".0") or "." not in text else len(text.split(".")[1])
    return 0.5 * 10.0 ** -decimals


def _parse_item(item_id: str, raw: Mapping[str, object]) -> BenchmarkItem:
    kind = raw.get("kind")
    if kind not in KINDS:
        raise BenchmarkError(f"unknown kind {kind!r}")
    assert isinstance(kind, str)
    question = raw.get("question")
    if not _text(question):
        raise BenchmarkError("'question' must be a non-empty string")
    doc_keys = raw.get("doc_keys", [])
    if not isinstance(doc_keys, list) or not all(_text(k) for k in doc_keys):
        raise BenchmarkError("'doc_keys' must be a list of non-empty strings")

    gold = _parse_gold(raw.get("gold"))
    _check_kind(kind, gold, doc_keys)
    return BenchmarkItem(
        id=item_id, kind=kind, question=str(question), gold=gold,
        doc_keys=tuple(str(k) for k in doc_keys),
    )


# Kinds whose gold is one or more figures the answer must state.
_VALUED_KINDS = frozenset({"fact_lookup", "cross_company", "calculation", "multi_year"})


def _check_kind(kind: str, gold: Gold, doc_keys: Sequence[object]) -> None:
    """The fields `kind` cannot be scored without."""
    if kind in _VALUED_KINDS:
        if not gold.answerable:
            raise BenchmarkError(f"a {kind} item must be answerable")
        if not gold.values:
            raise BenchmarkError(f"a {kind} item needs at least one gold value")
    if kind == "cross_company" and len(set(doc_keys)) < 2:
        raise BenchmarkError(
            "a cross_company item needs at least two doc_keys (one per Company compared)"
        )
    if kind == "calculation" and gold.formula is None:
        # Without it nobody can re-check the hand arithmetic the gold rests on.
        raise BenchmarkError(
            "a calculation item must record the 'gold.formula' its gold value was derived by"
        )
    if kind == "research":
        if not gold.answerable:
            raise BenchmarkError("a research item must be answerable")
        if not gold.evidence:
            # Evidence location is the deterministic half of its score.
            raise BenchmarkError("a research item needs at least one gold evidence location")
        if not gold.required_claims:
            raise BenchmarkError("a research item needs at least one required claim")
    if kind == "unanswerable":
        if gold.answerable:
            raise BenchmarkError("an unanswerable item must have 'gold.answerable' false")
        if gold.values:
            raise BenchmarkError("an unanswerable item has no gold values")
        if gold.evidence:
            raise BenchmarkError("an unanswerable item has no gold evidence")
        if gold.required_claims:
            raise BenchmarkError("an unanswerable item has no required claims")


def _parse_gold(raw: object) -> Gold:
    if not isinstance(raw, Mapping):
        raise BenchmarkError("'gold' must be a JSON object")
    answerable = raw.get("answerable")
    if not isinstance(answerable, bool):
        raise BenchmarkError("'gold.answerable' must be true or false")
    raw_values = raw.get("values", [])
    raw_evidence = raw.get("evidence", [])
    if not isinstance(raw_values, list):
        raise BenchmarkError("'gold.values' must be a list")
    if not isinstance(raw_evidence, list):
        raise BenchmarkError("'gold.evidence' must be a list")
    formula = raw.get("formula")
    if formula is not None and not _text(formula):
        raise BenchmarkError("'gold.formula' must be a non-empty string")
    return Gold(
        answerable=answerable,
        values=tuple(_parse_value(i, v) for i, v in enumerate(raw_values)),
        evidence=tuple(_parse_evidence(i, e) for i, e in enumerate(raw_evidence)),
        expected_tools=_parse_expected_tools(raw.get("expected_tools")),
        formula=None if formula is None else str(formula),
        required_claims=_parse_required_claims(raw.get("required_claims")),
    )


def _parse_required_claims(raw: object) -> tuple[str, ...]:
    """Absent is (). Present, a non-empty list of distinct claims: an empty
    list would make every answer fully supported, and a repeated claim
    would count twice in the judge's support rate."""
    if raw is None:
        return ()
    where = "'gold.required_claims'"
    if not isinstance(raw, list) or not raw:
        raise BenchmarkError(f"{where} must be a non-empty list of claims")
    claims: list[str] = []
    for index, claim in enumerate(raw):
        if not _text(claim):
            raise BenchmarkError(f"{where} #{index} must be a non-empty string")
        assert isinstance(claim, str)
        if claim in claims:
            raise BenchmarkError(f"{where} states {claim!r} twice")
        claims.append(claim)
    return tuple(claims)


def _parse_expected_tools(raw: object) -> tuple[str, ...] | None:
    """Absent is None. Present, it is a set of tool names written as a list:
    an empty list would score every plan as perfect, and a repeated name
    suggests a typo for a different tool."""
    if raw is None:
        return None
    where = "'gold.expected_tools'"
    if not isinstance(raw, list) or not raw:
        raise BenchmarkError(f"{where} must be a non-empty list of tool names")
    names: list[str] = []
    for index, name in enumerate(raw):
        if not _text(name):
            raise BenchmarkError(f"{where} #{index} must be a non-empty string")
        assert isinstance(name, str)
        if name in names:
            raise BenchmarkError(f"{where} names {name!r} twice")
        names.append(name)
    return tuple(names)


def _parse_value(index: int, raw: object) -> GoldValue:
    where = f"gold value #{index}"
    if not isinstance(raw, Mapping):
        raise BenchmarkError(f"{where} must be a JSON object")
    value = raw.get("value")
    if not _number(value):
        raise BenchmarkError(f"{where} 'value' must be a number")
    assert isinstance(value, int | float)
    unit = raw.get("unit")
    if unit not in UNITS:
        known = ", ".join(sorted(UNITS))
        raise BenchmarkError(f"{where} unknown unit {unit!r} (expected one of {known})")
    tolerance = raw.get("tolerance", printed_precision(value))
    if not _number(tolerance) or not isinstance(tolerance, int | float) or tolerance < 0:
        raise BenchmarkError(f"{where} 'tolerance' must be a number >= 0")
    return GoldValue(value=float(value), unit=str(unit), tolerance=float(tolerance))


def _parse_evidence(index: int, raw: object) -> Evidence:
    where = f"gold evidence #{index}"
    if not isinstance(raw, Mapping):
        raise BenchmarkError(f"{where} must be a JSON object")
    doc_key = raw.get("doc_key")
    if not _text(doc_key):
        raise BenchmarkError(f"{where} 'doc_key' must be a non-empty string")
    page = raw.get("page")
    if not isinstance(page, int) or isinstance(page, bool) or page < 1:
        raise BenchmarkError(f"{where} 'page' must be an integer >= 1")
    return Evidence(doc_key=str(doc_key), page=page)


# --------------------------------------------------------------------------
# fact_lookup generation: one item per ground-truth value, never typed.
# --------------------------------------------------------------------------

# Ground truth uses the real entity name; `doc_key`s predate that. REMC is a
# joint venture of Indian Railways and RITES Limited, NOT RITES -- its
# doc_key is historical and misnamed (docs/groundtruth/README.md).
ISSUER_DOC_KEYS: Mapping[str, str] = {
    "TCS": "tcs-2025-26",
    "RIL": "ril-2025-26",
    "REMC": "rites-2024-25",
}

# Per-share figures are printed in rupees, whatever unit the statement's
# header declares for its amounts.
PER_SHARE_TAGS = frozenset({"EPSB", "EPSD"})

_PAGE = re.compile(r"PDF page (\d+)")
_UNIT = re.compile(r"\bINR\s+(crore|lakh)\b", re.IGNORECASE)
# Trailing label noise in a question: the schema's sheet cross-reference
# ("(11 plus/minus 12 & 13)") and an "(as reported...)" qualifier the
# question already states.
_LABEL_NOISE = re.compile(r"\s*\((?:[^)]*\d[^)]*|as reported[^)]*)\)\s*$", re.IGNORECASE)


def fact_lookup_benchmark(
    version: str,
    groundtruth: Sequence[Mapping[str, object]],
    labels: Mapping[str, str],
) -> dict[str, object]:
    """A `fact_lookup` benchmark file body: one item per `values` tag of each
    ground-truth body (`docs/groundtruth/*.json`), labelled from the
    standard schema (`labels`: tag -> field name).

    Tags in `not_reported` and tags in neither list make no item -- exactly
    the tags `score_document` would not score as values. Items are sorted by
    id, so the output does not depend on the order files were read in.
    Raises `BenchmarkError` for ground truth that cannot make an item."""
    items: list[dict[str, object]] = []
    for index, body in enumerate(groundtruth):
        gt = parse_groundtruth(body)
        if gt is None:
            raise BenchmarkError(
                f"ground truth #{index} does not parse "
                "(see fdi.core.scoring.parse_groundtruth)"
            )
        stem = f"{gt.issuer}-{gt.fiscal_year}-{gt.basis}-{gt.statement}"
        where = f"ground truth {stem}"
        doc_key = ISSUER_DOC_KEYS.get(gt.issuer)
        if doc_key is None:
            raise BenchmarkError(f"{where}: no doc_key for issuer {gt.issuer!r}")
        page = _PAGE.search(str(body.get("_source", "")))
        if page is None:
            raise BenchmarkError(f"{where}: '_source' names no PDF page")
        unit = _UNIT.search(str(body.get("_unit", "")))
        if unit is None:
            raise BenchmarkError(f"{where}: '_unit' names no INR unit")
        basis = "" if gt.basis == "unknown" else f"{gt.basis} "

        for tag, value in gt.values.items():
            label = labels.get(tag)
            if not label:
                raise BenchmarkError(f"{where}: tag {tag!r} has no label in the schema")
            label = _LABEL_NOISE.sub("", label)
            items.append({
                "id": f"fact_lookup:{stem}:{tag}",
                "kind": "fact_lookup",
                "question": (
                    f"What was {gt.issuer}'s {basis}{label} for {gt.fiscal_year}, as reported?"
                ),
                "doc_keys": [doc_key],
                "gold": {
                    "answerable": True,
                    "values": [{
                        "value": value,
                        "unit": "INR" if tag in PER_SHARE_TAGS else f"INR {unit[1].lower()}",
                        "tolerance": printed_precision(value),
                    }],
                    "evidence": [{"doc_key": doc_key, "page": int(page[1])}],
                },
            })
    items.sort(key=lambda item: str(item["id"]))
    return {"version": version, "kind": "fact_lookup", "items": items}
