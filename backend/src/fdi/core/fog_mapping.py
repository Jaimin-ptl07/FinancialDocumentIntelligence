"""Map a FOG LineItem's own label onto a taxonomy concept.

THE INVERSION
-------------
Extraction asks a PULL question -- *what is the value of `GRSPNE`?* -- and the
model hunts the document for our label. Measured across three issuers, alias
matching alone answered 69% / 58% / 30% of face rows; the 30% is what a
TCS-shaped alias list does to an unrelated filer.

This asks the PUSH question instead: *here is Note 13(b) and its own lines --
which concept is each?* Three consequences: unseen vocabulary stops being fatal,
a line with no taxonomy home surfaces as `unmapped` instead of vanishing, and
the note's own total becomes the arbiter for its components.

The mapper assigns CONCEPTS ONLY. Every value already sits on a LineItem,
parsed by `fdi.core.numbers.parse_amount`; letting the model restate one
reintroduces the comma bug this codebase already paid for.

Pure (`fdi.core` law): no I/O, deterministic, total.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from fdi.core.concepts import ConceptCandidate

_UNMAPPED = {"unmapped", "none", "null", "n/a", "-", ""}


@dataclass(frozen=True)
class MapUnit:
    """One statement face or one note, with the labels and concepts in play.

    `kind` is "face" or "note", and it decides the candidate set: face rows are
    SUMMARIES and match parent concepts, note rows are DETAIL and match
    descendants. Conflating the two left 15 of TCS's 28 face rows unmapped."""

    unit_id: str
    kind: str
    title: str
    labels: tuple[str, ...]
    candidates: tuple[ConceptCandidate, ...]


_SYSTEM = """\
You map line items from an Indian company's financial statements onto a fixed \
list of accounting concepts.

Rules:
- For each LABEL, answer with the TAG of the single concept it represents.
- Use ONLY the tags listed. Never invent a tag, and never use one from \
elsewhere in the taxonomy.
- If a label has no good match in the list, answer "unmapped". Guessing is \
worse than admitting the gap.
- Each tag may be used at most ONCE. If two labels seem to fit one tag, keep \
the better fit and mark the other "unmapped".
- Do NOT report any value, amount or number. Values are already extracted; you \
are assigning concepts only.
- Subtotal and total rows usually map to a parent concept, not a component.

Answer with a JSON object mapping each label to a tag or to "unmapped".\
"""


def build_map_prompt(unit: MapUnit) -> tuple[str, str]:
    """`(system, user)` for one unit. The system half is shared and versioned;
    the user half is this unit's labels and its candidate concepts."""
    lines = [
        f"UNIT: {unit.unit_id} ({unit.kind})",
        f"TITLE: {unit.title}",
        "",
        "CONCEPTS (tag - name - aliases):",
    ]
    for c in unit.candidates:
        aliases = ", ".join(c.aliases) if c.aliases else "-"
        lines.append(f"  {c.tag} - {c.name} - {aliases}")
    lines += ["", "LABELS to map:"]
    lines += [f"  {label}" for label in unit.labels]
    return _SYSTEM, "\n".join(lines)


def read_map_reply(reply: object, unit: MapUnit) -> dict[str, str]:
    """`{label: tag}` for the assignments this unit actually authorises.

    Four filters, each closing a way the answer could be unauditable:
    a label the unit never held, a tag it never offered, an explicit
    `unmapped`, and a second claim on an already-taken tag. Total: never
    raises."""
    if not isinstance(reply, Mapping):
        return {}
    offered = {c.tag for c in unit.candidates}
    held = set(unit.labels)

    out: dict[str, str] = {}
    taken: set[str] = set()
    # Iterate the UNIT's order, not the reply's: two labels racing for one tag
    # must resolve the same way on every run or the artifact stops hashing
    # stably.
    for label in unit.labels:
        if label not in held:  # pragma: no cover - held is built from labels
            continue
        raw = reply.get(label)
        if not isinstance(raw, str):
            continue
        tag = raw.strip()
        if tag.lower() in _UNMAPPED or tag not in offered or tag in taken:
            continue
        out[label] = tag
        taken.add(tag)
    return out


def unmapped_labels(unit: MapUnit, resolved: Mapping[str, str]) -> list[str]:
    """The unit's labels that nothing resolved, in unit order.

    These are the point of the push model: "Facility expenses 2,966" has no
    taxonomy home, and it must stay VISIBLE rather than vanish."""
    return [label for label in unit.labels if label not in resolved]


def candidates_for_kind(
    kind: str,
    face_candidates: Sequence[ConceptCandidate],
    note_candidates: Sequence[ConceptCandidate],
) -> tuple[ConceptCandidate, ...]:
    """Face rows match PARENTS, note rows match DESCENDANTS."""
    return tuple(face_candidates) if kind == "face" else tuple(note_candidates)
