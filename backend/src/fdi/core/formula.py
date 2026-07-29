"""Pure parsing of a subtotal's calculation formula out of its Particulars text.

The taxonomy states each subtotal's arithmetic inside its label, e.g.
"Total Gross Revenue (Total of 1.1 to 1.5)" or "Net Revenue (1 minus 2)". This
turns that human text into a structured formula the derivation engine resolves
to tags. No I/O; total (returns None when nothing parses, never raises).

An operand is an sr_no reference -- either a single number `("single", (4, 1))`
or an inclusive range `("range", (1, 1), (1, 5))`. A formula is a signed sum
(`op="sum"` over operands, all added) or a binary difference (`op="diff"`,
left - right).
"""

import re
from typing import Final

_SR: Final = re.compile(r"\d+(?:\.\d+)*")
_MINUS: Final = re.compile(r"\bminus\b", re.IGNORECASE)
_PLUSMINUS: Final = re.compile(r"plus\s*/\s*minus|minus\s*/\s*plus", re.IGNORECASE)


def parse_sr(text: str) -> tuple[int, ...] | None:
    """"1.1" -> (1, 1); "3" -> (3,); anything else -> None."""
    text = text.strip()
    if not re.fullmatch(r"\d+(?:\.\d+)*", text):
        return None
    return tuple(int(p) for p in text.split("."))


def _reference_span(particulars: str) -> str | None:
    """The bit that states the arithmetic: prefer the LAST parenthesised group
    (that is where the taxonomy puts "Total of ..."/"a minus b"); else the tail
    after 'total of'. None when neither is present."""
    groups: list[str] = re.findall(r"\(([^()]*)\)", particulars)
    for g in reversed(groups):
        if "to" in g.lower() or "minus" in g.lower() or "&" in g or _SR.search(g):
            return g
    low = particulars.lower()
    idx = low.rfind("total of")
    if idx != -1:
        return particulars[idx + len("total of"):]
    return None


def _parse_operand(segment: str) -> tuple[object, ...] | None:
    """One '&'-separated segment: "1.1 to 1.5" -> range; "3" -> single."""
    parts = re.split(r"\bto\b", segment, flags=re.IGNORECASE)
    if len(parts) == 2:
        lo = parse_sr(parts[0])
        hi = parse_sr(parts[1])
        if lo is not None and hi is not None and len(lo) == len(hi) and lo <= hi:
            return ("range", lo, hi)
        return None
    sr = parse_sr(segment)
    return ("single", sr) if sr is not None else None


def parse_formula(particulars: str) -> dict[str, object] | None:
    """Structured formula for a subtotal label, or None.

    * "... (a minus b)"          -> {"op": "diff", "left": <op>, "right": <op>}
    * "... (Total of 1.1 to 1.5)"-> {"op": "sum", "operands": [<op>, ...]}
    * "plus / minus" (ambiguous sign) -> None, deliberately: a bank product must
      not guess a sign.
    """
    if not isinstance(particulars, str) or not particulars:
        return None
    if _PLUSMINUS.search(particulars):
        return None

    span = _reference_span(particulars)
    if span is None:
        return None
    span = re.sub(r"(?i)total\s+of", " ", span)

    if _MINUS.search(span):
        left_txt, right_txt = _MINUS.split(span, maxsplit=1)
        left = _parse_operand(left_txt.strip(" ()"))
        right = _parse_operand(right_txt.strip(" ()"))
        if left is not None and right is not None:
            return {"op": "diff", "left": left, "right": right}
        return None

    operands = []
    for segment in span.split("&"):
        segment = segment.strip()
        if not segment:
            continue
        op = _parse_operand(segment)
        if op is not None:
            operands.append(op)
    return {"op": "sum", "operands": operands} if operands else None


def sr_in_range(sr: tuple[int, ...], lo: tuple[int, ...], hi: tuple[int, ...]) -> bool:
    """True when `sr` sits inside the inclusive range `[lo, hi]` at the SAME
    depth -- "1.1 to 1.5" covers (1,1)..(1,5) but not (1,1,2) or (2,)."""
    return len(sr) == len(lo) and lo <= sr <= hi


def operand_matches(operand: tuple[object, ...], sr: tuple[int, ...]) -> bool:
    """Whether an sr_no belongs to one operand (single or range)."""
    if operand[0] == "single":
        return sr == operand[1]
    if operand[0] == "range":
        lo, hi = operand[1], operand[2]
        if isinstance(lo, tuple) and isinstance(hi, tuple):
            return sr_in_range(sr, lo, hi)
    return False
