# RT-7 — Cross-Document / Entity Retrieval (filing timelines + concept comparison)

- **Status:** Implemented — 2026-07-18. Seventh sub-project of the retrieval layer (`docs/architecture/
  retrieval-roadmap.md`), Wave C (parallel with RT-4; RT-7 depends on RT-6 for `concept_lookup` and on
  SP-16's crossdoc foundation for `consolidate_entity`). See `docs/superpowers/plans/2026-07-18-rt7-
  crossdoc-entity-retrieval.md` for the implementation plan and `docs/architecture/retrieval-roadmap.md`'s
  "What RT-7 shipped that RT-8 must know" for the handoff.
- **Depends on (complete):**
  - **RT-6** (`fdi.pipeline.tables.concept_lookup(*, session, store, doc_id, concept) ->
    list[dict] | None`; each match `{table_index, row_label, values: [{column_header, raw, value}]}`;
    `None` = "no `structure` artifact yet", `[]` = "has `structure`, concept not found" — a distinct,
    real outcome. RT-7 calls this UNCHANGED, once per member document; it does not re-implement numeric
    extraction or concept matching. RT-7 also inherits RT-6's documented false-positive class — a
    synonym word inside an unrelated label (e.g. "Cost of revenue" matching `REVENUE`) — unsolved here,
    see §6.)
  - **SP-16 / crossdoc** (`fdi.core.crossdoc.consolidate_entity(members: list[Mapping]) -> dict` — folds
    a list of `{doc_id, doc_key, version, knowledge}` records into `{schema_version, entity_id,
    member_count, members: [{doc_id, doc_key, version, section_count, concept_count, quality_ok}],
    concepts, concept_presence, quality_trend}`, version-ordered, `None`-version last, doc_id tie-break.
    RT-7 REUSES this function unchanged for its timeline — see §2 decision 2. `fdi.pipeline.crossdoc.
    resolve_entities` is the corpus-wide, embedding-similarity clustering orchestrator around it; RT-7
    does NOT call `resolve_entities` — see §2 decision 1, the central fork.)
  - **RT-1** (roadmap sequencing only; RT-7 does not read `chunks` — table concepts and knowledge
    facts are the whole payload, no chunk text is returned).
  - **Ingestion `knowledge`/`structure` artifacts** (SP-17c/SP-7c) — read on demand, no new artifact.

## 1. Purpose

An analyst's real question is rarely "what does this one PDF say" — it's "how did ACME's revenue move
from FY2021 to FY2023" or "show me every filing we have for ACME and whether each one parsed cleanly."
Every RT-6 tool answers a single-document question; every crossdoc tool (SP-16) answers "which documents
in the whole corpus are probably the same company" but returns an EPHEMERAL, recomputed-per-call
grouping with no stable id. Nothing today turns a *known* group of a company's filings into an
ordered timeline or a side-by-side number comparison. RT-7 builds exactly those two views:

1. **`entity_timeline(doc_key)`** — a `doc_key`'s document versions, version-ordered, each annotated
   with its filing's key facts (section_count, concepts present, quality) — a reshape of
   `consolidate_entity`'s existing output, not a new algorithm.
2. **`compare_concept_across_versions(doc_key, concept)`** — THE core RT-7 capability: for every
   version of a `doc_key`, call RT-6's `concept_lookup` and assemble a cross-version comparison
   (e.g. REVENUE across FY2021→FY2023), preserving RT-6's `None`/`[]` distinction per version and
   carrying full citations (`doc_id`, `doc_key`, `version`, `table_index`, `row_label`,
   `column_header`).

Both are exposed as an HTTP route AND, in the pure-core layer, as reusable assembly functions RT-8's
agent can call the same way it will call every other RT tool.

## 2. Scope decisions (autonomous; documented)

### Decision 1 — Entity identity: `doc_key`, not a crossdoc `entity_id` (THE central fork; flagged for human review)

Three ways an "entity" could be identified at the API boundary:

- **(a) `doc_key`** — the stable intake version-group id already used by `POST /documents` (`doc_key`
  defaults to the filename) and `GET /documents?doc_key=` / `DocumentRepository.list_by_doc_key`. Every
  version of the same `doc_key` is, by construction, "the same filing series" — no clustering, no
  threshold, no corpus scan. **Durable**: the same `doc_key` string always resolves to the same document
  set (plus any new versions).
- **(b) a crossdoc `entity_id`** (`fdi.core.crossdoc.consolidate_entity`'s `entity_id = min(doc_ids)` of
  a cluster) — semantic, catches the case where the SAME real company was uploaded under two DIFFERENT
  `doc_key`s (e.g. `"acme_2021.pdf"` vs. `"ACME Annual Report 2022.pdf"`, never intentionally linked at
  intake). But `resolve_entities` recomputes the WHOLE corpus clustering on every call (spec `docs/
  superpowers/specs/2026-07-18-sp16-crossdoc-...`, §6/roadmap: "recomputed on demand") — nothing
  persists an `entity_id`, so `entity_id` can DRIFT between calls: adding one new document to the corpus
  can change which doc_id is the cluster's min, and a borderline document can flip in or out of a
  cluster if its embedding or the corpus average shifts. An id a client cannot safely cache across two
  requests is not a resource id in any REST sense — `GET /entities/{entity_id}/timeline` would be
  fetching a moving target.
- **(c) an explicit `doc_ids: list[str]`** in the request — fully caller-controlled, no ambiguity, but
  pushes the "which documents are the same company" question entirely onto the caller (including RT-8's
  agent), which is exactly the question crossdoc exists to answer. Also awkward for a `GET .../timeline`
  path-style route (a list doesn't fit a path segment) and would force `timeline` to be POST-only,
  diverging from the roadmap's `GET /entities/{entity_id}/timeline`.

**Decision: (a) `doc_key` is the primary, load-bearing identity for both RT-7 routes.** `GET
/entities/{doc_key}/timeline` and `POST /entities/compare` (body carries `doc_key`) resolve their
member set via `DocumentRepository.list_by_doc_key(doc_key)` — an indexed, O(versions-of-one-key) query,
never a corpus scan, never a clustering pass, never non-deterministic between two calls with the same
input. This is strictly cheaper AND more stable than routing through `resolve_entities`.

**How this relates to crossdoc, spelled out (not silently dropped):** `resolve_entities` stays the
corpus-wide DISCOVERY step — "which `doc_key`s (and lone documents) probably belong to the same
company." Its output's `members[].doc_key` field is exactly the input RT-7 wants. The intended
composition is: call `POST /entities/resolve` once to discover which `doc_key`s cluster together, then
call `entity_timeline`/`compare_concept_across_versions` per `doc_key` (or per member) using RT-7's
tools. **RT-7 does NOT fuse a MULTI-`doc_key` crossdoc cluster into one timeline in this slice** — if a
company's filings truly span two different `doc_key`s, RT-7 today returns two separate timelines, one
per `doc_key`, not one merged one. This is called out explicitly in §6 as deferred, not silently
absorbed by picking option (a).

**Flag for human review:** this is the single biggest judgment call in the spec. The recommendation
optimizes for stability and cheapness over "handles messy intake where the same company got two
different `doc_key`s" — which today requires a human/agent to notice via `/entities/resolve` and query
RT-7 twice. If the product expectation is "one timeline per COMPANY, however it was uploaded," option
(a) alone under-delivers and a follow-on (fusing multiple `doc_key` timelines behind one crossdoc
cluster) is real, not hypothetical, future work. A human should confirm this tradeoff is acceptable
before this ships.

### Decision 2 — `entity_timeline` REUSES `consolidate_entity`, does not reimplement it

`consolidate_entity(members: list[{doc_id, doc_key, version, knowledge}]) -> {schema_version, entity_id,
member_count, members, concepts, concept_presence, quality_trend}` already does everything
`entity_timeline` needs: version-ordering (`None` last, doc_id tie-break), per-member `{section_count,
concept_count, quality_ok}`, corpus-union `concepts`, `concept_presence` (concept → the list of
versions/doc_ids it appeared in), and `quality_trend` — INDEX-ALIGNED with `members` (both built in the
same `for m in ordered` loop in `fdi.core.crossdoc`). RT-7 feeds it a `doc_key`-scoped member list
instead of a similarity-cluster member list; the function does not know or care which. A new pure
reshape, `fdi.core.entities.build_entity_timeline(doc_key, members)`, calls `consolidate_entity` once
and:
1. Zips `members`/`quality_trend` positionally (already aligned) into one per-version record.
2. Derives each version's `concepts` list by inverting `concept_presence` (`presence_key = version if
   version is not None else doc_id`, matching `consolidate_entity`'s own key convention exactly) — the
   base function only exposes concept presence at entity granularity; the timeline view wants it
   per-version, which is a pure post-processing step over data `consolidate_entity` already computed,
   not a new traversal of `knowledge`.
3. **Overrides `entity_id` with the caller's `doc_key`.** `consolidate_entity`'s own `entity_id` (`min`
   of the member doc_ids) is dropped from RT-7's output entirely — surfacing it would imply it is a
   stable identifier for this timeline, when in fact if a NEW earliest-alphabetical doc_id were ever
   added to the group it would silently change. `doc_key` is the one genuinely stable identity RT-7 has;
   it is the only id in the response.

### Decision 3 — `compare_concept_across_versions` preserves RT-6's `None`/`[]` distinction per version, does not collapse it

Naive assembly would do `rows = concept_lookup(...) or []` and lose exactly the distinction the RT-6
handoff (roadmap, "What RT-6 shipped that RT-7 must know") explicitly warns against conflating. Instead
every version in the comparison carries an explicit `status`:
- `"no_structure"` — `concept_lookup` returned `None` (document has no `structure` artifact yet,
  e.g. still processing or `structure` failed) — `rows: []`.
- `"no_match"` — `concept_lookup` returned `[]` (document has `structure`, concept's synonyms matched no
  row) — `rows: []`.
- `"matched"` — `concept_lookup` returned one or more rows — `rows` carries them.

A UI/agent consuming this can render "FY2022: not yet processed" differently from "FY2022: revenue not
reported this way" instead of both looking like a blank cell. Every version is ALWAYS present in the
output (no version silently dropped for having no data) — this mirrors RT-1/RT-6's "degrade a field to
`None`/empty, never omit the record" posture, applied at the per-version granularity here.

### Decision 4 — pure/impure split mirrors RT-2's template

`fdi.core.entities` (NEW, pure, no I/O): `build_entity_timeline`, `build_concept_comparison`, plus a
tiny local version-sort key (a 5-line duplicate of `consolidate_entity`'s private `sort_key`/
`_int_or_none`, not an import of crossdoc's underscore-prefixed internals — see §3.1 rationale). Reads
inputs as plain `Mapping`/`list`, never imports `fdi.core.ontology`/`fdi.pipeline.*`.

`fdi.pipeline.entities` (NEW pipeline module, does I/O): `entity_timeline`, `compare_concept_across_versions`
— fetch `DocumentRepository.list_by_doc_key(doc_key)`, loop `RegistryRepository.latest_run` + `ArtifactStore.
get_json` for `knowledge` (timeline) or loop RT-6's `concept_lookup` (compare), delegate assembly to
`fdi.core.entities`. A new module rather than extending `fdi.pipeline.crossdoc`: `crossdoc.py`'s whole
surface is "corpus-wide clustering" (`resolve_entities`); RT-7's two functions are `doc_key`-scoped
and never cluster — mixing them into the same module would blur that boundary the module's own
docstring currently states cleanly. `fdi.pipeline.crossdoc._read_artifact` is promoted to a public
`read_artifact(repo, store, doc_id, stage)` (drop the leading underscore, no behavior change) so
`fdi.pipeline.entities.entity_timeline` can reuse the exact same "latest artifact or `None`, isolate one
document's store failure rather than aborting the whole call" helper `resolve_entities` already uses,
instead of a second copy.

### Decision 5 — no persisted entity index; on-demand, like `resolve_entities`/RT-6

The roadmap flags this as optional. RT-7 defers it: unlike `resolve_entities` (an O(N²) cosine
clustering pass over the WHOLE corpus, the actual reason a persisted index was ever worth discussing),
`entity_timeline`/`compare_concept_across_versions` never scan the corpus — `list_by_doc_key` is a single
indexed `WHERE doc_key = ...` query, and the per-version loop is bounded by "how many times has this one
filing series been re-uploaded," typically single digits. There is no scale argument for a cache here
today. Revisit only if a real deployment shows a `doc_key` with hundreds of versions and query-time cost
becomes visible (same "revisit if it proves hot" posture RT-6 §8 already took for `structure`).

### Decision 6 — route shapes and parameter naming

- `GET /entities/{doc_key}/timeline` — a path segment, not a query parameter, DESPITE `GET /documents?
  doc_key=` using a query parameter for the same logical field. Rationale: `/documents?doc_key=` lists
  MANY resources filtered by a field; `/entities/{doc_key}/timeline` fetches ONE resource (a timeline)
  identified by that field — matching this API's existing pattern for single-resource routes
  (`/documents/{doc_id}/tables`, `/documents/{doc_id}/concepts/{concept}`), not the list-filter pattern.
  **Known limitation, documented not fixed:** a `doc_key` containing a literal `/` breaks path routing
  (FastAPI does not un-escape `%2F` in a path segment by default). `doc_key` today defaults to the
  uploaded filename (`fdi.api.routes.upload_document`), so a filename with a slash (unusual but not
  impossible, e.g. a client-supplied path-like name) would 404 unroutably. Deferred: either document
  "doc_key must not contain `/`" as an intake constraint, or move this route to a query parameter later
  if it proves a real problem. Not blocking for this slice.
- `POST /entities/compare` — body `{doc_key: str, concept: CanonicalConcept}`, matching the roadmap's
  "body: entity identification + concept" and RT-6's `concept` path-enum precedent (Pydantic validates
  `CanonicalConcept` as a `StrEnum` field the same way the RT-6 path route does).
- Both 404 when `doc_key` matches zero documents (`list_by_doc_key` returns `[]`) — same "no artifact →
  404" posture as RT-6's `get_tables_route`/`concept_lookup_route`, applied to "no documents for this
  key" instead of "no structure artifact for this document."

## 3. Architecture

### 3.1 `fdi.core.entities` (pure)

```python
"""Cross-document entity retrieval assembly (pure; no I/O, config, clock, RNG).

build_entity_timeline reshapes fdi.core.crossdoc.consolidate_entity's existing
output into a doc_key-scoped, version-ordered timeline -- REUSING consolidate_
entity's version-ordering and concept-presence math rather than reimplementing
them (see design spec decision 2). build_concept_comparison assembles a
cross-version concept comparison from a list of per-document
fdi.pipeline.tables.concept_lookup results, preserving concept_lookup's None
("no structure artifact yet") vs [] ("structure present, concept not found")
distinction per version instead of collapsing both to an empty row list (spec
decision 3). Reads inputs as plain Mapping/list -- no import of fdi.core.
ontology or any fdi.pipeline module.
"""

from collections.abc import Mapping, Sequence

from fdi.core.crossdoc import consolidate_entity


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _version_sort_key(entry: Mapping[str, object]) -> tuple[int, int, str]:
    """Version ascending, None-version last, doc_id tie-break -- the SAME
    ordering consolidate_entity uses internally (duplicated here, not
    imported: consolidate_entity's sort_key is a local closure, and reaching
    into fdi.core.crossdoc's private helpers from a sibling module is worse
    than five duplicated lines)."""
    doc_id = entry.get("doc_id")
    doc_id_s = doc_id if isinstance(doc_id, str) else ""
    v = _int_or_none(entry.get("version"))
    return (0, v, doc_id_s) if v is not None else (1, 0, doc_id_s)


def build_entity_timeline(
    doc_key: str, members: list[Mapping[str, object]]
) -> dict[str, object]:
    """`members`: [{doc_id, doc_key, version, knowledge}], the same shape
    fdi.pipeline.crossdoc.resolve_entities' `stash` values use. `knowledge`
    missing/malformed degrades per consolidate_entity's own rules -- this
    function adds no new validation on top."""
    entity = consolidate_entity(list(members))

    member_views = entity["members"]
    quality_trend = entity["quality_trend"]
    concept_presence_raw = entity["concept_presence"]
    assert isinstance(member_views, list)
    assert isinstance(quality_trend, list)
    assert isinstance(concept_presence_raw, dict)

    timeline: list[dict[str, object]] = []
    for mv, q in zip(member_views, quality_trend, strict=True):
        assert isinstance(mv, dict) and isinstance(q, dict)
        version = mv.get("version")
        presence_key: object = version if version is not None else mv.get("doc_id")
        concepts = sorted(
            c for c, versions in concept_presence_raw.items()
            if isinstance(versions, list) and presence_key in versions
        )
        timeline.append({
            "doc_id": mv.get("doc_id"),
            "doc_key": mv.get("doc_key"),
            "version": version,
            "section_count": mv.get("section_count"),
            "concept_count": mv.get("concept_count"),
            "quality_ok": mv.get("quality_ok"),
            "concepts": concepts,
            "quality": {
                "ok": q.get("ok"),
                "error_count": q.get("error_count"),
                "warning_count": q.get("warning_count"),
            },
        })

    return {
        "schema_version": 1,
        "doc_key": doc_key,
        "member_count": entity["member_count"],
        "timeline": timeline,
        "concepts": entity["concepts"],
        "concept_presence": concept_presence_raw,
    }


def build_concept_comparison(
    doc_key: str, concept: str, member_lookups: list[Mapping[str, object]]
) -> dict[str, object]:
    """`member_lookups`: [{doc_id, doc_key, version, rows: list[Mapping] | None}]
    -- `rows` is exactly fdi.pipeline.tables.concept_lookup's return value for
    that member, UNCHANGED (None / [] / [{table_index, row_label, values}, ...]).
    Never re-sorts input as a side effect visible elsewhere -- returns a NEW
    ordered list, defensively re-sorted by version regardless of input order
    (mirrors consolidate_entity's own defensive posture, since callers should
    not have to pre-sort)."""
    versions: list[dict[str, object]] = []
    for ml in sorted(member_lookups, key=_version_sort_key):
        rows_raw = ml.get("rows")
        if rows_raw is None:
            status, rows = "no_structure", []
        elif isinstance(rows_raw, list) and len(rows_raw) == 0:
            status, rows = "no_match", []
        elif isinstance(rows_raw, list):
            status = "matched"
            rows = [dict(r) for r in rows_raw if isinstance(r, Mapping)]
        else:
            status, rows = "no_structure", []  # malformed input degrades safely

        versions.append({
            "doc_id": ml.get("doc_id"),
            "doc_key": ml.get("doc_key"),
            "version": ml.get("version"),
            "status": status,
            "rows": rows,
        })

    return {
        "schema_version": 1,
        "doc_key": doc_key,
        "concept": concept,
        "versions": versions,
    }
```

(`Sequence` import kept only if actually used by the final typed signatures — the executor should drop
it if `sorted(..., key=...)` over a `list` is all that's needed; noted here so the plan's Step 3 doesn't
silently carry a dead import.)

### 3.2 `fdi.pipeline.entities` (orchestrator, does I/O)

```python
"""RT-7 orchestrator: turn a `doc_key`'s document versions into a filing
timeline (entity_timeline) or a cross-version concept comparison
(compare_concept_across_versions). Pipeline layer -- imports platform (repo +
store) + core, the same shape as fdi.pipeline.tables/fdi.pipeline.crossdoc. No
new stage, no new artifact, no clustering: `doc_key` IS the entity identity
here (design spec decision 1) -- resolve_entities (corpus-wide, embedding
clustering) is a SEPARATE, complementary discovery step this module does not
call. Both functions return None when `doc_key` matches zero documents (the
caller maps this to 404); an unknown-but-nonempty case cannot happen since
list_by_doc_key only returns rows that exist.
"""

from collections.abc import Mapping
from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession

from fdi.core.entities import build_concept_comparison, build_entity_timeline
from fdi.core.ontology import CanonicalConcept
from fdi.pipeline.crossdoc import read_artifact
from fdi.pipeline.tables import concept_lookup
from fdi.platform.db.repository import DocumentRepository, RegistryRepository
from fdi.platform.storage.artifact_store import ArtifactStore


async def entity_timeline(
    *, session: AsyncSession, store: ArtifactStore, doc_key: str
) -> dict[str, object] | None:
    docs = await DocumentRepository(session).list_by_doc_key(doc_key)
    if not docs:
        return None
    reg_repo = RegistryRepository(session)
    members: list[dict[str, object]] = []
    for doc in docs:
        knowledge = await read_artifact(reg_repo, store, doc.id, "knowledge")
        members.append({
            "doc_id": doc.id,
            "doc_key": doc.doc_key,
            "version": doc.version,
            "knowledge": knowledge if knowledge is not None else {},
        })
    return build_entity_timeline(doc_key, members)


async def compare_concept_across_versions(
    *, session: AsyncSession, store: ArtifactStore, doc_key: str, concept: CanonicalConcept
) -> dict[str, object] | None:
    docs = await DocumentRepository(session).list_by_doc_key(doc_key)
    if not docs:
        return None
    member_lookups: list[dict[str, object]] = []
    for doc in docs:
        rows = await concept_lookup(session=session, store=store, doc_id=doc.id, concept=concept)
        member_lookups.append({
            "doc_id": doc.id, "doc_key": doc.doc_key, "version": doc.version, "rows": rows,
        })
    return build_concept_comparison(doc_key, concept.value, member_lookups)
```

### 3.3 `fdi.pipeline.crossdoc.read_artifact` (rename, no behavior change)

`_read_artifact(repo, store, doc_id, stage)` → `read_artifact(repo, store, doc_id, stage)` (drop the
leading underscore; docstring unchanged; the one call site in `resolve_entities` updated). This is the
ONLY change to existing crossdoc code RT-7 makes — everything else in `fdi.core.crossdoc`/`fdi.pipeline.
crossdoc` is reused unmodified.

### 3.4 API layer

`backend/src/fdi/api/schemas.py` additions:

```python
class EntityTimelineQualityView(BaseModel):
    ok: bool
    error_count: int
    warning_count: int


class EntityTimelineEntryView(BaseModel):
    doc_id: str
    doc_key: str | None
    version: int | None
    section_count: int
    concept_count: int
    quality_ok: bool
    concepts: list[str]
    quality: EntityTimelineQualityView


class EntityTimelineView(BaseModel):
    schema_version: int
    doc_key: str
    member_count: int
    timeline: list[EntityTimelineEntryView]
    concepts: list[str]
    concept_presence: dict[str, list[int | str]]


class ConceptComparisonValueView(BaseModel):
    column_header: str
    raw: str
    value: float | None


class ConceptComparisonRowView(BaseModel):
    table_index: int
    row_label: str
    values: list[ConceptComparisonValueView]


class ConceptComparisonVersionView(BaseModel):
    doc_id: str
    doc_key: str | None
    version: int | None
    status: str  # "matched" | "no_match" | "no_structure"
    rows: list[ConceptComparisonRowView]


class ConceptCompareRequest(BaseModel):
    doc_key: str
    concept: CanonicalConcept


class ConceptComparisonView(BaseModel):
    schema_version: int
    doc_key: str
    concept: str
    versions: list[ConceptComparisonVersionView]
```

`backend/src/fdi/api/routes.py` additions:

```python
@router.get("/entities/{doc_key}/timeline", response_model=EntityTimelineView)
async def entity_timeline_route(
    doc_key: str, session: SessionDep, store: StoreDep
) -> EntityTimelineView:
    result = await entity_timeline(session=session, store=store, doc_key=doc_key)
    if result is None:
        raise HTTPException(status_code=404, detail=f"no documents for doc_key {doc_key!r}")
    return EntityTimelineView.model_validate(result)


@router.post("/entities/compare", response_model=ConceptComparisonView)
async def compare_concept_route(
    body: ConceptCompareRequest, session: SessionDep, store: StoreDep
) -> ConceptComparisonView:
    result = await compare_concept_across_versions(
        session=session, store=store, doc_key=body.doc_key, concept=body.concept
    )
    if result is None:
        raise HTTPException(status_code=404, detail=f"no documents for doc_key {body.doc_key!r}")
    return ConceptComparisonView.model_validate(result)
```

`CanonicalConcept` import already present in `routes.py` (RT-6). New imports: `EntityTimelineView`,
`ConceptCompareRequest`, `ConceptComparisonView` (from `fdi.api.schemas`); `entity_timeline`,
`compare_concept_across_versions` (from `fdi.pipeline.entities`).

## 4. Error handling

- `doc_key` matching zero documents → both pipeline functions return `None`; both routes 404 with
  `"no documents for doc_key {doc_key!r}"`.
- A member document with no `knowledge` artifact yet → `entity_timeline` still includes it in the
  timeline (via `consolidate_entity`'s existing "missing knowledge degrades to `{}`" rule, same as
  `resolve_entities`); its `section_count`/`concept_count` are `0`, `quality_ok` is `False`, `concepts`
  is `[]` — never dropped from the timeline, never raises.
- A member document with no `structure` artifact yet (`concept_lookup` returns `None`) →
  `compare_concept_across_versions` includes it with `status: "no_structure", rows: []` — never dropped,
  never conflated with `"no_match"`.
- `build_entity_timeline([])` (a `doc_key` with an empty member list, defensive — should not happen once
  the pipeline layer's `if not docs: return None` guard is in place, but the pure function itself must
  not raise on it) → `{"schema_version":1, "doc_key":doc_key, "member_count":0, "timeline":[],
  "concepts":[], "concept_presence":{}}`.
- `build_concept_comparison(doc_key, concept, [])` → `{"schema_version":1, "doc_key":doc_key,
  "concept":concept, "versions":[]}`.
- Malformed `member_lookups[i]["rows"]` (present but neither `None` nor a `list`) degrades to
  `status: "no_structure", rows: []` rather than raising — defensive, should not occur since
  `concept_lookup` only ever returns `None` or `list[dict]`.
- `read_artifact`'s existing store-read-failure isolation (one bad artifact object degrades to "no
  artifact" rather than aborting the whole `doc_key`'s loop) is inherited unchanged from
  `fdi.pipeline.crossdoc`.

## 5. Testing

- **Unit** (pure, no Docker), `backend/tests/unit/test_entities_timeline.py`:
  - Two members (versions 1, 2) with distinct `knowledge` (different `concepts`, one `quality.ok=False`)
    → `build_entity_timeline` returns a version-ordered `timeline` of two entries; each entry's
    `concepts` matches exactly what `concept_presence` says was present at that member's `presence_key`;
    `quality` fields index-align with the right member (not swapped).
  - `entity_id` is NOT a key in the output — assert `"entity_id" not in result` (decision 2's override
    is a removal, not a rename, so a lazy `entity_id: doc_key` alias must not silently reappear).
  - A member missing `knowledge` (empty `{}`) → its `section_count`/`concept_count` are `0`,
    `quality_ok` is `False`, `concepts` is `[]`, still present in `timeline`.
  - `build_entity_timeline("k", [])` → the documented empty shape (§4).
  - A member with `version: None` sorts LAST (mirrors `consolidate_entity`).
- **Unit**, `backend/tests/unit/test_entities_comparison.py`:
  - Three `member_lookups`: one `rows=None`, one `rows=[]`, one `rows=[{table_index:0, row_label:"Total
    revenue", values:[{column_header:"FY23","raw":"1,000","value":1000.0}]}]` → three `versions` entries
    with `status` exactly `"no_structure"`, `"no_match"`, `"matched"` respectively (order matches
    version-sort, not input order — pick versions 1/2/3 so the ordering assertion is meaningful).
  - The `"matched"` entry's `rows` is `concept_lookup`'s shape UNCHANGED (`table_index`, `row_label`,
    `values` with `column_header`/`raw`/`value`) — assert byte-for-byte equality against the input
    dict, proving no re-shaping/renaming happened.
  - Input order shuffled (e.g. version 3's dict listed first) → output `versions` is still ascending —
    proves the defensive re-sort, not an accidental pass-through.
  - `build_concept_comparison("k", "REVENUE", [])` → the documented empty shape.
- **Integration** (testcontainers + fakes), `backend/tests/integration/test_entity_timeline.py`:
  - Reuse `test_resolve_entities.py`'s `_build_registry`/`_process_document` harness (ocr→normalize→
    ast→structure→semantic→ontology→quality→knowledge — no `embedding` stage needed, RT-7 never
    clusters) to run TWO documents through to `knowledge` under the SAME `doc_key`, via two
    `intake_document` calls with DIFFERENT pdf bytes (distinct `uuid4()` body per call — `checksum`
    dedup in `intake_document` means identical bytes collapse to one document; distinct bytes + same
    `doc_key` is what produces `version=1`/`version=2` — see `DocumentRepository.next_version`).
  - `entity_timeline(doc_key=...)` returns `member_count == 2`, `timeline` ordered `[v1, v2]`, each
    entry's `doc_id` matching the intake call that produced it.
  - `entity_timeline(doc_key="no-such-key")` → `None`.
  - Session-scoped shared DB (per `tests/integration/conftest.py`, same as `test_resolve_entities.py`):
    assert MEMBERSHIP (`any(e["doc_id"] == my_doc_id for e in timeline)`), never an exact `member_count`
    against the whole table — mirror `test_resolve_entities.py`'s documented rationale verbatim (a
    session-scoped Postgres/MinIO is shared across the whole `tests/integration` run; this test's
    `doc_key` is a fresh `uuid4()` per test run so no OTHER test's documents can share it, which sidesteps
    the cross-file pollution concern `test_resolve_entities.py` has for `resolve_entities`'s
    corpus-wide scan — RT-7's query is already scoped to one `doc_key`, so exact-count assertions ARE
    safe here, unlike in `test_resolve_entities.py`; state this explicitly in the test module docstring
    rather than copying the hedge unnecessarily).
- **Integration**, `backend/tests/integration/test_compare_concept.py`:
  - Reuse `test_tables_lookup.py`'s `_numeric_table_ocr()` fixture, run ocr→structure only (no
    knowledge chain needed — `concept_lookup` reads `structure` alone), intake the SAME `doc_key` TWICE
    (two `intake_document` calls, distinct pdf bytes) — version 1 with `_numeric_table_ocr()`'s
    `"12,345"` FY24 revenue row, version 2 with a SECOND `FakeOcrClient` fixture reusing the same table
    shape but a different value (e.g. `"15,000"`, header `"FY25"`) to prove cross-version distinctness.
  - `compare_concept_across_versions(doc_key=..., concept=CanonicalConcept.REVENUE)` → `versions` has
    two `"matched"` entries, version 1's row value `12345.0`/header `"FY24"`, version 2's row value
    `15000.0`/header `"FY25"` — the concrete "REVENUE across FY24→FY25" assertion the roadmap's example
    describes.
  - `compare_concept_across_versions(doc_key=..., concept=CanonicalConcept.EQUITY)` (present in neither
    version's table) → both entries `status == "no_match"`.
  - A THIRD version intaken but left at `ocr` only (never run through `structure`) →
    its entry is `status == "no_match"`... **no** — `status == "no_structure"` (no `structure`
    artifact) — spell this out precisely in the test so the None/[] distinction is actually exercised,
    not just asserted in the unit test.
  - `compare_concept_across_versions(doc_key="no-such-key", ...)` → `None`.
- **API**, append to `backend/tests/integration/test_api.py` (matching RT-6's own precedent — its
  `/documents/{doc_id}/tables`/`/concepts/{concept}` tests live in this same file, not a separate one):
  `GET /entities/{doc_key}/timeline` and `POST /entities/compare` against a local two-version-doc_key
  fixture modeled on RT-6's `doc_with_numeric_table` — 200 with the expected shape, 404 for an unknown
  `doc_key`.
- **Regression**: `test_resolve_entities.py` continues to pass unchanged after `_read_artifact` →
  `read_artifact` (rename only, call site updated in the same commit) — run it explicitly as part of
  the rename task's gate, not just the full suite at the end.

## 6. Explicitly deferred (YAGNI)

- **Fusing a multi-`doc_key` crossdoc cluster into ONE timeline/comparison.** Decision 1's biggest
  concession: if `resolve_entities` finds that two DIFFERENT `doc_key`s are the same real company, RT-7
  today still answers per-`doc_key`, not per-company. A follow-on `entity_timeline_for_cluster` (or
  a `doc_keys: list[str]` variant of both functions, unioning member lists before delegating to the
  SAME pure `fdi.core.entities` functions — no core-layer change needed, only a new pipeline-layer
  entry point) is the natural extension once real corpora show this is a frequent, not edge, case.
- **A persisted entity index.** Decision 5 — no scale argument yet; `list_by_doc_key` is already an
  indexed single-key query.
- **Disambiguating RT-6's synonym false-positive class** ("Cost of revenue" matching `REVENUE`). RT-7
  surfaces `row_label` unchanged in every comparison row precisely so a human/agent can catch it by
  inspection — same posture RT-6 shipped with, still a human/product call, not solved here.
- **A `GET /entities/{doc_key}/compare?concept=` GET-alternative.** The roadmap specifies `POST /entities/
  compare`; a GET form is a pure convenience addition with no new capability, left for later if the
  agent (RT-8) or a UI finds POST-only awkward.
- **Numeric reconciliation across versions** (e.g. flagging "FY23 revenue as restated in the FY24 filing
  doesn't match what FY23's own filing said") — explicitly out of scope corpus-wide per the roadmap's
  "Explicitly deferred" section; RT-7 only presents the numbers side by side, never compares them.

## 7. Done criteria

1. `fdi.core.entities.build_entity_timeline(doc_key, members)` reuses `consolidate_entity` unchanged
   (verified by grep: no duplicated version-ordering/concept-aggregation logic beyond the tiny
   documented `_version_sort_key`/`_int_or_none` duplication), overrides `entity_id` with `doc_key`
   (never emits `entity_id`), derives per-version `concepts` from `concept_presence`, never raises on
   empty/malformed input; `core` stays pure (no I/O, no import of `fdi.pipeline.*`/`fdi.core.ontology`).
2. `fdi.core.entities.build_concept_comparison(doc_key, concept, member_lookups)` preserves `concept_
   lookup`'s `None`/`[]`/matched distinction as an explicit `status` per version, defensively re-sorts by
   version, never raises, never drops a version.
3. `fdi.pipeline.entities.entity_timeline`/`compare_concept_across_versions` do only I/O (`list_by_doc_
   key`, `read_artifact` or `concept_lookup` per member) and delegate assembly to `fdi.core.entities`;
   both return `None` for a `doc_key` matching zero documents.
4. `fdi.pipeline.crossdoc._read_artifact` → `read_artifact` (public), `resolve_entities`'s call site
   updated, `test_resolve_entities.py` passes unchanged.
5. `GET /entities/{doc_key}/timeline` and `POST /entities/compare` wired per §3.4, 404 on an unknown
   `doc_key`, 200 with the documented shape otherwise.
6. All four gates green (`ruff`, `mypy --strict`, `lint-imports` 1-kept/0-broken, full suite); §5's
   tests all pass, including the session-scoped-DB membership tests; deferred items (§6) remain out of
   scope; the retrieval roadmap's RT-7 row flips to done with a "what RT-7 shipped" handoff (the
   `doc_key`-as-entity-identity decision and its relationship to `resolve_entities`, the `status` field
   on comparison versions, `read_artifact`'s promotion to public).
7. **The Decision 1 fork (doc_key vs. crossdoc `entity_id` vs. explicit doc_ids) is explicitly
   surfaced to a human reviewer before/alongside merge** — this spec recommends and implements (a), but
   does not consider the recommendation self-ratifying; §2 decision 1's "flag for human review"
   paragraph is not satisfied by writing it down, only by a human actually seeing and confirming it.
