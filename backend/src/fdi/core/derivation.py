"""Pure computation of derived Facts (the `do not tag` subtotals) from extracted
values, using each subtotal's own formula (fdi.core.formula) resolved to sibling
tags by sr_no within the same statement block. No I/O; total.

Accuracy-first policy: a subtotal is computed ONLY when its formula parses AND
every operand resolves to a tag that has a value. Otherwise the derived Fact is
emitted with a `status` of "unresolved" (no formula / no matching tags) or
"missing_inputs" (operands resolved but some input value absent) and NO value.
A bank product must never publish a guessed number.

`values` maps tag -> {"value": float|None, "confidence": float}. Derivation runs
to a fixpoint so a subtotal built on other subtotals resolves once its inputs do.
Confidence propagates as the MIN of input confidences.
"""

from collections.abc import Mapping, Sequence

from fdi.core.formula import operand_matches, parse_formula, parse_sr

_SrTuple = tuple[int, ...]
_BlockMap = Mapping[_SrTuple, str]
_Fact = dict[str, object]


def _block_index(rows: Sequence[Mapping[str, object]]) -> dict[str, dict[_SrTuple, str]]:
    """block_id -> {sr_tuple: tag} over every row that has both a tag and a
    parseable sr_no. First writer wins on a collision (stable)."""
    index: dict[str, dict[_SrTuple, str]] = {}
    for r in rows:
        tag = r.get("tag")
        sr_raw = r.get("sr_no")
        block = r.get("block_id")
        if not isinstance(tag, str) or not tag or not isinstance(block, str):
            continue
        sr = parse_sr(sr_raw) if isinstance(sr_raw, str) else None
        if sr is None:
            continue
        index.setdefault(block, {}).setdefault(sr, tag)
    return index


def _resolve_operand(operand: tuple[object, ...], block_map: _BlockMap) -> list[str]:
    return [tag for sr, tag in block_map.items() if operand_matches(operand, sr)]


def _plan(row: Mapping[str, object], block_map: _BlockMap) -> dict[str, object] | None:
    """Resolve a derived row's formula to concrete input tags, or None when the
    label has no parseable formula."""
    particulars = row.get("particulars")
    if not isinstance(particulars, str):
        return None
    formula = parse_formula(particulars)
    if formula is None:
        return None
    if formula["op"] == "sum":
        inputs: list[str] = []
        for operand in formula["operands"]:  # type: ignore[attr-defined]
            inputs.extend(_resolve_operand(operand, block_map))
        return {"op": "sum", "inputs": inputs, "signs": dict.fromkeys(inputs, 1)}
    left = _resolve_operand(formula["left"], block_map)  # type: ignore[arg-type]
    right = _resolve_operand(formula["right"], block_map)  # type: ignore[arg-type]
    signs = dict.fromkeys(left, 1)
    signs.update(dict.fromkeys(right, -1))
    return {"op": "diff", "inputs": [*left, *right], "signs": signs,
            "left": left, "right": right}


def _explain(tag: str, op: str, inputs: list[str], left: list[str], right: list[str]) -> str:
    if op == "diff":
        lhs = " + ".join(left) or "?"
        rhs = " + ".join(right) or "?"
        return f"{tag} = ({lhs}) minus ({rhs})"
    return f"{tag} = sum of {', '.join(inputs)}" if inputs else f"{tag} = (no inputs resolved)"


def _number(v: object) -> float | None:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    return None


def _str_list(v: object) -> list[str]:
    return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []


def derive_facts(
    statements: Sequence[Mapping[str, object]],
    values: Mapping[str, Mapping[str, object]],
) -> dict[str, _Fact]:
    """Compute every derivable subtotal. Returns {tag: DerivedFact}.

    A DerivedFact is
      {tag, value, origin: "derived", confidence, status,
       derivation: {method, inputs, explanation}}.
    """
    rows: list[Mapping[str, object]] = []
    for st in statements:
        rows_obj = st.get("rows")
        if isinstance(rows_obj, list):
            rows.extend(r for r in rows_obj if isinstance(r, Mapping))

    index = _block_index(rows)

    plans: dict[str, dict[str, object]] = {}
    order: list[str] = []
    for r in rows:
        if r.get("origin") != "derived":
            continue
        tag = r.get("tag")
        block = r.get("block_id")
        if not isinstance(tag, str) or not tag or tag in plans:
            continue
        block_map = index.get(block, {}) if isinstance(block, str) else {}
        plan = _plan(r, block_map)
        plans[tag] = plan if plan is not None else {"op": "none", "inputs": []}
        order.append(tag)

    work: dict[str, dict[str, object]] = {
        t: dict(v) for t, v in values.items() if isinstance(v, Mapping)
    }
    facts: dict[str, _Fact] = {}

    def _value(tag: str) -> tuple[float, float] | None:
        v = work.get(tag)
        if not isinstance(v, Mapping):
            return None
        val = _number(v.get("value"))
        if val is None:
            return None
        conf = _number(v.get("confidence"))
        return val, conf if conf is not None else 0.0

    for _ in range(len(order) + 1):
        progressed = False
        for tag in order:
            existing = facts.get(tag)
            if existing is not None and existing["status"] == "ok":
                continue
            plan = plans[tag]
            inputs = _str_list(plan["inputs"])
            if plan["op"] == "none" or not inputs:
                facts[tag] = _unresolved(tag)
                continue
            signs: Mapping[str, int] = plan["signs"]  # type: ignore[assignment]
            resolved = [(_value(t), signs[t]) for t in inputs]
            if any(rv is None for rv, _ in resolved):
                facts[tag] = _missing(tag, plan, inputs)
                continue
            total = sum(sign * rv[0] for rv, sign in resolved if rv is not None)
            conf = min(rv[1] for rv, _ in resolved if rv is not None)
            facts[tag] = {
                "tag": tag, "value": total, "origin": "derived", "confidence": conf,
                "status": "ok",
                "derivation": {
                    "method": plan["op"], "inputs": inputs,
                    "explanation": _explain(
                        tag, str(plan["op"]), inputs,
                        _str_list(plan.get("left")), _str_list(plan.get("right")),
                    ),
                },
            }
            work[tag] = {"value": total, "confidence": conf}
            progressed = True
        if not progressed:
            break
    return facts


def _unresolved(tag: str) -> _Fact:
    return {
        "tag": tag, "value": None, "origin": "derived", "confidence": 0.0,
        "status": "unresolved",
        "derivation": {"method": "none", "inputs": [],
                       "explanation": f"{tag}: no resolvable formula"},
    }


def _missing(tag: str, plan: Mapping[str, object], inputs: Sequence[str]) -> _Fact:
    return {
        "tag": tag, "value": None, "origin": "derived", "confidence": 0.0,
        "status": "missing_inputs",
        "derivation": {
            "method": plan["op"], "inputs": list(inputs),
            "explanation": _explain(
                tag, str(plan["op"]), list(inputs),
                _str_list(plan.get("left")), _str_list(plan.get("right")),
            ),
        },
    }
