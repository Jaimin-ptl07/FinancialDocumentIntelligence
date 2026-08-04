"""Concept index: the bounded candidate set a report line may map onto.

WHY THIS EXISTS
---------------
Extraction today asks a "pull" question -- *what is the value of `GRSPNE`
(Plant and equipment)?* -- and the model hunts the document for a taxonomy
label. When the issuer writes something else ("Computer equipment", "Plant and
machinery", "Premises and equipment") nothing matches and the field comes back
silently null. Measured on TCS FY2025-26, only 4 of 402 leaves matched a report
label directly.

The inversion that fixes it is a "push" question -- *which of THESE concepts
does each report line belong to?* -- and it needs one thing this module
provides: a SMALL, bounded candidate set. Asking a model to place "Computer
equipment" among ~500 tags is hopeless; asking it to place that line among the
seven children of "Fixed assets/PPE Gross Block" is tractable, and the parent
total then proves the assignment by reconciliation.

The candidate set is derived from the schema hierarchy the taxonomy already
encodes -- nothing is invented here. A concept's candidates are every TAGGED
descendant, not only its direct leaves, because issuers report at varying
granularity: one filing lists "Raw materials" (the parent), another splits it
into "Imported"/"Indigenous" (the leaves), and both must be mappable.

Pure (`fdi.core` law): no I/O, deterministic, total -- malformed input yields an
empty index rather than raising.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from dataclasses import field as dc_field

# A candidate set larger than this is not a useful disambiguation prompt; it
# also signals the grouping is too coarse. Truncation is reported, never silent.
MAX_CANDIDATES = 40


def normalize(text: object) -> str:
    """Case/punctuation-insensitive key, shared with fdi.core.notes."""
    if not isinstance(text, str):
        return ""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", text.lower())).strip()


@dataclass(frozen=True)
class ConceptCandidate:
    """One taxonomy concept a report line may be mapped onto."""

    tag: str
    name: str
    field_path: str
    depth: int
    origin: str
    sr_no: str | None = None
    aliases: tuple[str, ...] = ()
    definition: str = ""

    def describe(self) -> str:
        """One line for a mapping prompt: the tag, its name, its report-side
        wordings, and -- where siblings are genuinely ambiguous -- what the
        concept includes and excludes."""
        parts = [f"{self.tag}: {self.name}"]
        if self.aliases:
            parts.append(f"(also reported as: {', '.join(self.aliases)})")
        if self.definition:
            parts.append(f"-- {self.definition}")
        return " ".join(parts)


@dataclass(frozen=True)
class ConceptGroup:
    """A parent concept plus the bounded set of concepts beneath it."""

    key: str
    name: str
    statement: str
    note_aliases: tuple[str, ...] = ()
    candidates: tuple[ConceptCandidate, ...] = ()
    truncated: int = 0

    def describe(self) -> list[str]:
        return [c.describe() for c in self.candidates]


@dataclass(frozen=True)
class ConceptIndex:
    """All groups, addressable by concept name or by a report-side alias."""

    groups: tuple[ConceptGroup, ...] = ()
    by_key: dict[str, ConceptGroup] = dc_field(default_factory=dict)
    by_alias: dict[str, ConceptGroup] = dc_field(default_factory=dict)

    def for_label(
        self, label: object, *, statement: str | None = None
    ) -> ConceptGroup | None:
        """The candidate set for a report-side label ("Property, plant and
        equipment") or a taxonomy concept name.

        `statement` disambiguates a label that legitimately appears in more than
        one statement: the PPE note holds both the asset block AND the
        depreciation charge, so "property plant and equipment" binds a concept
        in `assets` and another in `profit_and_loss`. Without the hint the first
        (broadest, document-order) binding wins."""
        key = normalize(label)
        if not key:
            return None
        if statement:
            scoped = self.by_alias.get(f"{statement}\x00{key}")
            if scoped is not None:
                return scoped
            own = self.by_key.get(f"{statement}\x00{key}")
            if own is not None:
                return own
        return self.by_key.get(key) or self.by_alias.get(key)

    def for_tag(self, tag: object) -> ConceptGroup | None:
        """The NARROWEST group owning `tag` -- the siblings it actually competes
        with. A tag also belongs to every ancestor group, but mapping wants the
        tightest set: `GRSBLDG` competes with the other PPE classes, not with
        every concept under "Non-current assets"."""
        if not isinstance(tag, str) or not tag:
            return None
        best: ConceptGroup | None = None
        for group in self.groups:
            if any(c.tag == tag for c in group.candidates) and (
                best is None or len(group.candidates) < len(best.candidates)
            ):
                best = group
        return best


def _alias_list(node: Mapping[str, object]) -> tuple[str, ...]:
    """A concept's report-side label variants, under either schema generation.

    v4.3 names this `note_aliases`; the v3.2 seed that `load_schema` still
    serves by default names it `aliases` -- 190 of them, none of which this
    module could see. Reading only one key meant the FREE lexical tier never
    fired against whichever generation was live, sending every label to the
    paid tier or leaving it unmapped. Aliases are aliases; accept both, with
    the v4.3 name winning when a document carries both."""
    for key in ("note_aliases", "aliases"):
        raw = node.get(key)
        if isinstance(raw, list):
            found = tuple(a for a in raw if isinstance(a, str) and a)
            if found:
                return found
    return ()


def _tagged_descendants(
    node: Mapping[str, object], depth: int = 1
) -> list[ConceptCandidate]:
    """Every tagged node beneath `node`, at any depth. Issuers report at
    varying granularity, so an intermediate parent is as valid a target as a
    leaf."""
    out: list[ConceptCandidate] = []
    children = node.get("fields")
    if not isinstance(children, list):
        return out
    for child in children:
        if not isinstance(child, Mapping):
            continue
        tag = child.get("tag")
        if isinstance(tag, str) and tag:
            name = child.get("field_name")
            path = child.get("field_path")
            sr_no = child.get("sr_no")
            aliases = _alias_list(child)
            definition = child.get("definition")
            out.append(
                ConceptCandidate(
                    tag=tag,
                    name=name if isinstance(name, str) else "",
                    field_path=path if isinstance(path, str) else "",
                    depth=depth,
                    origin=str(child.get("origin") or "extracted"),
                    sr_no=sr_no if isinstance(sr_no, str) else None,
                    aliases=aliases,
                    definition=definition if isinstance(definition, str) else "",
                )
            )
        out.extend(_tagged_descendants(child, depth + 1))
    return out


def _walk_groups(
    nodes: Sequence[object], statement: str, out: list[ConceptGroup], min_children: int
) -> None:
    for node in nodes:
        if not isinstance(node, Mapping):
            continue
        children = node.get("fields")
        if not isinstance(children, list) or not children:
            continue
        candidates = _tagged_descendants(node)
        if len(candidates) >= min_children:
            name = node.get("field_name")
            aliases = _alias_list(node)
            kept = candidates[:MAX_CANDIDATES]
            out.append(
                ConceptGroup(
                    key=normalize(name),
                    name=name if isinstance(name, str) else "",
                    statement=statement,
                    note_aliases=aliases,
                    candidates=tuple(kept),
                    truncated=max(0, len(candidates) - len(kept)),
                )
            )
        _walk_groups(children, statement, out, min_children)


def parent_map(schema: Mapping[str, object]) -> dict[str, str]:
    """child tag -> nearest tagged ancestor.

    Needed to catch a note's TOTAL row being mapped onto one of its own
    components: note 15's grand total 13,710 was placed on `OTTAX` ("Other tax
    items"), whose ancestor `TOTTAXEXP` was already known to be 13,710 -- so the
    reconstructed total came out at exactly twice the truth. Recognising that a
    value equals an ANCESTOR's is what makes that detectable."""
    out: dict[str, str] = {}
    modules = schema.get("modules") if isinstance(schema, Mapping) else None
    if not isinstance(modules, list):
        return out

    def walk(fields: object, ancestor: str) -> None:
        if not isinstance(fields, list):
            return
        for node in fields:
            if not isinstance(node, Mapping):
                continue
            tag = node.get("tag")
            own = tag if isinstance(tag, str) and tag else ancestor
            if isinstance(tag, str) and tag and ancestor:
                out[tag] = ancestor
            walk(node.get("fields"), own)

    for module in modules:
        if not isinstance(module, Mapping):
            continue
        units = module.get("retrieval_units")
        if not isinstance(units, list):
            continue
        for unit in units:
            if isinstance(unit, Mapping):
                walk(unit.get("fields"), "")
    return out


def all_concepts(schema: Mapping[str, object]) -> tuple[ConceptCandidate, ...]:
    """EVERY tagged concept in the schema, top-level parents included.

    Not the same set as the union of `ConceptIndex` group candidates, and the
    difference is the whole point: a group holds a parent's DESCENDANTS, so a
    root concept is a group *key* but appears in no group's candidate list.
    Mapping the face against group candidates alone therefore cannot place
    "Revenue from operations" or "Profit before tax" -- measured on TCS, 15 of
    28 face rows went unmapped, including every waterfall total.

    Face rows are summaries and match parents; note rows are detail and match
    descendants. The two need different candidate sets."""
    modules = schema.get("modules") if isinstance(schema, Mapping) else None
    if not isinstance(modules, list):
        return ()
    seen: set[str] = set()
    out: list[ConceptCandidate] = []
    for module in modules:
        if not isinstance(module, Mapping):
            continue
        units = module.get("retrieval_units")
        if not isinstance(units, list):
            continue
        for unit in units:
            if not isinstance(unit, Mapping):
                continue
            fields = unit.get("fields")
            if not isinstance(fields, list):
                continue
            # A synthetic root makes the unit's own top-level fields children,
            # so they are emitted like any other tagged node.
            for cand in _tagged_descendants({"fields": fields}):
                if cand.tag not in seen:
                    seen.add(cand.tag)
                    out.append(cand)
    return tuple(out)


def build_concept_index(
    schema: Mapping[str, object], *, min_children: int = 2
) -> ConceptIndex:
    """Index every parent concept that owns at least `min_children` tagged
    descendants. Groups are emitted parent-before-child, so the FIRST binding
    for an alias is the broadest one -- a report label like "Property, plant
    and equipment" should reach the whole PPE block, not one sub-slice."""
    modules = schema.get("modules") if isinstance(schema, Mapping) else None
    if not isinstance(modules, list):
        return ConceptIndex()
    groups: list[ConceptGroup] = []
    for module in modules:
        if not isinstance(module, Mapping):
            continue
        statement = str(module.get("statement") or "")
        units = module.get("retrieval_units")
        if not isinstance(units, list):
            continue
        for unit in units:
            if not isinstance(unit, Mapping):
                continue
            fields = unit.get("fields")
            if isinstance(fields, list):
                _walk_groups(fields, statement, groups, min_children)

    by_key: dict[str, ConceptGroup] = {}
    by_alias: dict[str, ConceptGroup] = {}
    for group in groups:
        if group.key:
            by_key.setdefault(group.key, group)
            by_key.setdefault(f"{group.statement}\x00{group.key}", group)
        for alias in group.note_aliases:
            key = normalize(alias)
            if key:
                by_alias.setdefault(key, group)
                by_alias.setdefault(f"{group.statement}\x00{key}", group)
    return ConceptIndex(tuple(groups), by_key, by_alias)
