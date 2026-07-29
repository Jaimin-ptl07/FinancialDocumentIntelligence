"""Map a report's own line items onto taxonomy concepts, and PROVE the mapping.

THE INVERSION
-------------
Extraction asks a "pull" question -- *what is the value of `GRSPNE`?* -- and the
model hunts the document for our label. When the issuer writes "Computer
equipment", "Plant and machinery" or "Premises and equipment", nothing matches
and the field is silently null. Measured on TCS: 4 of 402 leaves matched a
report label directly.

This module asks the "push" question instead -- *which of THESE concepts does
each REPORT line belong to?* Three things follow:

  * the candidate set is bounded (fdi.core.concepts supplies the parent's
    children, not all ~500 tags);
  * we classify the issuer's words rather than searching for ours, so unseen
    vocabulary stops being fatal;
  * EVERY report line gets a decision -- mapped, or explicitly unmapped. Nothing
    is silently invisible, which is the structural fix for false nulls.

THE ARBITER IS WHY THIS IS SAFE
-------------------------------
A mapping is a guess until something checks it. The parent total is already
extracted at high confidence, so `arbitrate` uses it as the proof: when the
mapped children sum to the parent, the assignment is corroborated; when the gap
equals exactly one unmapped line, that line IS the missing child; otherwise the
whole note is flagged for review rather than silently committed.

Deliberate asymmetry: TOLERANT at leaf level, STRICT at total level. Swapping
two siblings is harmless when the parent still ties (both roll into the same
downstream ratio); a total that does not tie is never acceptable.

Pure (`fdi.core` law): no I/O, no LLM, deterministic, total. The lexical pass
here resolves most lines for free; only what it cannot decide needs a model,
and that call lives in the pipeline layer.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from fdi.core.concepts import ConceptCandidate, normalize

# A lexical hit must cover most of the shorter string; below this, "Other
# assets" would happily claim "Other financial assets".
_MIN_TOKEN_OVERLAP = 0.75

# Sum-vs-parent tolerance. Statements are rounded to the reporting unit, so a
# handful of units of drift across a dozen children is presentation, not error.
DEFAULT_TOLERANCE = 1.0


@dataclass(frozen=True)
class ReportLine:
    """One line item as the REPORT states it, before any mapping."""

    label: str
    value: float | None = None
    page: int | None = None
    evidence_id: str = ""


@dataclass(frozen=True)
class Mapped:
    """A report line placed onto a concept -- or explicitly not placed.

    `source_label` is retained always: the tag is for computation, the issuer's
    own wording is what an auditor recognises. A fact that cannot show the words
    it came from is not auditable."""

    source_label: str
    tag: str = ""
    value: float | None = None
    method: str = "unmapped"  # alias | name | memory | semantic | unmapped
    confidence: float = 0.0
    page: int | None = None
    evidence_id: str = ""
    note: str = ""

    @property
    def is_mapped(self) -> bool:
        return bool(self.tag)


def _tokens(text: str) -> set[str]:
    return {t for t in normalize(text).split() if len(t) > 2}


def _overlap(a: str, b: str) -> float:
    """Fraction of the SHORTER token set that the two labels share."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def lexical_match(
    label: str, candidates: Sequence[ConceptCandidate]
) -> tuple[ConceptCandidate | None, str, float]:
    """Resolve `label` against `candidates` without a model.

    Order matters: an exact name or alias hit is certain, token overlap is a
    judgement. Ties are refused rather than guessed -- two candidates that score
    identically mean the label is genuinely ambiguous, which is a job for the
    semantic pass, not for whichever happened to come first."""
    key = normalize(label)
    if not key:
        return (None, "unmapped", 0.0)

    for cand in candidates:
        if normalize(cand.name) == key:
            return (cand, "name", 1.0)
    for cand in candidates:
        if any(normalize(a) == key for a in cand.aliases):
            return (cand, "alias", 0.95)

    scored = [(c, max(_overlap(label, c.name),
                      *(_overlap(label, a) for a in c.aliases)) if c.aliases
               else _overlap(label, c.name))
              for c in candidates]
    scored = [(c, s) for c, s in scored if s >= _MIN_TOKEN_OVERLAP]
    if not scored:
        return (None, "unmapped", 0.0)
    scored.sort(key=lambda cs: cs[1], reverse=True)
    if len(scored) > 1 and scored[0][1] == scored[1][1]:
        return (None, "unmapped", 0.0)  # ambiguous: refuse, never guess
    best, score = scored[0]
    return (best, "name", round(min(0.9, score), 3))


def map_lines(
    lines: Sequence[ReportLine], candidates: Sequence[ConceptCandidate]
) -> list[Mapped]:
    """Place every report line on a candidate, or mark it unmapped.

    A concept is claimed at most once: financial schedules do not list the same
    class twice, so a second line matching an already-taken concept is far more
    likely to be a different item and is left for the semantic pass."""
    taken: set[str] = set()
    out: list[Mapped] = []
    for line in lines:
        available = [c for c in candidates if c.tag not in taken]
        cand, method, score = lexical_match(line.label, available)
        if cand is None:
            out.append(
                Mapped(source_label=line.label, value=line.value, page=line.page,
                       evidence_id=line.evidence_id, note="no lexical candidate")
            )
            continue
        taken.add(cand.tag)
        out.append(
            Mapped(source_label=line.label, tag=cand.tag, value=line.value,
                   method=method, confidence=score, page=line.page,
                   evidence_id=line.evidence_id)
        )
    return out


@dataclass(frozen=True)
class Arbitration:
    """The verdict on a note's mappings, and why."""

    status: str  # corroborated | repaired | unreconciled | no_total
    expected: float | None
    actual: float | None
    delta: float | None
    rows: tuple[Mapped, ...] = ()
    message: str = ""

    @property
    def accepted(self) -> bool:
        return self.status in ("corroborated", "repaired")


def _repair(
    rows: Sequence[Mapped],
    assign: Mapping[int, str],
    parent_total: float,
    total: float,
    delta: float,
    message: str,
) -> Arbitration:
    repaired = tuple(
        Mapped(source_label=r.source_label, tag=assign.get(id(r), r.tag),
               value=r.value, method="residual", confidence=0.6, page=r.page,
               evidence_id=r.evidence_id,
               note="assigned: the unexplained residual accounts for it")
        if id(r) in assign else r
        for r in rows
    )
    return Arbitration("repaired", parent_total, round(total, 4), delta,
                       repaired, message)


def arbitrate(
    rows: Sequence[Mapped],
    parent_total: float | None,
    *,
    tolerance: float = DEFAULT_TOLERANCE,
    fallback_tag: str = "",
) -> Arbitration:
    """Check mapped children against the parent total already extracted.

    Outcomes:
      corroborated -- the sum ties; accept and let confidence rise;
      repaired     -- the gap equals exactly one unmapped line, so that line is
                      the missing child and is assigned;
      unreconciled -- the sum does not tie; flag the NOTE, commit nothing
                      silently;
      no_total     -- nothing to check against; mappings stand unproven.

    `fallback_tag` is the concept's catch-all ("Other tangible assets"); repairs
    that AGGREGATE several orphans require it, because inventing a home for them
    would be a guess where the sum is the only evidence.
    """
    mapped = [r for r in rows if r.is_mapped and isinstance(r.value, (int, float))]
    total = sum(float(r.value) for r in mapped if r.value is not None)

    if parent_total is None:
        return Arbitration("no_total", None, round(total, 4), None, tuple(rows),
                           "no parent total to reconcile against")

    delta = round(parent_total - total, 4)
    if abs(delta) <= tolerance:
        return Arbitration("corroborated", parent_total, round(total, 4), delta,
                           tuple(rows), "children sum to the parent total")

    unmapped = [
        r for r in rows if not r.is_mapped and isinstance(r.value, (int, float))
    ]
    exact = [r for r in unmapped if abs(float(r.value or 0.0) - delta) <= tolerance]

    # One orphan whose value IS the residual: it is that child. Still needs a
    # bucket to name -- proving something is missing does not say what it IS.
    if fallback_tag and len(exact) == 1 and len(unmapped) == 1:
        return _repair(rows, {id(exact[0]): fallback_tag}, parent_total, total,
                       delta, f"one unmapped line equals the residual {delta}")

    # Several orphans that TOGETHER equal the residual: the issuer split a class
    # we carry as one bucket (TCS reports Computer/Office equipment and
    # Electrical installations where the taxonomy has "Other tangible assets").
    # The sum tying exactly is the proof; without a fallback bucket to hold
    # them we must not invent one.
    if fallback_tag and len(unmapped) > 1:
        orphan_total = sum(float(r.value or 0.0) for r in unmapped)
        if abs(orphan_total - delta) <= tolerance:
            return _repair(
                rows, {id(r): fallback_tag for r in unmapped}, parent_total,
                total, delta,
                f"{len(unmapped)} unmapped lines sum to the residual {delta}; "
                f"aggregated into {fallback_tag}",
            )

    return Arbitration("unreconciled", parent_total, round(total, 4), delta,
                       tuple(rows),
                       f"children sum to {round(total, 4)} against a parent "
                       f"total of {parent_total} (delta {delta})")


def unmapped_labels(rows: Sequence[Mapped]) -> list[str]:
    """Report lines that found no home -- taxonomy gaps worth surfacing, not
    silently dropping."""
    return [r.source_label for r in rows if not r.is_mapped]


def to_records(rows: Sequence[Mapped]) -> list[dict[str, object]]:
    """Mapped rows as plain records, each carrying the issuer's own wording."""
    out: list[dict[str, object]] = []
    for row in rows:
        record: dict[str, object] = {
            "tag": row.tag,
            "source_label": row.source_label,
            "value": row.value,
            "mapping_method": row.method,
            "mapping_confidence": row.confidence,
            "origin": "extracted",
        }
        if row.page is not None:
            record["page"] = row.page
        if row.evidence_id:
            record["evidence_id"] = row.evidence_id
        if row.note:
            record["note"] = row.note
        out.append(record)
    return out


def candidates_from(group: Mapping[str, object] | None) -> list[ConceptCandidate]:
    """Tolerant accessor for a ConceptGroup-shaped mapping (or None)."""
    if group is None:
        return []
    cands = group.get("candidates") if isinstance(group, Mapping) else None
    if not isinstance(cands, (list, tuple)):
        return []
    return [c for c in cands if isinstance(c, ConceptCandidate)]
