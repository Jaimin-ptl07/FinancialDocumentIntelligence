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

from fdi.core.concepts import MAX_CANDIDATES, ConceptCandidate
from fdi.core.fog import Edge
from fdi.core.mapping import lexical_match
from fdi.core.memory import MemoryKey, MemoryStore, recall

_UNMAPPED = {"unmapped", "none", "null", "n/a", "-", ""}


@dataclass(frozen=True)
class MapUnit:
    """One statement face or one note, with the labels and concepts in play.

    `kind` is "face" or "note", and it decides the candidate set: face rows are
    SUMMARIES and match parent concepts, note rows are DETAIL and match
    descendants. Conflating the two left 15 of TCS's 28 face rows unmapped.

    `basis` ("consolidated" | "standalone" | ...) is carried so memory lookups
    can be scoped by it -- see `resolve_from_memory`'s docstring for why a
    label alone is not a safe key."""

    unit_id: str
    kind: str
    title: str
    labels: tuple[str, ...]
    candidates: tuple[ConceptCandidate, ...]
    kinds: tuple[str, ...] = ()
    basis: str = ""
    sections: tuple[tuple[str, ...], ...] = ()


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


def _ranked_candidates(unit: MapUnit) -> tuple[ConceptCandidate, ...]:
    """`unit.candidates`, best-affinity-first for the labels being asked.

    Ties keep declaration order (`sorted` is stable), so the result is
    deterministic -- this feeds a prompt whose answer reaches an artifact."""
    if not unit.labels:
        return unit.candidates

    def affinity(candidate: ConceptCandidate) -> float:
        best = 0.0
        for label in unit.labels:
            _hit, _method, score = lexical_match(label, (candidate,))
            best = max(best, score)
        return best

    return tuple(sorted(unit.candidates, key=affinity, reverse=True))


def build_map_prompt(unit: MapUnit) -> tuple[str, str, int]:
    """`(system, user, dropped)` for one unit. The system half is shared and
    versioned; the user half is this unit's labels and its candidate concepts.

    Candidates are capped at `MAX_CANDIDATES` (fdi.core.concepts): an
    uncapped note-candidate set MEASURED at ~519 concepts, ~12.3k tokens/unit,
    ~INR 218/document -- `build_map_prompt` was the one place in the mapper
    that bypassed the cap every other candidate-set builder already respects.
    `dropped` is how many candidates the cap cut, so a caller can log it
    rather than let the truncation pass as "considered everything".

    WHICH candidates survive is the whole game. Truncating in schema
    DECLARATION order keeps an arbitrary 40 of ~519 and leaves the model
    answering from a set that is 92% blind -- the right concept is almost
    never in it, and a confidently wrong answer from a blind shortlist is
    worse than the cost the cap was saving. Candidates are therefore
    ranked by their best lexical affinity to any label this unit is
    actually asking about, and the cap keeps the strongest.

    This tier only ever sees labels the FREE tiers could not settle, so a
    high affinity here means "close but under the floor" -- exactly the
    population a judgement is worth paying for."""
    kept = _ranked_candidates(unit)[:MAX_CANDIDATES]
    dropped = max(0, len(unit.candidates) - len(kept))
    lines = [
        f"UNIT: {unit.unit_id} ({unit.kind})",
        f"TITLE: {unit.title}",
        "",
        "CONCEPTS (tag - name - aliases):",
    ]
    for c in kept:
        aliases = ", ".join(c.aliases) if c.aliases else "-"
        lines.append(f"  {c.tag} - {c.name} - {aliases}")
    lines += ["", "LABELS to map:"]
    for index, label in enumerate(unit.labels):
        section = unit.sections[index] if index < len(unit.sections) else ()
        if section:
            lines.append(f"  {label}  [section: {' > '.join(section)}]")
        else:
            lines.append(f"  {label}")
    return _SYSTEM, "\n".join(lines), dropped


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


@dataclass(frozen=True)
class Resolution:
    """One label resolved to one concept, and HOW.

    `method` is "memory" | "lexical" | "llm". It is not decoration: a wrong
    value must trace to the tier that produced it, and the three tiers have
    very different trust profiles."""

    label: str
    tag: str
    method: str
    score: float


# Below this, `lexical_match` is guessing from token overlap rather than
# recognising a name. MEASURED: sub-floor overlaps produced every remaining
# wrong value (REMC 1, RIL 2) by winning uncontested. Silence is better --
# an unmapped label is visible in the artifact, a wrong number is not.
LEXICAL_FLOOR = 0.75

# A concept expecting money can never be satisfied by a share count, a
# per-share figure or a ratio. "unknown" is permitted: a classification gap
# must not become silent data loss.
_CURRENCY_ONLY = frozenset({"count", "per_share", "ratio"})

# A concept name may carry a period qualifier the SECTION can confirm or
# contradict. These are the only two that matter on an Indian balance sheet,
# and they are mutually exclusive by construction.
_QUALIFIERS: tuple[tuple[str, str], ...] = (
    ("non-current", "non-current"),
    ("current", "current"),
)


def _qualifier(text: str) -> str:
    """The period qualifier `text` asserts, or "".

    "non-current" is tested FIRST and wins: it CONTAINS "current", so testing
    in the other order would label every non-current concept "current" and
    invert the disambiguation this exists to provide."""
    key = text.lower()
    for needle, name in _QUALIFIERS:
        if needle in key:
            return name
    return ""


def resolve_lexical(unit: MapUnit) -> dict[str, Resolution]:
    """Labels an alias or normalised form settles, with no model call.

    Free and deterministic, so it runs before the LLM. It is NOT sufficient on
    its own -- measured across three issuers it answered 69% / 58% / 30% -- but
    every label it settles is one the expensive tier never sees.

    `lexical_match(label, candidates)` returns `(candidate | None, method,
    score)` and REFUSES a tie rather than guessing, so a None here means the
    label is genuinely ambiguous and belongs to the semantic tier.

    A ROW'S SECTION BREAKS A TIE THE LABEL ALONE CANNOT. Two candidates
    sharing a label -- "Lease liabilities" under both Non-current and Current
    liabilities -- score identically on token overlap and `lexical_match`
    refuses rather than guess. When the row's section carries a period
    qualifier that CONTRADICTS a candidate's own qualifier, that candidate is
    excluded before matching, so the surviving candidate is no longer tied
    and resolves for free. If every candidate is excluded, the label stays
    unresolved rather than accepting a contradicted match."""
    out: dict[str, Resolution] = {}
    for index, label in enumerate(unit.labels):
        kind = unit.kinds[index] if index < len(unit.kinds) else "unknown"
        if kind in _CURRENCY_ONLY:
            continue
        section = unit.sections[index] if index < len(unit.sections) else ()
        section_qualifier = _qualifier(" ".join(section))
        candidates = unit.candidates
        if section_qualifier:
            candidates = tuple(
                c for c in candidates
                if _qualifier(c.name) in ("", section_qualifier)
            )
        candidate, method, score = lexical_match(label, candidates)
        if candidate is not None and score >= LEXICAL_FLOOR:
            out[label] = Resolution(label, candidate.tag, f"lexical:{method}", score)
    return out


def resolve_from_memory(
    unit: MapUnit, store: MemoryStore
) -> dict[str, Resolution]:
    """Labels a PREVIOUS document already resolved.

    Keyed on (label, section, basis) rather than label alone: "Computer
    equipment" under PPE is a fixed asset, "Computer software" under
    intangibles is not -- and CONSOLIDATED and STANDALONE are different
    numbers for the same concept, which is exactly the confusion the basis
    model exists to prevent. `MemoryKey.of` normalises all three parts, so
    callers never have to match its casing rules by hand.

    `recall` returns only TRUSTED entries, and a remembered tag this unit does
    not offer is DROPPED -- memory from another statement must not leak in."""
    offered = {c.tag for c in unit.candidates}
    out: dict[str, Resolution] = {}
    for label in unit.labels:
        entry = recall(store, MemoryKey.of(label, unit.title, unit.basis))
        if entry is not None and entry.tag in offered:
            out[label] = Resolution(label, entry.tag, "memory", entry.confidence)
    return out


def _items_by_parent(region: Mapping[str, object]) -> dict[str, list[Mapping[str, object]]]:
    out: dict[str, list[Mapping[str, object]]] = {}
    raw = region.get("line_items") if isinstance(region, Mapping) else None
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, Mapping):
            continue
        parent = item.get("parent")
        if isinstance(parent, str) and parent:
            out.setdefault(parent, []).append(item)
    return out


def _section_path_of(item: Mapping[str, object]) -> tuple[str, ...]:
    """A LineItem body's `section_path`, or `()` if absent or malformed."""
    raw = item.get("section_path")
    if not isinstance(raw, list):
        return ()
    return tuple(str(s) for s in raw)


def units_from_fog(
    region: Mapping[str, object],
    *,
    face_candidates: Sequence[ConceptCandidate],
    note_candidates: Sequence[ConceptCandidate],
    basis: str = "",
) -> list[MapUnit]:
    """One MapUnit per statement face and per note that HAS line items.

    A unit with no items is not emitted: it would cost an LLM call and answer
    nothing. Units are id-sorted so the artifact hashes stably. Total.

    `basis` is carried onto every unit unchanged -- it identifies which of the
    FOG's bases this `region` IS, not something derived per-unit -- so
    `resolve_from_memory` can scope its lookups by it."""
    by_parent = _items_by_parent(region)
    units: list[MapUnit] = []

    for key, kind in (("statements", "face"), ("notes", "note")):
        raw = region.get(key) if isinstance(region, Mapping) else None
        for node in raw if isinstance(raw, list) else []:
            if not isinstance(node, Mapping):
                continue
            node_id = node.get("id")
            if not isinstance(node_id, str) or not node_id:
                continue
            items = by_parent.get(node_id, [])
            labels = tuple(
                str(i.get("label", "")) for i in items if str(i.get("label", ""))
            )
            if not labels:
                continue
            kinds = tuple(
                str(i.get("kind", "unknown"))
                for i in items if str(i.get("label", ""))
            )
            sections = tuple(
                _section_path_of(i) for i in items if str(i.get("label", ""))
            )
            title = str(node.get("title") or node.get("statement") or "")
            units.append(MapUnit(
                unit_id=node_id, kind=kind, title=title, labels=labels,
                candidates=candidates_for_kind(kind, face_candidates, note_candidates),
                kinds=kinds, basis=basis, sections=sections,
            ))
    units.sort(key=lambda u: u.unit_id)
    return units


def mapping_edges(
    resolutions: Mapping[str, Mapping[str, Resolution]],
    region: Mapping[str, object],
) -> tuple[list[Edge], list[dict[str, object]]]:
    """`MAPS_TO` edges plus the proof record behind each.

    A resolution naming a line item this region does not hold is dropped: an
    edge to a node that is not in the graph looks like navigation and leads
    nowhere. Both outputs are sorted -- they reach an artifact body. Total."""
    by_parent = _items_by_parent(region)
    edges: list[Edge] = []
    proof: list[dict[str, object]] = []

    for unit_id in sorted(resolutions):
        per_label = resolutions[unit_id]
        for item in by_parent.get(unit_id, []):
            label = str(item.get("label", ""))
            item_id = item.get("id")
            resolution = per_label.get(label)
            if resolution is None or not isinstance(item_id, str):
                continue
            edges.append(Edge("MAPS_TO", item_id, resolution.tag))
            proof.append({
                "line_item": item_id,
                "unit": unit_id,
                "label": label,
                "tag": resolution.tag,
                "method": resolution.method,
                "score": round(float(resolution.score), 4),
            })

    edges.sort(key=lambda e: (e.source, e.target))
    proof.sort(key=lambda p: (str(p["line_item"]), str(p["tag"])))
    return edges, proof
