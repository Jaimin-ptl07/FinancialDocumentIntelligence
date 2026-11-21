"""Decide which notes to open, and what to ask each one about.

The face of a statement is a summary with pointers. "Other expenses 43,071 (note
13(b))" is one number standing in for a schedule of fifteen. Planning is the step
that turns those pointers into a bounded set of questions -- ONE per note, each
carrying only the concepts that note could plausibly answer.

WHY PLANNING IS NOT RETRIEVAL
-----------------------------
Similarity search retrieves what a query resembles. But the face already states,
in the issuer's own hand, exactly where each concept's detail lives -- a citation
is a fact, not a guess, and following it beats ranking chunks by cosine distance.
Retrieval is the fallback for what the face does not cite, not the primary path.

TWO WAYS A NOTE EARNS A VISIT
-----------------------------
1. BY REFERENCE -- a face row cites it and that row maps to a concept. The note
   is then asked about that concept's descendants.
2. BY TITLE -- the note's own heading matches a concept the face never cited.
   This is not redundant: TCS's face prints one "Other income" line, so without
   the title path the nine non-operating income concepts beneath it have no
   route to the note that actually lists them.

WHAT BOUNDS THE COST
--------------------
One call per NOTE, not per concept. TCS's P&L face cites 11 notes; the taxonomy
beneath them holds 188 leaves. Asking per concept would be 188 calls that mostly
re-read the same tables.

Pure (`fdi.core` law): no I/O, deterministic, total.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from fdi.core.concepts import ConceptCandidate, ConceptIndex, normalize
from fdi.core.mapping import ReportLine, map_lines
from fdi.core.statements import FaceRow, NoteWindow


@dataclass(frozen=True)
class NoteUnit:
    """One note, and the concepts it is being asked to resolve."""

    ref: str
    title: str
    page: int
    window: tuple[int, int]
    concepts: tuple[ConceptCandidate, ...]
    reason: str          # "reference" | "title"
    serves: tuple[str, ...] = ()   # the face concepts that sent us here


@dataclass(frozen=True)
class FacePlan:
    """What the face alone settled, and where the rest must be looked up."""

    facts: tuple[tuple[str, float], ...]      # tag -> reported value
    units: tuple[NoteUnit, ...]
    unmapped: tuple[str, ...]                 # face labels no concept claimed


def _candidates_for(index: ConceptIndex, tag: str) -> list[ConceptCandidate]:
    """Every concept beneath `tag` -- the ones a note could print.

    Descendants at ANY depth, not just the next level: note 11 lists Interest,
    Dividend and Rent individually, so offering only the three intermediate
    subtotals leaves the model nothing to map those lines onto."""
    group = index.for_tag(tag)
    return list(group.candidates) if group is not None else []


def _dedupe(cands: Sequence[ConceptCandidate]) -> tuple[ConceptCandidate, ...]:
    seen: set[str] = set()
    out: list[ConceptCandidate] = []
    for c in cands:
        if c.tag not in seen:
            seen.add(c.tag)
            out.append(c)
    return tuple(out)


def plan_from_face(
    face: Sequence[FaceRow],
    windows: Mapping[str, NoteWindow],
    index: ConceptIndex,
    candidates: Sequence[ConceptCandidate],
    *,
    max_concepts: int = 120,
) -> FacePlan:
    """Read the face, then decide which notes to open.

    `candidates` is the statement's full concept set -- what the face rows
    themselves are mapped against. Per-note concept sets are drawn from the
    index, narrowed to the descendants of whatever the citing row resolved to.
    """
    rows = [r for r in face if r.label]
    mapped = map_lines(
        [ReportLine(label=r.label, value=r.current, page=None) for r in rows],
        candidates,
    )

    facts: list[tuple[str, float]] = []
    unmapped: list[str] = []
    tag_of: dict[int, str] = {}
    for i, m in enumerate(mapped):
        if not m.is_mapped:
            unmapped.append(m.source_label)
            continue
        tag_of[i] = m.tag
        if m.value is not None:
            facts.append((m.tag, m.value))

    # 1. BY REFERENCE -- the issuer told us where the detail is.
    wanted: dict[str, list[ConceptCandidate]] = {}
    serves: dict[str, list[str]] = {}
    reason: dict[str, str] = {}
    for i, row in enumerate(rows):
        tag = tag_of.get(i)
        if not row.ref or row.ref not in windows or not tag:
            continue
        kids = _candidates_for(index, tag)
        if not kids:
            continue
        wanted.setdefault(row.ref, []).extend(kids)
        serves.setdefault(row.ref, []).append(tag)
        reason.setdefault(row.ref, "reference")

    # 2. BY TITLE -- a note the face summarised into one line still gets opened.
    for ref, win in windows.items():
        if ref in wanted or not win.title:
            continue
        group = index.for_label(win.title)
        if group is None or not group.candidates:
            continue
        wanted[ref] = list(group.candidates)
        serves[ref] = [group.key]
        reason[ref] = "title"

    units: list[NoteUnit] = []
    for ref in sorted(wanted, key=lambda r: windows[r].start):
        win = windows[ref]
        concepts = _dedupe(wanted[ref])[:max_concepts]
        if not concepts:
            continue
        units.append(NoteUnit(
            ref=ref, title=win.title, page=win.page,
            window=(win.start, win.end), concepts=concepts,
            reason=reason.get(ref, "reference"),
            serves=tuple(dict.fromkeys(serves.get(ref, ()))),
        ))
    return FacePlan(tuple(facts), tuple(units), tuple(unmapped))


NOTE_SYSTEM = (
    "You read ONE note or statement table from an Indian annual report and map "
    "its line items onto a fixed list of concepts.\n"
    "RULES:\n"
    "- Copy the number EXACTLY as printed. Do NOT rescale or convert units.\n"
    "- Map each line to at most one concept. If a line fits none, use tag \"\".\n"
    "- Never invent a value. If a concept is not in this table, omit it.\n"
    "- The FIRST value column is the current year, the second is the prior year.\n"
    "- Some tables EXPLAIN a total instead of composing it: an effective-tax-"
    "rate reconciliation (statutory rate -> expected tax -> adjustments -> "
    "reported tax), a variance bridge, a movement schedule running from an "
    "opening to a closing balance. Their rows are adjustments to a hypothetical "
    "or prior figure, NOT components of the reported one. Map nothing from "
    "them.\n"
    "- If a line is already contained in a subtotal you are also mapping, map "
    "only the subtotal. Never map both a total and the parts that make it up.\n"
    'Respond with JSON only: {"lines":[{"label":"<verbatim>","tag":"<TAG or '
    'empty>","current":<num|null>,"prior":<num|null>}]}'
)


def build_note_prompt(unit: NoteUnit, table_markdown: str, *, unit_hint: str = "as printed") -> str:
    """The user turn for one note unit.

    Values are requested as PRINTED and parsed afterwards by
    `fdi.core.numbers.parse_amount`. Asking the model to normalise separators was
    measurably wrong -- it read '8,685' as 8 -- and no amount of prompt wording
    fixed it, so the instruction is 'copy', not 'interpret'."""
    lines = [
        f"TABLE: Note {unit.ref}" + (f" - {unit.title}" if unit.title else ""),
        f"Reporting unit: {unit_hint}",
        "",
        "CONCEPTS you may map to:",
    ]
    lines.extend(f"- {c.describe()}" for c in unit.concepts)
    lines += ["", "TABLE CONTENT:", table_markdown]
    return "\n".join(lines)


def _echoes_an_ancestor(
    tag: str,
    value: float,
    known: Mapping[str, float],
    parent_of: Mapping[str, str],
    tolerance: float = 1.0,
) -> str:
    """The ancestor whose value this line merely repeats, or "".

    A note ends with its own total, and the model sometimes maps that row onto
    one of the note's COMPONENTS. Measured on TCS: note 15's total 13,710 landed
    on `OTTAX` ("Other tax items"), and since the face already had
    `TOTTAXEXP` = 13,710, the reconstructed tax expense came to 27,420 --
    exactly twice the truth. A component that equals its own ancestor is not a
    component."""
    seen: set[str] = set()
    cur = parent_of.get(tag, "")
    while cur and cur not in seen:
        seen.add(cur)
        prior = known.get(cur)
        if prior is not None and abs(prior - value) <= max(tolerance, abs(prior) * 0.005):
            return cur
        cur = parent_of.get(cur, "")
    return ""


def read_note_reply(
    reply: object,
    unit: NoteUnit,
    *,
    known: Mapping[str, float] | None = None,
    parent_of: Mapping[str, str] | None = None,
) -> list[tuple[str, str, float | None, float | None]]:
    """(tag, label, current, prior) for the lines the model actually placed.

    A tag the unit never offered is DROPPED, not trusted: the model occasionally
    answers with a plausible concept from elsewhere in the taxonomy, and a fact
    attributed to a note that does not mention it is unauditable.

    `known` + `parent_of` additionally reject a line that just repeats an
    ancestor's already-established total -- see `_echoes_an_ancestor`. Both are
    optional so existing callers are unaffected."""
    if not isinstance(reply, Mapping):
        return []
    allowed = {c.tag for c in unit.concepts}
    raw = reply.get("lines")
    if not isinstance(raw, list):
        return []
    facts = known or {}
    parents = parent_of or {}
    out: list[tuple[str, str, float | None, float | None]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        tag = item.get("tag")
        if not isinstance(tag, str) or tag not in allowed or tag in seen:
            continue
        current = item.get("current")
        prior = item.get("prior")
        if not isinstance(current, (int, float)) or isinstance(current, bool):
            continue
        if facts and parents and _echoes_an_ancestor(tag, float(current), facts, parents):
            continue
        seen.add(tag)
        label = item.get("label")
        out.append((
            tag,
            label if isinstance(label, str) else "",
            float(current),
            float(prior) if isinstance(prior, (int, float))
            and not isinstance(prior, bool) else None,
        ))
    return out


def unmapped_face_labels(plan: FacePlan) -> list[str]:
    """Face lines no concept claimed -- the honest coverage gap.

    Reported separately rather than silently dropped: a label here is either a
    concept the taxonomy lacks or an alias it has not learned, and both are
    findings."""
    return [lbl for lbl in plan.unmapped if normalize(lbl)]
