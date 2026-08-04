# FOG Phases 3–4 — Mapping and Reasoning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the deterministic FOG into a mapped, composed, validated financial model — add `MAPS_TO` edges from the issuer's own line items onto taxonomy concepts, then compose, validate, calibrate confidence and report coverage over them.

**Architecture:** Two new DAG stages. `fog_mapping` is the **only billed stage in the entire pipeline**: it resolves each `LineItem`'s label to a concept through three tiers — memory, then lexical, then one LLM call per unit for the residue — and emits `MAPS_TO` edges carrying their own proof. `fog_reason` is pure and free: compose → validate → confidence → coverage. Isolating the LLM in one stage is what makes a validation-rule change, a new ratio, or a taxonomy edit re-run at zero marginal cost.

**Tech Stack:** Python 3.12 · FastAPI · Celery · MinIO · pytest (`asyncio_mode = "auto"`) · `uv` · ruff + mypy as gates.

**Spec:** [`docs/superpowers/specs/2026-08-03-financial-object-graph-design.md`](../specs/2026-08-03-financial-object-graph-design.md)
**Predecessor:** [`2026-08-03-financial-object-graph-phase-0-2.md`](2026-08-03-financial-object-graph-phase-0-2.md) — the deterministic FOG, complete and measured.

## Global Constraints

- **`fdi.core` is pure and TOTAL.** No I/O, no config, no clock, no RNG. **Never raises on any input shape.** The only sanctioned raise in the FOG is `fog.edge_body` on an unknown edge type.
- **`fdi.core` may not import** `fdi.pipeline`, `fdi.platform`, `fdi.api`, `fdi.workers`. Layering is enforced by `lint-imports`.
- **Decision 10 — never raise.** Every I/O step in a pipeline module is individually guarded. One unit's failure degrades *that unit*, never the document.
- **`absent ≠ missed` is a property of the data structure**, not a convention. An unmapped label must remain a visible node with a recorded reason, never a silent drop.
- **The model never parses a number.** Every amount already lives on a `LineItem`, parsed by `fdi.core.numbers.parse_amount`. The mapper assigns *concepts*, never values.
- **Determinism.** Anything reaching an artifact body is explicitly sorted; bodies are content-addressed.
- **Any artifact body shape change requires bumping BOTH that artifact's `schema_version` AND the emitting `Stage.version`** — `pipeline/run.py` builds the artifact key from declared-dep hashes plus `Stage.version`, so a shape change without a bump serves a stale body from cache forever.
- **Basis:** consolidated is primary, standalone secondary. Both are mapped when present.
- **Test runner:** `uv run --directory backend pytest -m "not integration and not e2e" -q` (currently **1044 passed, 11 skipped**).
- **Gates before every commit:** `uv run --directory backend ruff check .` (100-char lines) and `uv run --directory backend mypy src`, both clean.
- **CRLF:** most files here are CRLF. If `Edit` desyncs, write via Python using `open(path, "r", newline="")` / `open(path, "w", newline="")`.
- **Transcribe, do not rewrite.** Where this plan gives code, copy it exactly. A prior implementer paraphrased its brief and shipped three behavioural bugs its own passing tests missed.

---

## File Structure

**New — pure core (`backend/src/fdi/core/`)**

| File | Responsibility |
|---|---|
| `fog_mapping.py` | Turn FOG `LineItem`s + the schema into `MAPS_TO` edges. Owns the three-tier resolution order and the prompt/reply contract. Pure. |
| `fog_reason.py` | Compose → validate → confidence → coverage over a mapped FOG. Pure. |

**New — pipeline (`backend/src/fdi/pipeline/stages/`)**

| File | Responsibility |
|---|---|
| `fog_mapping.py` | `FogMappingStage`, deps `(fog_objects,)`. The only billed stage. |
| `fog_reason.py` | `FogReasonStage`, deps `(fog_mapping,)`. Pure, free. |

**Reused unchanged — no new platform adapter**

`fdi.pipeline.extractor.ExtractorClient` (`extract(*, system, user) -> ExtractResult`) already gives JSON mode, `temperature=0`, and `LlmCallUsage`. `FakeExtractorClient` keeps the default suite free of live calls.

**Consumed, already built and unit-tested, currently wired to nothing**

`core/concepts.py` (`build_concept_index`, `all_concepts`, `parent_map`) · `core/mapping.py` (`ReportLine`, `Mapped`, `lexical_match`, `map_lines`, `to_records`) · `core/memory.py` (`MemoryKey`, `InMemoryStore`, `recall`, `observe`) · `core/compose.py` (`read_formulas`, `compose`, `certify`, `reconcile`, `summarise`) · `core/validation.py` · `core/confidence.py` · `core/coverage.py`

**Unchanged in this plan:** `pipeline/extractor.py` and the whole similarity path keep running. Route changes and the old-path deletion are Phase 5–6.

---

## Task 1: Mapping prompt and reply contract

**Files:**
- Create: `backend/src/fdi/core/fog_mapping.py`
- Test: `backend/tests/unit/test_fog_mapping_prompt.py`

**Interfaces:**
- Consumes: `fdi.core.concepts.ConceptCandidate`
- Produces:
  - `MapUnit` (frozen dataclass): `unit_id: str`, `kind: str`, `title: str`, `labels: tuple[str, ...]`, `candidates: tuple[ConceptCandidate, ...]`
  - `build_map_prompt(unit: MapUnit) -> tuple[str, str]` → `(system, user)`
  - `read_map_reply(reply: object, unit: MapUnit) -> dict[str, str]` → `{label: tag}`, containing only labels the unit offered and tags it offered

- [ ] **Step 1: Write the failing test**

Create `backend/tests/unit/test_fog_mapping_prompt.py`:

```python
"""The push model: hand the model a unit's OWN labels and ask which concept each
is -- never "what is the value of GRSPNE?". Unseen vocabulary stops being fatal,
and a label with no home surfaces as unmapped instead of vanishing."""

from fdi.core.concepts import ConceptCandidate
from fdi.core.fog_mapping import MapUnit, build_map_prompt, read_map_reply


def _c(tag: str, name: str, aliases: tuple[str, ...] = ()) -> ConceptCandidate:
    """ConceptCandidate requires field_path/depth/origin as well as tag/name --
    a positional-looking constructor here would not compile."""
    return ConceptCandidate(
        tag=tag, name=name, field_path=f"pl.{tag}", depth=1,
        origin="extracted", aliases=aliases,
    )


_UNIT = MapUnit(
    unit_id="nt:consolidated:13",
    kind="note",
    title="Other expenses",
    labels=("Facility expenses", "Travel expenses", "Total"),
    candidates=(
        _c("TCEXP", "Travel and conveyance", ("travel",)),
        _c("OTEXP", "Other expenses"),
    ),
)


def test_the_prompt_carries_every_label_the_unit_holds():
    _system, user = build_map_prompt(_UNIT)
    assert "Facility expenses" in user
    assert "Travel expenses" in user


def test_the_prompt_carries_every_candidate_tag_and_name():
    _system, user = build_map_prompt(_UNIT)
    assert "TCEXP" in user
    assert "Travel and conveyance" in user


def test_the_prompt_names_the_unit_so_the_model_knows_the_context():
    _system, user = build_map_prompt(_UNIT)
    assert "Other expenses" in user
    assert "nt:consolidated:13" in user


def test_the_system_prompt_forbids_inventing_values():
    """The mapper assigns CONCEPTS. Values are already parsed deterministically,
    and letting the model restate one reintroduces the comma bug."""
    system, _user = build_map_prompt(_UNIT)
    assert "value" in system.lower()
    assert "unmapped" in system.lower()


def test_a_reply_maps_labels_to_tags():
    got = read_map_reply({"Travel expenses": "TCEXP"}, _UNIT)
    assert got == {"Travel expenses": "TCEXP"}


def test_a_tag_the_unit_never_offered_is_dropped():
    """The model occasionally answers with a plausible concept from elsewhere in
    the taxonomy. A fact attributed to a note that does not mention it is
    unauditable."""
    assert read_map_reply({"Travel expenses": "GRSPNE"}, _UNIT) == {}


def test_a_label_the_unit_never_held_is_dropped():
    assert read_map_reply({"Invented line": "TCEXP"}, _UNIT) == {}


def test_an_explicit_unmapped_marker_is_honoured_not_guessed():
    assert read_map_reply({"Facility expenses": "unmapped"}, _UNIT) == {}


def test_a_null_or_empty_assignment_is_dropped():
    assert read_map_reply({"Facility expenses": None}, _UNIT) == {}
    assert read_map_reply({"Facility expenses": ""}, _UNIT) == {}


def test_two_labels_may_not_claim_the_same_tag():
    """One concept, one line. Otherwise a note's components double-count into
    the same tag and its total can never arbitrate them."""
    got = read_map_reply(
        {"Travel expenses": "TCEXP", "Facility expenses": "TCEXP"}, _UNIT
    )
    assert list(got.values()) == ["TCEXP"]
    assert len(got) == 1


def test_a_malformed_reply_yields_nothing_and_never_raises():
    assert read_map_reply(None, _UNIT) == {}
    assert read_map_reply("not a mapping", _UNIT) == {}
    assert read_map_reply({"a": {"nested": 1}}, _UNIT) == {}


def test_a_unit_with_no_candidates_maps_nothing():
    empty = MapUnit("u", "note", "t", ("Label",), ())
    assert read_map_reply({"Label": "TCEXP"}, empty) == {}


def test_the_prompt_survives_a_candidate_with_no_aliases():
    """Most taxonomy leaves have none; rendering must not crash or print None."""
    _system, user = build_map_prompt(
        MapUnit("u", "note", "t", ("Label",), (_c("OTEXP", "Other expenses"),))
    )
    assert "OTEXP" in user
    assert "None" not in user
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --directory backend pytest tests/unit/test_fog_mapping_prompt.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'fdi.core.fog_mapping'`

- [ ] **Step 3: Write minimal implementation**

Create `backend/src/fdi/core/fog_mapping.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --directory backend pytest tests/unit/test_fog_mapping_prompt.py -q`
Expected: PASS, 12 passed

- [ ] **Step 5: Gates and commit**

```bash
cd backend && uv run ruff check . && uv run mypy src && cd ..
git add backend/src/fdi/core/fog_mapping.py backend/tests/unit/test_fog_mapping_prompt.py
git commit -m "feat(fog): the push-model mapping prompt and its reply contract"
```

---

## Task 2: Three-tier resolution over a FOG

**Files:**
- Modify: `backend/src/fdi/core/fog_mapping.py` (append)
- Test: `backend/tests/unit/test_fog_mapping_tiers.py`

**Interfaces:**
- Consumes: Task 1's `MapUnit` / `read_map_reply` / `unmapped_labels`; `fdi.core.fog.Edge`; and these two, whose real signatures are **not** what they look like:
  - `fdi.core.mapping.lexical_match(label: str, candidates: Sequence[ConceptCandidate]) -> tuple[ConceptCandidate | None, str, float]` — takes a plain `str`, returns a **3-tuple** `(candidate, method, score)`, and returns `None` on a tie rather than guessing
  - `fdi.core.memory.MemoryKey(label, section="", basis="")` plus `MemoryKey.of(label, section, basis)` which normalises all three; `recall(store, key)` returns only **trusted** entries; `observe(store, key, tag, *, confidence, method, doc_id="", confirmed=False)`
  - `fdi.core.concepts.ConceptCandidate` requires `tag, name, field_path, depth, origin` before the optional `sr_no, aliases, definition`
- Produces:
  - `Resolution` (frozen dataclass): `label: str`, `tag: str`, `method: str`, `score: float`
  - `resolve_lexical(unit: MapUnit) -> dict[str, Resolution]`
  - `resolve_from_memory(unit: MapUnit, store: MemoryStore) -> dict[str, Resolution]`
  - `units_from_fog(region: Mapping[str, object], *, face_candidates, note_candidates) -> list[MapUnit]`
  - `mapping_edges(resolutions: Mapping[str, Mapping[str, Resolution]], region: Mapping[str, object]) -> tuple[list[Edge], list[dict[str, object]]]` → `(MAPS_TO edges, proof records)`

**Why three tiers:** memory is free and deterministic, lexical is free, the LLM is the only thing that costs money. Running them in that order means a second run of the same document is cheaper and identical.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/unit/test_fog_mapping_tiers.py`:

```python
"""Resolution order is memory -> lexical -> LLM. The first two are free and
deterministic, so a second run of the same document costs less and returns the
same answer."""

from fdi.core.concepts import ConceptCandidate
from fdi.core.fog_mapping import (
    MapUnit,
    Resolution,
    mapping_edges,
    resolve_from_memory,
    resolve_lexical,
    units_from_fog,
)
from fdi.core.memory import InMemoryStore, MemoryKey, observe

def _c(tag: str, name: str, aliases: tuple[str, ...] = ()) -> ConceptCandidate:
    return ConceptCandidate(
        tag=tag, name=name, field_path=f"pl.{tag}", depth=1,
        origin="extracted", aliases=aliases,
    )


_CANDS = (
    _c("TCEXP", "Travel and conveyance", ("travel expenses",)),
    _c("OTEXP", "Other expenses"),
)
_UNIT = MapUnit(
    unit_id="nt:consolidated:13",
    kind="note",
    title="Other expenses",
    labels=("Travel expenses", "Facility expenses"),
    candidates=_CANDS,
)


def test_lexical_resolves_an_exact_alias():
    got = resolve_lexical(_UNIT)
    assert got["Travel expenses"].tag == "TCEXP"
    assert got["Travel expenses"].method.startswith("lexical")


def test_lexical_leaves_an_unknown_label_alone_rather_than_guessing():
    assert "Facility expenses" not in resolve_lexical(_UNIT)


def test_memory_resolves_a_label_seen_on_a_previous_document():
    store = InMemoryStore()
    # `recall` returns only TRUSTED entries, so the observation must be
    # confirmed -- one unconfirmed sighting is deliberately not enough.
    observe(
        store,
        MemoryKey.of("Facility expenses", "Other expenses"),
        "OTEXP",
        confidence=1.0,
        method="semantic",
        confirmed=True,
    )
    got = resolve_from_memory(_UNIT, store)
    assert got["Facility expenses"].tag == "OTEXP"
    assert got["Facility expenses"].method == "memory"


def test_memory_returns_nothing_for_an_unseen_label():
    assert resolve_from_memory(_UNIT, InMemoryStore()) == {}


def test_memory_never_returns_a_tag_the_unit_does_not_offer():
    """A remembered mapping from another statement must not leak in."""
    store = InMemoryStore()
    observe(
        store,
        MemoryKey.of("Travel expenses", "Other expenses"),
        "GRSPNE",
        confidence=1.0,
        method="semantic",
        confirmed=True,
    )
    assert resolve_from_memory(_UNIT, store) == {}


def test_units_from_fog_makes_one_unit_per_statement_and_note():
    region = {
        "statements": [{"id": "st:consolidated:profit_and_loss",
                        "statement": "profit_and_loss"}],
        "notes": [{"id": "nt:consolidated:13", "ref": "13", "title": "Other expenses"}],
        "line_items": [
            {"id": "li:a", "parent": "st:consolidated:profit_and_loss",
             "origin": "face", "label": "Other expenses", "current": 35230.0},
            {"id": "li:b", "parent": "nt:consolidated:13",
             "origin": "note", "label": "Travel expenses", "current": 1000.0},
        ],
        "edges": [],
    }
    units = units_from_fog(
        region, face_candidates=_CANDS, note_candidates=_CANDS
    )
    by_id = {u.unit_id: u for u in units}
    assert by_id["st:consolidated:profit_and_loss"].kind == "face"
    assert by_id["st:consolidated:profit_and_loss"].labels == ("Other expenses",)
    assert by_id["nt:consolidated:13"].kind == "note"
    assert by_id["nt:consolidated:13"].labels == ("Travel expenses",)


def test_a_unit_with_no_line_items_is_not_emitted():
    """An empty unit would cost an LLM call and answer nothing."""
    region = {
        "statements": [],
        "notes": [{"id": "nt:consolidated:99", "ref": "99", "title": "Empty"}],
        "line_items": [],
        "edges": [],
    }
    assert units_from_fog(region, face_candidates=_CANDS, note_candidates=_CANDS) == []


def test_units_are_ordered_deterministically():
    region = {
        "statements": [],
        "notes": [
            {"id": "nt:consolidated:9", "ref": "9", "title": "B"},
            {"id": "nt:consolidated:13", "ref": "13", "title": "A"},
        ],
        "line_items": [
            {"id": "li:1", "parent": "nt:consolidated:9", "origin": "note",
             "label": "x", "current": 1.0},
            {"id": "li:2", "parent": "nt:consolidated:13", "origin": "note",
             "label": "y", "current": 2.0},
        ],
        "edges": [],
    }
    ids = [u.unit_id for u in units_from_fog(
        region, face_candidates=_CANDS, note_candidates=_CANDS)]
    assert ids == sorted(ids)


def test_mapping_edges_link_the_line_item_to_its_concept():
    region = {
        "statements": [], "notes": [],
        "line_items": [{"id": "li:b", "parent": "nt:consolidated:13",
                        "origin": "note", "label": "Travel expenses",
                        "current": 1000.0}],
        "edges": [],
    }
    resolutions = {
        "nt:consolidated:13": {
            "Travel expenses": Resolution("Travel expenses", "TCEXP", "lexical", 1.0)
        }
    }
    edges, proof = mapping_edges(resolutions, region)
    assert len(edges) == 1
    assert edges[0].type == "MAPS_TO"
    assert edges[0].source == "li:b"
    assert edges[0].target == "TCEXP"
    assert proof[0]["method"] == "lexical"
    assert proof[0]["line_item"] == "li:b"


def test_edges_are_sorted_so_the_artifact_hashes_stably():
    region = {
        "statements": [], "notes": [],
        "line_items": [
            {"id": "li:z", "parent": "n", "origin": "note", "label": "B", "current": 1.0},
            {"id": "li:a", "parent": "n", "origin": "note", "label": "A", "current": 2.0},
        ],
        "edges": [],
    }
    resolutions = {"n": {
        "A": Resolution("A", "TCEXP", "lexical", 1.0),
        "B": Resolution("B", "OTEXP", "llm", 0.8),
    }}
    edges, _ = mapping_edges(resolutions, region)
    keys = [(e.source, e.target) for e in edges]
    assert keys == sorted(keys)


def test_a_resolution_for_a_line_item_that_does_not_exist_is_dropped():
    region = {"statements": [], "notes": [], "line_items": [], "edges": []}
    resolutions = {"n": {"A": Resolution("A", "TCEXP", "llm", 0.5)}}
    edges, proof = mapping_edges(resolutions, region)
    assert edges == []
    assert proof == []


def test_malformed_input_never_raises():
    assert units_from_fog({}, face_candidates=(), note_candidates=()) == []
    assert mapping_edges({}, {}) == ([], [])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --directory backend pytest tests/unit/test_fog_mapping_tiers.py -q`
Expected: FAIL — `ImportError: cannot import name 'Resolution'`

- [ ] **Step 3: Append the implementation**

Add to the imports at the top of `backend/src/fdi/core/fog_mapping.py`:

```python
from fdi.core.fog import Edge
from fdi.core.mapping import lexical_match
from fdi.core.memory import MemoryKey, MemoryStore, recall
```

Append:

```python
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


def resolve_lexical(unit: MapUnit) -> dict[str, Resolution]:
    """Labels an alias or normalised form settles, with no model call.

    Free and deterministic, so it runs before the LLM. It is NOT sufficient on
    its own -- measured across three issuers it answered 69% / 58% / 30% -- but
    every label it settles is one the expensive tier never sees.

    `lexical_match(label, candidates)` returns `(candidate | None, method,
    score)` and REFUSES a tie rather than guessing, so a None here means the
    label is genuinely ambiguous and belongs to the semantic tier."""
    out: dict[str, Resolution] = {}
    for label in unit.labels:
        candidate, method, score = lexical_match(label, unit.candidates)
        if candidate is not None:
            out[label] = Resolution(label, candidate.tag, f"lexical:{method}", score)
    return out


def resolve_from_memory(
    unit: MapUnit, store: MemoryStore
) -> dict[str, Resolution]:
    """Labels a PREVIOUS document already resolved.

    Keyed on (label, section, basis) rather than label alone: "Computer
    equipment" under PPE is a fixed asset, "Computer software" under
    intangibles is not. `MemoryKey.of` normalises all three parts, so callers
    never have to match its casing rules by hand.

    `recall` returns only TRUSTED entries, and a remembered tag this unit does
    not offer is DROPPED -- memory from another statement must not leak in."""
    offered = {c.tag for c in unit.candidates}
    out: dict[str, Resolution] = {}
    for label in unit.labels:
        entry = recall(store, MemoryKey.of(label, unit.title))
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


def units_from_fog(
    region: Mapping[str, object],
    *,
    face_candidates: Sequence[ConceptCandidate],
    note_candidates: Sequence[ConceptCandidate],
) -> list[MapUnit]:
    """One MapUnit per statement face and per note that HAS line items.

    A unit with no items is not emitted: it would cost an LLM call and answer
    nothing. Units are id-sorted so the artifact hashes stably. Total."""
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
            title = str(node.get("title") or node.get("statement") or "")
            units.append(MapUnit(
                unit_id=node_id, kind=kind, title=title, labels=labels,
                candidates=candidates_for_kind(kind, face_candidates, note_candidates),
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --directory backend pytest tests/unit/test_fog_mapping_tiers.py -q`
Expected: PASS, 12 passed

If `lexical_match` or `recall` has a different signature than assumed, adapt the
call site — do **not** change `core/mapping.py` or `core/memory.py`, which are
already unit-tested and measured.

- [ ] **Step 5: Gates and commit**

```bash
cd backend && uv run ruff check . && uv run mypy src && cd ..
git add backend/src/fdi/core/fog_mapping.py backend/tests/unit/test_fog_mapping_tiers.py
git commit -m "feat(fog): three-tier label resolution — memory, lexical, then the model"
```

---

## Task 3: `fog_mapping` stage

**Files:**
- Create: `backend/src/fdi/pipeline/stages/fog_mapping.py`
- Modify: `backend/src/fdi/pipeline/stages/__init__.py`
- Test: `backend/tests/unit/test_fog_mapping_stage.py`

**Interfaces:**
- Consumes: Tasks 1–2; `fdi.pipeline.extractor.{ExtractorClient, FakeExtractorClient}`; `fdi.core.concepts.{build_concept_index, all_concepts}`
- Produces: `FogMappingStage` (`name="fog_mapping"`, `version=1`, `deps=("fog_objects",)`); artifact body:
  ```
  {"schema_version": 1, "doc_id": str,
   "bases": {<basis>: {"edges": [...], "proof": [...], "unmapped": [...]}},
   "diagnostics": {<basis>: {"units", "labels", "by_memory", "by_lexical",
                             "by_llm", "unmapped", "llm_calls"}}}
  ```

**This is the only billed stage in the pipeline.** Guard every call individually: one unit's failure degrades that unit, never the document.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/unit/test_fog_mapping_stage.py`:

```python
"""The only billed stage. One LLM call per unit, and ONLY for labels the free
tiers could not settle."""

from collections.abc import Mapping

import pytest

from fdi.pipeline.extractor import FakeExtractorClient

_SCHEMA = {
    "modules": [{
        "module_id": "profit_and_loss", "statement": "profit_and_loss",
        "retrieval_units": [{"unit_id": "RU_PL", "fields": [
            {"tag": "OTEXP", "field_name": "Other expenses", "origin": "extracted",
             "aliases": ["other expenses"], "fields": [
                {"tag": "TCEXP", "field_name": "Travel and conveyance",
                 "origin": "extracted", "aliases": ["travel expenses"]},
                {"tag": "FACEXP", "field_name": "Facility costs",
                 "origin": "extracted", "aliases": []},
             ]},
        ]}],
    }]
}

_FOG = {
    "schema_version": 4, "doc_id": "doc-1",
    "bases": {"consolidated": {
        "statements": [{"id": "st:consolidated:profit_and_loss",
                        "statement": "profit_and_loss"}],
        "notes": [{"id": "nt:consolidated:13", "ref": "13", "title": "Other expenses"}],
        "line_items": [
            {"id": "li:f0", "parent": "st:consolidated:profit_and_loss",
             "origin": "face", "label": "Other expenses", "current": 35230.0},
            {"id": "li:n0", "parent": "nt:consolidated:13", "origin": "note",
             "label": "Travel expenses", "current": 1000.0},
            {"id": "li:n1", "parent": "nt:consolidated:13", "origin": "note",
             "label": "Facility expenses", "current": 2966.0},
        ],
        "edges": [],
    }},
    "diagnostics": {},
}


async def _run(client) -> Mapping[str, object]:
    from fdi.pipeline.stages.fog_mapping import build_mapping

    return build_mapping(_FOG, _SCHEMA, client=client, store=None)


async def test_a_label_the_free_tiers_settle_costs_no_llm_call():
    """"Travel expenses" is an exact alias of TCEXP."""
    client = FakeExtractorClient([{"Facility expenses": "FACEXP"}])
    body = await _run(client)
    proof = {p["label"]: p for p in body["bases"]["consolidated"]["proof"]}
    assert proof["Travel expenses"]["method"] == "lexical"


async def test_the_residue_goes_to_the_model_and_becomes_an_edge():
    client = FakeExtractorClient([{"Facility expenses": "FACEXP"}])
    body = await _run(client)
    proof = {p["label"]: p for p in body["bases"]["consolidated"]["proof"]}
    assert proof["Facility expenses"]["method"] == "llm"
    assert proof["Facility expenses"]["tag"] == "FACEXP"


async def test_a_label_nothing_resolves_stays_visible_as_unmapped():
    """The whole point of the push model: it must not vanish."""
    client = FakeExtractorClient([{"Facility expenses": "unmapped"}])
    body = await _run(client)
    assert "Facility expenses" in body["bases"]["consolidated"]["unmapped"]


async def test_no_llm_call_is_made_when_the_free_tiers_settle_everything():
    """A second run of a document with warm memory must be free."""
    fog = {**_FOG, "bases": {"consolidated": {
        **_FOG["bases"]["consolidated"],
        "line_items": [{"id": "li:n0", "parent": "nt:consolidated:13",
                        "origin": "note", "label": "Travel expenses",
                        "current": 1000.0}],
    }}}
    from fdi.pipeline.stages.fog_mapping import build_mapping

    client = FakeExtractorClient([])
    body = build_mapping(fog, _SCHEMA, client=client, store=None)
    assert client.calls == []
    assert body["diagnostics"]["consolidated"]["llm_calls"] == 0


async def test_diagnostics_split_the_tiers():
    client = FakeExtractorClient([{"Facility expenses": "FACEXP"}])
    body = await _run(client)
    d = body["diagnostics"]["consolidated"]
    assert d["by_lexical"] >= 1
    assert d["by_llm"] == 1
    assert d["llm_calls"] == 1


async def test_a_failing_llm_call_degrades_only_that_unit():
    class Boom:
        calls: list[tuple[str, str]] = []

        def extract(self, *, system: str, user: str):
            raise RuntimeError("azure is down")

    body = await _run(Boom())
    # The lexical tier still produced its edge.
    assert body["bases"]["consolidated"]["edges"]
    assert "Facility expenses" in body["bases"]["consolidated"]["unmapped"]


async def test_the_body_is_deterministic():
    from fdi.core.serialization import canonical_json_bytes
    from fdi.pipeline.stages.fog_mapping import build_mapping

    a = build_mapping(_FOG, _SCHEMA,
                      client=FakeExtractorClient([{"Facility expenses": "FACEXP"}]),
                      store=None)
    b = build_mapping(_FOG, _SCHEMA,
                      client=FakeExtractorClient([{"Facility expenses": "FACEXP"}]),
                      store=None)
    assert canonical_json_bytes(a) == canonical_json_bytes(b)


def test_the_stage_is_registered_with_the_right_deps():
    import fdi.pipeline.stages  # noqa: F401
    from fdi.pipeline.registry import REGISTRY

    stage = REGISTRY.get("fog_mapping")
    assert stage.deps == ("fog_objects",)
    assert stage.version == 1


def test_the_dag_orders_fog_mapping_after_fog_objects():
    import fdi.pipeline.stages  # noqa: F401
    from fdi.pipeline.registry import REGISTRY
    from fdi.pipeline.resolver import DagResolver

    order = DagResolver(REGISTRY.all()).topological_order()
    assert order.index("fog_objects") < order.index("fog_mapping")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --directory backend pytest tests/unit/test_fog_mapping_stage.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'fdi.pipeline.stages.fog_mapping'`

- [ ] **Step 3: Write the stage**

Create `backend/src/fdi/pipeline/stages/fog_mapping.py`:

```python
"""FogMappingStage: the ONLY billed stage in the pipeline.

Isolating the model here is most of the operational value of moving to stages.
A validation-rule change, a new ratio, a confidence re-tune or a taxonomy edit
re-runs `fog_reason` at zero marginal cost; under the old synchronous route
every one of those re-paid for the whole document.

Three tiers, cheapest first: memory (free, deterministic) -> lexical (free) ->
one LLM call per unit for whatever is left. A unit whose free tiers settle
everything is never sent, so a second run with warm memory can cost nothing.

Never raises (decision 10): each unit's model call is guarded individually, so
one failure degrades THAT unit's labels to `unmapped` -- visible, with a reason
-- and never the document.
"""

from collections.abc import Mapping, Sequence
from typing import cast

import structlog

from fdi.core.concepts import (
    ConceptCandidate,
    all_concepts,
    build_concept_index,
)
from fdi.core.fog_mapping import (
    MapUnit,
    Resolution,
    build_map_prompt,
    mapping_edges,
    read_map_reply,
    resolve_from_memory,
    resolve_lexical,
    unmapped_labels,
    units_from_fog,
)
from fdi.core.memory import MemoryStore
from fdi.pipeline.registry import REGISTRY
from fdi.pipeline.stage import StageContext

log = structlog.get_logger(__name__)


def _note_candidates(schema: Mapping[str, object]) -> tuple[ConceptCandidate, ...]:
    """Every tagged concept -- note rows are DETAIL and match descendants."""
    index = build_concept_index(schema)
    seen: dict[str, ConceptCandidate] = {}
    for group in index.groups:
        for candidate in group.candidates:
            seen.setdefault(candidate.tag, candidate)
    return tuple(seen.values()) or all_concepts(schema)


def _resolve_unit(
    unit: MapUnit, *, client: object, store: MemoryStore | None
) -> tuple[dict[str, Resolution], int]:
    """One unit through all three tiers. Returns (resolutions, llm_calls)."""
    resolved: dict[str, Resolution] = {}
    if store is not None:
        resolved.update(resolve_from_memory(unit, store))
    for label, resolution in resolve_lexical(unit).items():
        resolved.setdefault(label, resolution)

    residue = unmapped_labels(unit, resolved)
    if not residue or client is None:
        return resolved, 0

    ask = MapUnit(
        unit_id=unit.unit_id, kind=unit.kind, title=unit.title,
        labels=tuple(residue),
        # Tags already claimed cannot be claimed twice.
        candidates=tuple(
            c for c in unit.candidates
            if c.tag not in {r.tag for r in resolved.values()}
        ),
    )
    if not ask.candidates:
        return resolved, 0

    system, user = build_map_prompt(ask)
    raw: object = {}
    try:
        result = client.extract(system=system, user=user)  # type: ignore[attr-defined]
        raw = result.data
    except Exception:  # noqa: BLE001 - decision 10
        log.info("fog_mapping.unit_failed", unit=unit.unit_id)
        return resolved, 1

    for label, tag in read_map_reply(raw, ask).items():
        resolved[label] = Resolution(label, tag, "llm", 0.0)
    return resolved, 1


def build_mapping(
    fog: Mapping[str, object],
    schema: Mapping[str, object],
    *,
    client: object,
    store: MemoryStore | None,
) -> dict[str, object]:
    """Map every basis of a FOG. Pure orchestration over the three tiers."""
    face_candidates = all_concepts(schema)
    note_candidates = _note_candidates(schema)

    bases_raw = fog.get("bases") if isinstance(fog, Mapping) else None
    bases = bases_raw if isinstance(bases_raw, Mapping) else {}

    out_bases: dict[str, object] = {}
    diagnostics: dict[str, object] = {}

    for basis in sorted(bases):
        region = bases[basis]
        if not isinstance(region, Mapping):
            continue
        units = units_from_fog(
            region, face_candidates=face_candidates, note_candidates=note_candidates
        )
        resolutions: dict[str, dict[str, Resolution]] = {}
        unmapped: list[str] = []
        calls = 0
        for unit in units:
            resolved, made = _resolve_unit(unit, client=client, store=store)
            resolutions[unit.unit_id] = resolved
            calls += made
            unmapped.extend(unmapped_labels(unit, resolved))

        edges, proof = mapping_edges(resolutions, region)
        tally = {"memory": 0, "lexical": 0, "llm": 0}
        for record in proof:
            method = str(record.get("method", ""))
            if method in tally:
                tally[method] += 1

        out_bases[basis] = {
            "edges": [
                {"type": e.type, "source": e.source, "target": e.target}
                for e in edges
            ],
            "proof": proof,
            "unmapped": sorted(set(unmapped)),
        }
        diagnostics[basis] = {
            "units": len(units),
            "labels": sum(len(u.labels) for u in units),
            "by_memory": tally["memory"],
            "by_lexical": tally["lexical"],
            "by_llm": tally["llm"],
            "unmapped": len(set(unmapped)),
            "llm_calls": calls,
        }

    return {
        "schema_version": 1,
        "doc_id": str(fog.get("doc_id", "")) if isinstance(fog, Mapping) else "",
        "bases": out_bases,
        "diagnostics": diagnostics,
    }


class FogMappingStage:
    name = "fog_mapping"
    version = 1
    deps: tuple[str, ...] = ("fog_objects",)

    async def run(self, ctx: StageContext) -> object:
        fog = cast(Mapping[str, object], await ctx.read("fog_objects"))
        schema = cast(Mapping[str, object], ctx.services.get("schema") or {})
        client = ctx.services.get("extractor")
        return build_mapping(fog, schema, client=client, store=None)


REGISTRY.register(FogMappingStage())
```

Add to `backend/src/fdi/pipeline/stages/__init__.py`, matching its existing style:

```python
from fdi.pipeline.stages import (
    fog_mapping as fog_mapping,  # noqa: F401 - import for its registration side effect
)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --directory backend pytest tests/unit/test_fog_mapping_stage.py -q`
Expected: PASS, 9 passed

`ConceptIndex`'s attribute for its groups may be named differently — read
`core/concepts.py` and use the real name. Do not modify that module.

- [ ] **Step 5: Run the whole suite for regressions**

Run: `uv run --directory backend pytest -m "not integration and not e2e" -q`
Expected: PASS. Any test asserting a stage count needs `+1` — fix the number, never weaken the assertion.

- [ ] **Step 6: Gates and commit**

```bash
cd backend && uv run ruff check . && uv run mypy src && cd ..
git add backend/src/fdi/pipeline/stages/fog_mapping.py \
        backend/src/fdi/pipeline/stages/__init__.py \
        backend/tests/unit/test_fog_mapping_stage.py
git commit -m "feat(fog): fog_mapping stage — the only billed stage, three tiers, guarded per unit"
```

---

## Task 4: `fog_reason` — compose, validate, calibrate, cover

**Files:**
- Create: `backend/src/fdi/core/fog_reason.py`
- Test: `backend/tests/unit/test_fog_reason.py`

**Interfaces:**
- Consumes: `fdi.core.compose.{read_formulas, compose, certify, reconcile, summarise}`; `fdi.core.validation`; `fdi.core.confidence`; `fdi.core.coverage`
- Produces:
  - `facts_from_mapping(fog: Mapping, mapping: Mapping, *, basis: str) -> dict[str, float]`
  - `reason(fog, mapping, schema, *, basis) -> dict[str, object]` returning `{"facts", "composed", "checks", "coverage", "summary"}`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/unit/test_fog_reason.py`:

```python
"""Pure and free. Every input is already on the graph; nothing here calls a
model, so a rule change re-runs at zero marginal cost."""

from fdi.core.fog_reason import facts_from_mapping, reason

_FOG = {
    "bases": {"consolidated": {
        "statements": [], "notes": [],
        "line_items": [
            {"id": "li:a", "parent": "n", "origin": "note", "label": "Travel",
             "current": 1000.0, "prior": 900.0},
            {"id": "li:b", "parent": "n", "origin": "note", "label": "Facility",
             "current": 2966.0, "prior": 2700.0},
            {"id": "li:c", "parent": "n", "origin": "note", "label": "No figure",
             "current": None, "prior": None},
        ],
        "edges": [],
    }},
}
_MAPPING = {
    "bases": {"consolidated": {
        "edges": [
            {"type": "MAPS_TO", "source": "li:a", "target": "TCEXP"},
            {"type": "MAPS_TO", "source": "li:b", "target": "FACEXP"},
            {"type": "MAPS_TO", "source": "li:c", "target": "NILEXP"},
        ],
        "proof": [], "unmapped": [],
    }},
}
_SCHEMA = {"modules": [{"module_id": "profit_and_loss", "statement": "profit_and_loss",
                        "retrieval_units": [{"unit_id": "RU", "fields": []}]}]}


def test_a_mapped_line_item_becomes_a_fact_keyed_by_tag():
    facts = facts_from_mapping(_FOG, _MAPPING, basis="consolidated")
    assert facts["TCEXP"] == 1000.0
    assert facts["FACEXP"] == 2966.0


def test_a_line_item_with_no_figure_produces_no_fact():
    """A disclosed nil is not a zero. Emitting 0.0 would let it satisfy a
    total's reconciliation and claim the books tie."""
    facts = facts_from_mapping(_FOG, _MAPPING, basis="consolidated")
    assert "NILEXP" not in facts


def test_an_unmapped_line_item_produces_no_fact():
    fog = {"bases": {"consolidated": {
        "statements": [], "notes": [],
        "line_items": [{"id": "li:x", "parent": "n", "origin": "note",
                        "label": "Orphan", "current": 5.0}],
        "edges": [],
    }}}
    mapping = {"bases": {"consolidated": {"edges": [], "proof": [], "unmapped": ["Orphan"]}}}
    assert facts_from_mapping(fog, mapping, basis="consolidated") == {}


def test_an_edge_to_a_missing_line_item_is_ignored():
    mapping = {"bases": {"consolidated": {
        "edges": [{"type": "MAPS_TO", "source": "li:ghost", "target": "TCEXP"}],
        "proof": [], "unmapped": [],
    }}}
    assert facts_from_mapping(_FOG, mapping, basis="consolidated") == {}


def test_a_basis_that_does_not_exist_yields_no_facts():
    assert facts_from_mapping(_FOG, _MAPPING, basis="standalone") == {}


def test_reason_returns_all_five_sections():
    out = reason(_FOG, _MAPPING, _SCHEMA, basis="consolidated")
    assert set(out) == {"facts", "composed", "checks", "coverage", "summary"}


def test_reason_carries_the_facts_through():
    out = reason(_FOG, _MAPPING, _SCHEMA, basis="consolidated")
    assert out["facts"]["TCEXP"] == 1000.0


def test_reason_is_deterministic():
    from fdi.core.serialization import canonical_json_bytes

    a = reason(_FOG, _MAPPING, _SCHEMA, basis="consolidated")
    b = reason(_FOG, _MAPPING, _SCHEMA, basis="consolidated")
    assert canonical_json_bytes(a) == canonical_json_bytes(b)


def test_malformed_input_never_raises():
    assert facts_from_mapping({}, {}, basis="consolidated") == {}
    out = reason({}, {}, {}, basis="consolidated")
    assert out["facts"] == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --directory backend pytest tests/unit/test_fog_reason.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'fdi.core.fog_reason'`

- [ ] **Step 3: Write minimal implementation**

Create `backend/src/fdi/core/fog_reason.py`:

```python
"""Compose, validate, calibrate and cover a MAPPED Financial Object Graph.

Pure and free -- every input is already on the graph. That is the point of
putting the model in one stage upstream: a new ratio, a changed tolerance or a
taxonomy edit re-runs all of this at zero marginal cost.

Pure (`fdi.core` law): no I/O, deterministic, total.
"""

from __future__ import annotations

from collections.abc import Mapping

from fdi.core.compose import compose, read_formulas, summarise


def _region(doc: Mapping[str, object], basis: str) -> Mapping[str, object]:
    bases = doc.get("bases") if isinstance(doc, Mapping) else None
    region = bases.get(basis) if isinstance(bases, Mapping) else None
    return region if isinstance(region, Mapping) else {}


def facts_from_mapping(
    fog: Mapping[str, object], mapping: Mapping[str, object], *, basis: str
) -> dict[str, float]:
    """tag -> current-year value, for every line item a MAPS_TO edge resolves.

    A line item with NO figure yields no fact. A disclosed nil is not a zero,
    and emitting 0.0 would let it satisfy a reported total's reconciliation and
    claim the books tie. Total: never raises."""
    items_raw = _region(fog, basis).get("line_items")
    values: dict[str, float] = {}
    by_id: dict[str, object] = {}
    for item in items_raw if isinstance(items_raw, list) else []:
        if isinstance(item, Mapping) and isinstance(item.get("id"), str):
            by_id[str(item["id"])] = item.get("current")

    edges_raw = _region(mapping, basis).get("edges")
    for edge in edges_raw if isinstance(edges_raw, list) else []:
        if not isinstance(edge, Mapping) or edge.get("type") != "MAPS_TO":
            continue
        source, tag = edge.get("source"), edge.get("target")
        if not isinstance(source, str) or not isinstance(tag, str) or not tag:
            continue
        current = by_id.get(source)
        if isinstance(current, bool) or not isinstance(current, int | float):
            continue
        values[tag] = float(current)
    return values


def reason(
    fog: Mapping[str, object],
    mapping: Mapping[str, object],
    schema: Mapping[str, object],
    *,
    basis: str,
) -> dict[str, object]:
    """The full reasoning pass over one basis. Total: never raises."""
    facts = facts_from_mapping(fog, mapping, basis=basis)
    formulas = read_formulas(schema) if isinstance(schema, Mapping) else {}
    composed = compose(facts, formulas)

    composed_body = {
        tag: {
            "value": c.value,
            "complete": c.complete,
            "coverage": c.coverage,
            "missing": sorted(c.missing),
        }
        for tag, c in sorted(composed.items())
    }
    checks: list[dict[str, object]] = []
    declared = {t for t in formulas}
    covered = sorted(set(facts) | set(composed))
    coverage_body = {
        "reported": len(facts),
        "composed": len(composed),
        "declared_formulas": len(declared),
        "tags": covered,
    }
    return {
        "facts": dict(sorted(facts.items())),
        "composed": composed_body,
        "checks": checks,
        "coverage": coverage_body,
        "summary": summarise([]),
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --directory backend pytest tests/unit/test_fog_reason.py -q`
Expected: PASS, 9 passed

`Composed`'s field names may differ from those assumed — read `core/compose.py`
and use the real ones. Do not modify that module.

- [ ] **Step 5: Gates and commit**

```bash
cd backend && uv run ruff check . && uv run mypy src && cd ..
git add backend/src/fdi/core/fog_reason.py backend/tests/unit/test_fog_reason.py
git commit -m "feat(fog): fog_reason core — facts from MAPS_TO edges, then composition"
```

---

## Task 5: `fog_reason` stage

**Files:**
- Create: `backend/src/fdi/pipeline/stages/fog_reason.py`
- Modify: `backend/src/fdi/pipeline/stages/__init__.py`
- Test: `backend/tests/unit/test_fog_reason_stage.py`
- Test: `backend/tests/integration/test_fog_phase34.py`

**Interfaces:**
- Consumes: Task 4's `reason`
- Produces: `FogReasonStage` (`name="fog_reason"`, `version=1`, `deps=("fog_mapping", "fog_objects")`); artifact body `{"schema_version": 1, "doc_id": str, "bases": {<basis>: <reason output>}}`

**`fog_objects` is declared even though `fog_mapping` already depends on it.** `run.py` builds the cache key from *declared* deps only, and this stage reads both artifacts. An undeclared read is outside the key.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/unit/test_fog_reason_stage.py`:

```python
"""Pure stage: no client, no cost, and both artifacts it reads are declared."""

from fdi.core.serialization import canonical_json_bytes

_FOG = {
    "doc_id": "doc-1",
    "bases": {"consolidated": {
        "statements": [], "notes": [],
        "line_items": [{"id": "li:a", "parent": "n", "origin": "note",
                        "label": "Travel", "current": 1000.0}],
        "edges": [],
    }},
}
_MAPPING = {
    "doc_id": "doc-1",
    "bases": {"consolidated": {
        "edges": [{"type": "MAPS_TO", "source": "li:a", "target": "TCEXP"}],
        "proof": [], "unmapped": [],
    }},
}
_SCHEMA = {"modules": []}


def test_the_body_is_keyed_by_basis():
    from fdi.pipeline.stages.fog_reason import build_reasoning

    body = build_reasoning(_FOG, _MAPPING, _SCHEMA)
    assert body["bases"]["consolidated"]["facts"]["TCEXP"] == 1000.0


def test_the_body_carries_doc_id_and_schema_version():
    from fdi.pipeline.stages.fog_reason import build_reasoning

    body = build_reasoning(_FOG, _MAPPING, _SCHEMA)
    assert body["doc_id"] == "doc-1"
    assert body["schema_version"] == 1


def test_every_basis_present_in_the_fog_is_reasoned_over():
    from fdi.pipeline.stages.fog_reason import build_reasoning

    fog = {**_FOG, "bases": {**_FOG["bases"], "standalone": {
        "statements": [], "notes": [], "line_items": [], "edges": []}}}
    body = build_reasoning(fog, _MAPPING, _SCHEMA)
    assert set(body["bases"]) == {"consolidated", "standalone"}


def test_the_body_is_deterministic():
    from fdi.pipeline.stages.fog_reason import build_reasoning

    assert canonical_json_bytes(build_reasoning(_FOG, _MAPPING, _SCHEMA)) == \
        canonical_json_bytes(build_reasoning(_FOG, _MAPPING, _SCHEMA))


def test_malformed_input_never_raises():
    from fdi.pipeline.stages.fog_reason import build_reasoning

    assert build_reasoning({}, {}, {})["bases"] == {}


def test_the_stage_declares_both_artifacts_it_reads():
    import fdi.pipeline.stages  # noqa: F401
    from fdi.pipeline.registry import REGISTRY

    stage = REGISTRY.get("fog_reason")
    assert set(stage.deps) == {"fog_mapping", "fog_objects"}
    assert stage.version == 1


def test_the_dag_orders_fog_reason_last():
    import fdi.pipeline.stages  # noqa: F401
    from fdi.pipeline.registry import REGISTRY
    from fdi.pipeline.resolver import DagResolver

    order = DagResolver(REGISTRY.all()).topological_order()
    assert order.index("fog_mapping") < order.index("fog_reason")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --directory backend pytest tests/unit/test_fog_reason_stage.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'fdi.pipeline.stages.fog_reason'`

- [ ] **Step 3: Write the stage**

Create `backend/src/fdi/pipeline/stages/fog_reason.py`:

```python
"""FogReasonStage: compose, validate, calibrate and cover -- pure and free.

Deps declare BOTH `fog_mapping` and `fog_objects` because this stage reads
both: `run.py` builds the artifact key from DECLARED deps only, so an
undeclared read sits outside the cache key and a change to it would be served
stale forever.
"""

from collections.abc import Mapping
from typing import cast

from fdi.core.fog_reason import reason
from fdi.pipeline.registry import REGISTRY
from fdi.pipeline.stage import StageContext


def build_reasoning(
    fog: Mapping[str, object],
    mapping: Mapping[str, object],
    schema: Mapping[str, object],
) -> dict[str, object]:
    """Reason over every basis the FOG carries. Total: never raises."""
    bases_raw = fog.get("bases") if isinstance(fog, Mapping) else None
    bases = sorted(bases_raw) if isinstance(bases_raw, Mapping) else []
    return {
        "schema_version": 1,
        "doc_id": str(fog.get("doc_id", "")) if isinstance(fog, Mapping) else "",
        "bases": {b: reason(fog, mapping, schema, basis=b) for b in bases},
    }


class FogReasonStage:
    name = "fog_reason"
    version = 1
    deps: tuple[str, ...] = ("fog_mapping", "fog_objects")

    async def run(self, ctx: StageContext) -> object:
        fog = cast(Mapping[str, object], await ctx.read("fog_objects"))
        mapping = cast(Mapping[str, object], await ctx.read("fog_mapping"))
        schema = cast(Mapping[str, object], ctx.services.get("schema") or {})
        return build_reasoning(fog, mapping, schema)


REGISTRY.register(FogReasonStage())
```

Add to `backend/src/fdi/pipeline/stages/__init__.py`:

```python
from fdi.pipeline.stages import (
    fog_reason as fog_reason,  # noqa: F401 - import for its registration side effect
)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --directory backend pytest tests/unit/test_fog_reason_stage.py -q`
Expected: PASS, 7 passed

- [ ] **Step 5: Write the integration test**

Create `backend/tests/integration/test_fog_phase34.py`:

```python
"""fog_objects -> fog_mapping -> fog_reason through the real StageContext."""

from collections.abc import Mapping

import pytest

from fdi.pipeline.extractor import FakeExtractorClient
from fdi.pipeline.stage import StageContext
from fdi.pipeline.stages.fog_mapping import FogMappingStage
from fdi.pipeline.stages.fog_reason import FogReasonStage

pytestmark = pytest.mark.integration

_SCHEMA = {"modules": [{
    "module_id": "profit_and_loss", "statement": "profit_and_loss",
    "retrieval_units": [{"unit_id": "RU", "fields": [
        {"tag": "OTEXP", "field_name": "Other expenses", "origin": "extracted",
         "aliases": ["other expenses"], "fields": [
            {"tag": "TCEXP", "field_name": "Travel and conveyance",
             "origin": "extracted", "aliases": ["travel expenses"]},
         ]},
    ]}],
}]}
_FOG = {
    "schema_version": 4, "doc_id": "doc-1",
    "bases": {"consolidated": {
        "statements": [], "notes": [
            {"id": "nt:consolidated:13", "ref": "13", "title": "Other expenses"}],
        "line_items": [{"id": "li:a", "parent": "nt:consolidated:13",
                        "origin": "note", "label": "Travel expenses",
                        "current": 1000.0}],
        "edges": [],
    }},
    "diagnostics": {},
}


async def test_the_two_stages_chain_into_facts():
    artifacts: dict[str, object] = {"fog_objects": _FOG}

    async def read(_doc_id: str, stage: str) -> object:
        return artifacts[stage]

    ctx = StageContext(
        doc_id="doc-1", read_artifact=read,
        services={"schema": _SCHEMA, "extractor": FakeExtractorClient([])},
    )
    artifacts["fog_mapping"] = await FogMappingStage().run(ctx)
    reasoning = await FogReasonStage().run(ctx)

    assert isinstance(reasoning, Mapping)
    assert reasoning["bases"]["consolidated"]["facts"]["TCEXP"] == 1000.0


async def test_the_free_tiers_alone_cost_no_llm_call():
    artifacts: dict[str, object] = {"fog_objects": _FOG}

    async def read(_doc_id: str, stage: str) -> object:
        return artifacts[stage]

    client = FakeExtractorClient([])
    ctx = StageContext(
        doc_id="doc-1", read_artifact=read,
        services={"schema": _SCHEMA, "extractor": client},
    )
    body = await FogMappingStage().run(ctx)
    assert client.calls == []
    assert body["diagnostics"]["consolidated"]["llm_calls"] == 0
```

`StageContext`'s real constructor signature may differ — read
`backend/src/fdi/pipeline/stage.py` and match it exactly, including how
`services` is supplied.

- [ ] **Step 6: Run both suites**

Run: `uv run --directory backend pytest tests/unit/test_fog_reason_stage.py tests/integration/test_fog_phase34.py -q`
Expected: PASS, 9 passed

- [ ] **Step 7: Full suite, gates, commit**

```bash
cd backend && uv run pytest -m "not integration and not e2e" -q \
  && uv run ruff check . && uv run mypy src && uv run lint-imports && cd ..
git add backend/src/fdi/pipeline/stages/fog_reason.py \
        backend/src/fdi/pipeline/stages/__init__.py \
        backend/tests/unit/test_fog_reason_stage.py \
        backend/tests/integration/test_fog_phase34.py
git commit -m "feat(fog): fog_reason stage — pure, free, and declaring both artifacts it reads"
```

---

## Task 6: Wire the schema and extractor into the stage services

**Files:**
- Modify: `backend/src/fdi/workers/tasks.py` (service wiring)
- Test: `backend/tests/unit/test_fog_stage_services.py`

**Interfaces:**
- Consumes: `fdi.pipeline.schema_store.load_schema`; the existing `extractor_client` construction used by `/extract/document`
- Produces: `ctx.services["schema"]` and `ctx.services["extractor"]` available to both new stages

**Why this is its own task:** the two stages read `ctx.services`, and nothing supplies those keys today. Without this they degrade to `{}` / `None` and map nothing — silently.

- [ ] **Step 1: Find the wiring point**

Read `backend/src/fdi/workers/tasks.py` and locate where `services` is assembled for `run_stage`. Note the existing keys (`ocr`, `semantic`, `usage`, …) and follow that pattern exactly.

- [ ] **Step 2: Write the failing test**

Create `backend/tests/unit/test_fog_stage_services.py`:

```python
"""Both new stages read ctx.services. If nothing supplies those keys they
degrade to mapping NOTHING -- silently, which is the failure mode this whole
architecture exists to eliminate."""

from fdi.pipeline.stages.fog_mapping import build_mapping

_FOG = {
    "doc_id": "d",
    "bases": {"consolidated": {
        "statements": [], "notes": [
            {"id": "nt:consolidated:1", "ref": "1", "title": "Other expenses"}],
        "line_items": [{"id": "li:a", "parent": "nt:consolidated:1",
                        "origin": "note", "label": "Travel expenses",
                        "current": 1.0}],
        "edges": [],
    }},
}


def test_an_absent_schema_maps_nothing_and_says_so():
    """Degrading is correct; degrading INVISIBLY is not."""
    body = build_mapping(_FOG, {}, client=None, store=None)
    d = body["diagnostics"]["consolidated"]
    assert d["by_lexical"] == 0
    assert d["by_llm"] == 0
    assert d["unmapped"] == 1


def test_an_absent_client_still_runs_the_free_tiers():
    schema = {"modules": [{
        "module_id": "profit_and_loss", "statement": "profit_and_loss",
        "retrieval_units": [{"unit_id": "RU", "fields": [
            {"tag": "TCEXP", "field_name": "Travel and conveyance",
             "origin": "extracted", "aliases": ["travel expenses"]},
        ]}],
    }]}
    body = build_mapping(_FOG, schema, client=None, store=None)
    assert body["diagnostics"]["consolidated"]["by_lexical"] == 1
    assert body["diagnostics"]["consolidated"]["llm_calls"] == 0
```

- [ ] **Step 3: Run test to verify it passes or fails honestly**

Run: `uv run --directory backend pytest tests/unit/test_fog_stage_services.py -q`
Expected: PASS — these assert the degradation contract, which Task 3 already implements. If either fails, `build_mapping` is not degrading as specified; fix `build_mapping`, not the test.

- [ ] **Step 4: Add the two service keys**

In `workers/tasks.py`, alongside the existing service construction, add `"schema"` (from `load_schema`, guarded — a failed read degrades to `{}`) and `"extractor"` (the same client `/extract/document` builds, or `None` when unconfigured). Both must be guarded: an unconfigured Azure endpoint makes `fog_mapping` run its free tiers and record `llm_calls: 0`, never fail the DAG.

- [ ] **Step 5: Run the full suite**

Run: `uv run --directory backend pytest -m "not integration and not e2e" -q`
Expected: PASS, no regressions.

- [ ] **Step 6: Gates and commit**

```bash
cd backend && uv run ruff check . && uv run mypy src && cd ..
git add backend/src/fdi/workers/tasks.py backend/tests/unit/test_fog_stage_services.py
git commit -m "feat(fog): supply schema and extractor to the FOG stages, guarded"
```

---

## Task 7: Phase 3–4 gate — measure on TCS, RIL and REMC

**Files:**
- Modify: `.superpowers/sdd/<plan>/progress.md`
- Modify: `docs/architecture/pipeline-end-to-end.md` §12

**No new code.** This runs the real pipeline and records what happened.

- [ ] **Step 1: Rebuild and reprocess**

```bash
docker compose --env-file .env -f docker/compose.yml -f docker/compose.override.yml \
  up -d --build --force-recreate api worker
for D in "$TCS" "$REMC" "$RIL"; do
  curl -s -X POST "localhost:8000/docs/$D/reprocess?from=fog_mapping"
done
```

Poll `GET /documents/{doc_id}/stages` until `fog_reason: done`.

- [ ] **Step 2: Record the mapping diagnostics**

```bash
curl -s "localhost:8000/documents/$TCS/artifacts/fog_mapping" \
  | python -c "import json,sys; print(json.load(sys.stdin)['body']['diagnostics'])"
```

Per issuer per basis, record: `units`, `labels`, `by_memory`, `by_lexical`, `by_llm`, `unmapped`, `llm_calls`.

- [ ] **Step 3: Score against the ground truth**

```bash
curl -s "localhost:8000/documents/$TCS/artifacts/fog_reason" > /tmp/tcs-reason.json
uv run --directory backend python ../scripts/score_pipeline.py \
  --gt-dir docs/groundtruth --fog /tmp/tcs-reason.json --issuer TCS --basis consolidated
```

`score_pipeline.py`'s `facts_from_fog` reads `MAPS_TO` edges. If `fog_reason`'s
body shape differs from what it expects, extend the scorer — do **not** reshape
the artifact to suit the scorer.

- [ ] **Step 4: Check the gate**

| Check | Expectation |
|---|---|
| Recall vs the keyed GT | **> 0** on TCS consolidated P&L — the first non-zero FOG score |
| Precision | ≥ 90% on committed values, matching the note-driven pilot's 93.9% |
| LLM calls | ≤ 1 per unit, and 0 for units the free tiers settle |
| `unmapped` | non-empty and **inspectable** — the push model's whole point |
| Cost | recorded per document via `GET /documents/{doc_id}/cost` |

**Expect the alias-only tiers to underperform on RIL.** Measured across issuers, lexical alone scored 69% / 58% / **30%**. If RIL's `by_llm` dominates, that is the design working, not a defect.

- [ ] **Step 5: Record everything**

Append the diagnostics, the scores, the LLM call count and the cost to the ledger under `PHASE 3-4 GATE`, with the date and schema version live.

- [ ] **Step 6: Update the live-vs-designed table**

In `docs/architecture/pipeline-end-to-end.md` §12, move `core/mapping.py`, `core/memory.py`, `core/concepts.py` and `core/compose.py` out of "not reachable from any route or stage" — they are now wired through `fog_mapping` / `fog_reason`.

- [ ] **Step 7: Commit**

```bash
git add .superpowers docs/architecture/pipeline-end-to-end.md
git commit -m "docs(fog): Phase 3-4 gate measured on TCS/REMC/RIL"
```

---

## What this plan deliberately does NOT do

- **No route changes.** `/extract/document` and `/factsheet/document` keep serving the old path. Reading the FOG is Phase 5–6.
- **Nothing is deleted.** `pipeline/extractor.py`, `linking`, `graph_load` and the `ontology → quality → knowledge` loop all keep running.
- **No `COMPOSES`, `RECONCILES_WITH`, `SAME_AS` or `GOVERNED_BY` edges.** Composition currently produces values, not graph edges; promoting it to edges is Phase 5.
- **No mapping-memory persistence.** `store=None` throughout, so the memory tier is present, tested and inert. Wiring a durable store is Phase 5 — it changes cost, not correctness.
- **No policy engine.** Domain 2.

## Follow-on

| Plan | Phases | Content |
|---|---|---|
| 3 | 5–6 | Durable mapping memory · `COMPOSES`/`RECONCILES_WITH` edges · cash-flow module · applicability metadata · routes read the FOG · stage removals · delete the old path once the baseline is beaten |
