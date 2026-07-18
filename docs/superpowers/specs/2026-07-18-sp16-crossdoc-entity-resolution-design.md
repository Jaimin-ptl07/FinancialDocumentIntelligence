# SP-16 — Cross-Document Entity Resolution (embedding clustering)

- **Status:** Design — approved 2026-07-18 (entity-resolution strategy: embedding-similarity clustering;
  delivery: on-demand API; default threshold 0.85 — all three user-selected). Awaiting spec review.
- **Depends on:**
  - **SP-13/SP-13b** (`EmbeddingStage`, `deps=("ast",)` → `{schema_version:1, model, dim,
    chunks:[{chunk_id, heading, vector}]}` at a content-addressed key; `vector` is a `list[float]` of
    length `dim` = 384 for the local `BgeEmbeddingClient`). SP-16 **reuses these already-computed,
    already-cached per-section vectors** — it makes no new embedding call.
  - **SP-15** (`fdi.core.knowledge.consolidate` + `KnowledgeStage` → the per-document `knowledge`
    artifact `{schema_version:1, section_count, section_types, concepts, concept_count, quality}`).
    SP-15 declared this record "SP-16's per-document input"; SP-16 reads it as each member's
    contribution to its entity record. `concepts` are `CanonicalConcept` strings — the roadmap's
    designated cross-document comparison key (SP-11 handoff, roadmap §"What SP-11 shipped").
  - **SP-4** (`Document` model: `id`, `doc_key`, `version`; `intake_document` allocates a monotonic
    `version` per `doc_key`). `version` orders filings **within** a discovered entity; it is NOT the
    entity key here (the clustering is on content, not `doc_key` — the user chose the embedding
    strategy over the `doc_key` strategy).
  - **SP-14** (`POST /search` — the first synchronous, task-free route; the template for
    `POST /entities/resolve`: a route that orchestrates injected platform services and returns a
    computed result with no Celery task).
  - **SP-2** (artifact read path: `RegistryRepository.latest_run(doc_id, stage)` → `artifact_key` →
    `ArtifactStore.get_json(key)`; already used verbatim by the `GET /documents/{id}/artifacts/{stage}`
    route to read any stage's artifact outside a `StageContext`).
- **Roadmap:** `docs/architecture/ingestion-roadmap.md` §3 table — SP-16 "Cross-document layer — entity
  resolution, version linking, cross-doc refs", weight **enterprise/heavy**, currently ⬜. The final
  roadmap SP; §4 build order lists it last ("SP-15 store · SP-16 cross-doc · SP-17 quality"). SP-9b and
  SP-11 both explicitly deferred cross-document work to SP-16 (roadmap §"What SP-9b shipped",
  §"What SP-11 shipped").

## 1. Purpose

Every stage so far is **per-document**: each runs for one `doc_id` and its artifacts describe that one
document. Nothing groups the corpus into the real-world **entities** (companies) whose filings it
contains, and nothing compares one entity's filings across time. SP-16 is the first **corpus-level**
(multi-document) operation, and it is the roadmap's final piece: the cross-document layer.

It does three things the roadmap names:

1. **Entity resolution** — decide which documents are the same real-world entity, by clustering
   documents on **embedding similarity** (the user-selected strategy). Two documents land in the same
   entity when their whole-document vectors are cosine-similar at or above a threshold.
2. **Version linking** — within a resolved entity, order the member documents by `Document.version`
   (the intake-time monotonic version), producing a filing timeline.
3. **Cross-doc concept refs** — fold the members' per-document `knowledge` records into one entity
   record: the union of `CanonicalConcept`s across the entity, **which versions each concept appears
   in** (the cross-document reference), and the entity's quality trend across versions.

**Design spine (matches the whole codebase's ethos):** the clustering *algorithm* is a pure,
deterministic function of already-computed vectors — exact cosine, connected-components (single-linkage).
The only non-deterministic, external, heavyweight part (producing the vectors) already happened upstream
in SP-13/13b and is cached. So `fdi.core.crossdoc` is fully unit-testable with hand-crafted vectors — no
BGE, no Milvus, no Docker for the core — the same "pure-core-first, thin slice, mockable seam only where
a real external dependency exists" shape as every prior SP.

## 2. Scope decisions (autonomous unless marked user-selected; documented)

1. **Entity key = embedding-similarity cluster, NOT `doc_key`** *(user-selected)*. Documents are grouped
   by clustering their whole-document vectors; `doc_key` is deliberately not the entity key (that was
   the competing, unchosen option). `doc_key`/`version` still serve **version linking within** a
   discovered cluster (decision 6), but two documents can resolve to one entity even with different
   `doc_key`s, and one `doc_key`'s versions could in principle split across clusters if their content
   diverges (reported honestly, not forced back together — decision 9).
2. **Whole-document vector = mean-pool of the SP-13 per-section chunk vectors, L2-normalized**
   (`fdi.core.crossdoc.document_vector`). No new embedding call: the `embedding` artifact already holds
   `chunks:[{vector}]`; the document vector is their element-wise mean, then normalized to unit length so
   cosine reduces to a dot product and documents with more sections are not weighted differently. A
   document with zero chunks (zero AST sections — the same case `EmbeddingStage` already skips) yields an
   empty vector and is **excluded from clustering** (decision 9), not clustered as a zero vector.
3. **Clustering = single-linkage / connected components over the "cosine ≥ threshold" graph**
   (`fdi.core.crossdoc.cluster`). Build an undirected graph whose nodes are documents (with valid
   vectors) and whose edges are pairs with `cosine ≥ threshold`; each connected component is one entity.
   Single-linkage is the natural fit for "same company's filings form a chain across years" (A~B, B~C ⇒
   A,B,C one entity even if A and C drifted below threshold directly). It is **order-independent and fully
   deterministic** (a property tested explicitly), needs no random seed or centroid initialization (unlike
   k-means, which also needs a pre-chosen k the corpus does not give us), and requires no cluster count up
   front. Exact pairwise cosine, O(n²); acceptable at dev/test scale, Milvus-ANN acceleration deferred
   (§6).
4. **Delivery = on-demand synchronous API** *(user-selected)*. `POST /entities/resolve` computes the
   clustering and entity records live from the corpus on each call, exactly like `POST /search` — no new
   persisted store, no entity-index artifact, no staleness/refresh machinery. The result is a pure
   function of the current corpus's cached artifacts; recomputing is cheap enough at this scale and always
   fresh. Persisting a materialized entity index is the explicitly-deferred alternative (§6).
5. **Default threshold = 0.85, overridable per-request and via config** *(user-selected)*. The default
   lives in `Settings.entity_sim_threshold` (config trio: `config.py` / `compose.yml` / `.env.example`
   as `FDI_ENTITY_SIM_THRESHOLD`), surfaced into the route via the lifespan → `app.state` → dependency
   pattern that `embedder`/`vector_store` already use. `POST /entities/resolve` accepts an optional
   `threshold` in the body that overrides the default for that call (for tuning against real data without
   a redeploy). Threshold is validated to `0.0 ≤ threshold ≤ 1.0`.
6. **Version linking = order an entity's members by `(version, doc_id)`** — `Document.version` is the
   intake-time monotonic filing version; where two members share a version or a member's `version` is
   `None`, `doc_id` breaks the tie deterministically. This orders the `members`, `quality_trend`, and the
   version lists inside `concept_presence`.
7. **Entity record shape** (`fdi.core.crossdoc.consolidate_entity`) — a fixed-key dict, not a passthrough
   merge:
   ```json
   {
     "schema_version": 1,
     "entity_id": "<min doc_id in the cluster>",
     "member_count": 2,
     "members": [
       {"doc_id": "...", "doc_key": "acme-annual", "version": 1,
        "section_count": 12, "concept_count": 4, "quality_ok": true},
       {"doc_id": "...", "doc_key": "acme-annual", "version": 2,
        "section_count": 13, "concept_count": 5, "quality_ok": true}
     ],
     "concepts": ["REVENUE", "TOTAL_ASSETS", "..."],
     "concept_presence": {"REVENUE": [1, 2], "TOTAL_ASSETS": [1], "...": ["..."]},
     "quality_trend": [
       {"version": 1, "ok": true, "error_count": 0, "warning_count": 1},
       {"version": 2, "ok": true, "error_count": 0, "warning_count": 0}
     ]
   }
   ```
   - `entity_id` = the lexicographically smallest member `doc_id` (deterministic, no wall-clock/RNG —
     Global Constraint 6; an entity has no id of its own, so a stable function of its members is the id).
   - `concepts` = sorted union of every member's `knowledge["concepts"]`.
   - `concept_presence[concept]` = the sorted list of member **versions** in which that concept appears —
     the cross-document reference ("REVENUE is reported in v1 and v2; MARGIN only in v2"). Members whose
     `version` is `None` contribute their `doc_id` string in place of a version so presence is never
     silently dropped.
   - `quality_trend` = per-member `{version, ok, error_count, warning_count}` echoed from each member's
     `knowledge["quality"]`, version-ordered — the entity's soundness over time.
8. **Layering (honours `api | workers → pipeline → platform → core`, exhaustive):**
   - **`fdi.core.crossdoc`** (pure; no I/O, no config, no clock, no RNG; imports neither
     `fdi.core.knowledge` nor `fdi.core.embeddings` — reads all inputs as plain `Mapping`/`Sequence`,
     the same "read defensively, never import the producer's types" posture `consolidate`/`build_graph`
     take): `document_vector`, `cosine`, `cluster`, `consolidate_entity`.
   - **`fdi.pipeline.crossdoc`** — `resolve_entities(...)`, the corpus-level orchestrator that enumerates
     documents and reads their `embedding`/`knowledge` artifacts, then calls the pure core. This mirrors
     the existing pipeline-layer orchestrators `fdi.pipeline.intake` and `fdi.pipeline.eval_service`,
     which already import `fdi.platform.db.repository` + `fdi.platform.storage.artifact_store` and are
     called by API routes — pipeline→platform→core is legal, and this is the established precedent, not a
     new pattern.
   - **`fdi.platform.db.repository`** — one new method `RegistryRepository.list_documents()` enumerating
     the corpus (`Document` rows).
   - **`fdi.api.routes` / `fdi.api.schemas` / `fdi.api.deps` / `fdi.api.app`** — the `POST
     /entities/resolve` route, its request/response models, the threshold dependency, and the
     lifespan line seeding `app.state.entity_threshold` from settings.
   - **No new Stage.** Entity resolution is inherently corpus-level (it needs many documents at once);
     it does not fit the per-document `Stage`/DAG model and deliberately is not forced into it. This is
     the first non-Stage pipeline capability since the eval service — precedent exists.
9. **Documents that cannot be placed are reported, never guessed.** A document with no `embedding`
   artifact (its pipeline has not reached `EmbeddingStage`), or whose embedding yields an empty vector
   (zero chunks), is excluded from clustering and returned in a top-level `unresolved: [doc_id, ...]`
   list — the API tells the caller "these N documents were not clustered and why-shaped" rather than
   silently dropping them or fabricating singleton entities for them. A document that simply matches no
   other document **does** form a valid one-member entity (a company with a single filing is a real
   entity — that is not "unresolved").
10. **Missing `knowledge` for a clustered member degrades via `consolidate`'s own contract.** If a member
    was clustered (it has an embedding vector) but has no `knowledge` artifact yet, its knowledge fields
    are read as the empty record `consolidate({}, {}, {})` would produce (zeros/empties) — the member
    still appears in the entity with `section_count: 0`, `concept_count: 0`, `quality_ok: false`,
    contributing nothing to `concepts`/`concept_presence`. Never raises. (A vector without a knowledge
    record is an unusual but legal intermediate pipeline state.)

## 3. Architecture

### 3.1 `fdi.core.crossdoc` (pure)

```python
def document_vector(embedding: Mapping[str, object]) -> list[float]:
    """Mean-pool the chunk vectors of an SP-13 `embedding` artifact, L2-normalized.
    Empty/malformed / zero chunks / all-zero mean -> [] (excluded from clustering)."""

def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity. 0.0 if either is empty, zero-norm, or length-mismatched."""

def cluster(vectors: Mapping[str, Sequence[float]], threshold: float) -> list[list[str]]:
    """Single-linkage connected components over the cosine>=threshold graph.
    Input: doc_id -> unit vector (callers exclude empty vectors first).
    Returns components as sorted doc_id lists, the list itself sorted by each
    component's min doc_id. Order-independent and deterministic."""

def consolidate_entity(members: list[Mapping[str, object]]) -> dict[str, object]:
    """Fold clustered members (each {doc_id, doc_key, version, knowledge}) into the
    entity record of decision 7. Version-ordered; entity_id = min member doc_id.
    Pure; degrades on missing knowledge (decision 10); never raises."""
```

- `document_vector`: read `embedding["chunks"]` (list of `Mapping`); collect each `chunk["vector"]` that
  is a non-empty list of real numbers; keep only vectors whose length equals the first kept vector's
  length (a mismatched-dimension vector is malformed — skipped, not zero-padded); element-wise mean; if
  the mean has zero L2 norm or no vectors were kept, return `[]`; else divide by the norm.
- `cosine`: `dot(a,b) / (norm(a) * norm(b))`; guard `len(a) == len(b)` and both norms `> 0`, else `0.0`.
  (Inputs from `document_vector` are already unit vectors, so this is effectively a dot product, but
  `cosine` normalizes defensively so it is correct for any caller and any hand-crafted test vector.)
- `cluster`: union-find (or iterative BFS) over the pair graph; deterministic because the output is
  sorted, not because iteration order happens to be stable. Complexity O(n²·dim) — §6 defers the
  Milvus-ANN speedup.
- `consolidate_entity`: sort members by `(version if int else +inf, doc_id)`; `concepts` = sorted union;
  `concept_presence` built by walking version-ordered members and appending each member's version (or
  `doc_id` when version is `None`) to each of its concepts' lists; `quality_trend` = version-ordered echo
  of each member's `knowledge["quality"]` three-key summary.

### 3.2 `fdi.pipeline.crossdoc.resolve_entities` (orchestrator)

```python
async def resolve_entities(
    *, session: AsyncSession, store: ArtifactStore, threshold: float
) -> dict[str, object]:
    """Corpus-level entity resolution. Enumerate documents, read each one's
    `embedding` + `knowledge` artifacts, cluster on embedding similarity, fold each
    cluster into an entity record. Returns {schema_version, threshold, entity_count,
    entities:[...], unresolved:[doc_id,...]}."""
```

Flow (all reads use the existing `latest_run` → `artifact_key` → `store.get_json` path, exactly as
`GET /documents/{id}/artifacts/{stage}`):

1. `docs = await RegistryRepository(session).list_documents()` — every `Document`.
2. For each `doc`: read its latest `embedding` artifact (if any) and latest `knowledge` artifact (if any).
   Compute `document_vector`. A doc with no embedding artifact or an empty vector → `unresolved`.
   Otherwise record `vectors[doc.id] = vec` and stash `{doc_id, doc_key, version, knowledge}`.
3. `components = cluster(vectors, threshold)`.
4. For each component, gather its members' stashed dicts and call `consolidate_entity`.
5. Return the record; `entities` sorted by `entity_id`, `unresolved` sorted.

`resolve_entities` reads settings for nothing — `threshold` is passed in (the route resolves default vs
override), keeping the orchestrator a pure-ish function of (corpus, threshold) and directly testable with
a real session + store in an integration test.

### 3.3 API — `POST /entities/resolve`

- **Request** (`EntityResolveRequest`): `{ "threshold": float | null }` — `null`/absent uses the
  configured default; a value must satisfy `0.0 <= threshold <= 1.0` (Pydantic `ge=0.0, le=1.0`).
- **Response** (`EntitiesView`): `{ schema_version, threshold, entity_count, entities: [EntityView...],
  unresolved: [str...] }`, `EntityView` mirroring decision 7's record.
- **Handler**: resolve `threshold = body.threshold if body.threshold is not None else
  request.app.state.entity_threshold`; call `resolve_entities(session=..., store=..., threshold=...)`
  with the injected `SessionDep`/`StoreDep` (both already exist and are used by
  `GET /documents/{id}/artifacts/{stage}`); return the view. No 503 gate is needed — entity resolution
  needs neither Milvus nor an embedder at request time (it reads already-stored artifacts), so unlike
  `/search` it has no external-service precondition; an empty corpus simply returns
  `entity_count: 0, entities: []`.
- **Threshold wiring**: `app.py` lifespan adds `app.state.entity_threshold = settings.entity_sim_threshold`
  (alongside the existing `app.state.embedder`/`vector_store`); `deps.py` adds a small dependency reading
  it — the identical config→lifespan→state→dep path those two services already use.

### 3.4 Config

`Settings.entity_sim_threshold: float = 0.85` (`FDI_ENTITY_SIM_THRESHOLD`). Mirrored across all three
files the inert-config validator (`_reject_unknown_fdi_env_vars`) governs: `config.py` (the field),
`docker/compose.yml` (`x-backend-env`), `.env.example`. Omitting any one makes the var inert or crashes
boot — the discipline every prior FDI_ field followed.

## 4. Error handling

- **Empty corpus** → `{schema_version:1, threshold, entity_count:0, entities:[], unresolved:[]}`. No raise.
- **Doc with no `embedding` artifact / empty vector (zero chunks)** → excluded from clustering, listed in
  `unresolved` (decision 9). Not a crash, not a fabricated entity.
- **Doc with an `embedding` but no `knowledge` artifact** → clustered normally; its knowledge fields
  degrade to the zeroed record (decision 10). No raise.
- **Malformed `embedding` artifact** (non-list `chunks`, a chunk with a non-list/empty/mismatched-length
  `vector`) → `document_vector` skips the bad parts; if nothing usable remains, `[]` → `unresolved`.
  Never raises.
- **Malformed `knowledge` artifact** (missing/typo'd keys) → each field degrades exactly as
  `consolidate`'s own §4 already specifies (SP-16 does not re-implement that; it reads the
  already-defensive record's fields with the same `.get`/`isinstance` posture).
- **Singleton cluster** (a document matching no other at the threshold) → a valid one-member entity, NOT
  an error and NOT `unresolved`.
- **`threshold` out of `[0,1]`** → 422 from Pydantic validation at the request boundary, before any work.
- **A vector of the wrong dimension vs the rest of the corpus** (e.g. a legacy 1536-dim artifact left over
  from the pre-SP-13b Azure backend alongside new 384-dim ones) → `cosine` returns `0.0` for every
  cross-dimension pair (length guard), so a stray-dimension document simply never links to the others and
  falls into its own singleton entity — degraded, not crashed. (Fresh corpora are all 384-dim; this
  guard is defensive.)
- **No raise anywhere in `fdi.core.crossdoc`**, matching `consolidate`/`build_graph`/`map_document`: any
  malformed/partial input yields a smaller/emptier result, never an exception.

## 5. Testing

- **Unit** (pure, no Docker), `backend/tests/unit/test_crossdoc.py`:
  - `document_vector`: a two-chunk artifact → the L2-normalized element-wise mean (checked with
    `pytest.approx`); zero chunks / missing `chunks` / a chunk with an empty or mismatched-length vector →
    `[]` or the bad chunk skipped; an all-zero mean → `[]`.
  - `cosine`: identical unit vectors → `1.0`; orthogonal → `0.0`; opposite → `-1.0`; length mismatch →
    `0.0`; empty/zero-norm → `0.0` (all `pytest.approx`).
  - `cluster`: a corpus of two tight groups plus one far singleton at threshold 0.85 → three components
    with the right members; a single-linkage chain (A~B, B~C, A≁C directly) → one component of all three
    (proves single-linkage, not complete-linkage); the **determinism/order-independence** property —
    clustering the same vectors under several input orderings yields byte-identical output (the core
    guarantee of decision 3); an empty input → `[]`.
  - `consolidate_entity`: two members sharing a `doc_key`, versions 1 and 2, one concept present in both
    and one only in v2 → correct `concepts` union, `concept_presence` (`{shared:[1,2], v2only:[2]}`),
    `quality_trend` version-ordered, `entity_id` = min `doc_id`, `member_count` 2; a member with `version
    None` → ordered last, its presence recorded under its `doc_id`; a member with an empty/missing
    knowledge record → appears with zeroed fields, contributes no concepts (decision 10).
- **Integration** (testcontainers Postgres+MinIO, `FakeOcrClient` + `FakeEmbeddingClient` +
  `FakeSemanticClient`), `backend/tests/integration/test_entities_resolve.py`:
  - Intake **three** documents; drive each through `ocr → … → embedding` and `→ knowledge` via
    `run_stage`. The `FakeEmbeddingClient` is set up so docs A and B produce near-identical vectors
    (cosine ≥ 0.85) and doc C a distinct one (< 0.85 vs both).
  - `POST /entities/resolve` (default threshold) → **2 entities**: {A,B} and {C}; assert members,
    version ordering, `concepts` union, and `concept_presence` (a concept present in A but not B shows the
    right version list) — the cross-doc reference end-to-end.
  - A body `threshold` override of `0.99` → **3 singleton entities** (A and B no longer merge), proving
    the per-request override path.
  - A fourth document intaken but **not** run through `EmbeddingStage` → appears in `unresolved`, not in
    any entity (decision 9).
  - Assert `POST /entities/resolve` on an empty DB → `entity_count: 0` (no 503, no raise — decision 4 /
    §4).
- **Config test** — extend `backend/tests/unit/test_config.py` with `entity_sim_threshold == 0.85`
  default (mirrors the existing per-domain default tests).

## 6. Explicitly deferred (YAGNI)

- **Milvus-ANN clustering for scale.** Exact pairwise cosine is O(n²·dim); at 5000+ documents that is
  ~12.5M comparisons per call — fine for dev/test and a first correct implementation, but a materialized,
  Milvus-approximate-nearest-neighbour build is the real-scale path. Deferred with the on-demand delivery
  choice (decision 4); the pure `cluster` stays the reference/correctness implementation regardless.
- **Materialized / persisted entity index** — the unchosen delivery option: a batch build writing an
  entity-index artifact + a `GET /entities` read + refresh/staleness handling. On-demand recomputation is
  the chosen path; persistence is a clean follow-on that would reuse the exact same `fdi.core.crossdoc`.
- **Content-name / fuzzy entity resolution** — extracting a company name from the title page and matching
  normalized legal names (the other unchosen strategy). Could complement embedding clustering later
  (e.g. as a tie-breaker or a `doc_key` cross-check); not built here.
- **Cross-document Neo4j graph merge** — SP-9b deferred merging per-document graphs into one corpus graph
  to SP-16; the entity records ARE the cross-document layer, but projecting them into Neo4j (entity nodes
  linking their filings) is a separate graph-load follow-on, not required for the resolve API.
- **Numeric roll-ups across versions** ("Revenue grew 12% v1→v2") — blocked on real numeric-value
  extraction, deferred since SP-11 §6 / SP-15 §6; `concept_presence` gives which concepts recur across
  versions, not their values.
- **Incremental / cached resolution** — recomputing the full clustering each call is acceptable at this
  scale (decision 4); caching by corpus-content hash is a scale optimization, not needed now.

## 7. Done criteria

1. `fdi.core.crossdoc` provides pure `document_vector`, `cosine`, `cluster`, `consolidate_entity` per
   §3.1: mean-pool+normalize; guarded cosine; deterministic single-linkage connected components;
   version-ordered entity record with `concepts` union + `concept_presence` (cross-doc refs) +
   `quality_trend`. All §4 error cases degrade (never raise); `cluster` output is order-independent
   (tested); `core` stays pure (no I/O/config/clock/RNG, no import of `fdi.core.knowledge`/`embeddings`).
2. `fdi.pipeline.crossdoc.resolve_entities` enumerates the corpus (new
   `RegistryRepository.list_documents()`), reads each document's `embedding`+`knowledge` via the existing
   `latest_run`→`get_json` path, clusters, and returns `{schema_version, threshold, entity_count,
   entities, unresolved}`; documents without a usable embedding land in `unresolved` (decision 9),
   members without a knowledge record degrade (decision 10).
3. `POST /entities/resolve` returns the entity view for the corpus, honouring an optional in-body
   `threshold` override (else the `FDI_ENTITY_SIM_THRESHOLD` = 0.85 default via lifespan→`app.state`→dep);
   out-of-range threshold → 422; empty corpus → `entity_count: 0` with no 503/raise. The integration test
   proves 2 entities from a 3-document corpus (two near-identical + one distinct), the override path
   (0.99 → 3 singletons), and an un-embedded document surfacing in `unresolved`.
4. `FDI_ENTITY_SIM_THRESHOLD` is mirrored across `config.py`, `compose.yml`, and `.env.example` (inert-
   config discipline); `test_config.py` asserts the 0.85 default.
5. All four static gates green (`ruff`, `mypy --strict src`, `lint-imports` "1 kept, 0 broken", full
   non-e2e suite); the layering contract holds with the new `fdi.pipeline.crossdoc` importing
   `fdi.platform` + `fdi.core` (precedented by `intake`/`eval_service`); no new Stage is registered (entity
   resolution is corpus-level by design — decision 8); Milvus-ANN scale, materialized persistence,
   content-name resolution, cross-doc Neo4j merge, and numeric roll-ups remain explicitly out of scope
   (§6). SP-16 being the final roadmap SP, the roadmap's status line flips to reflect the ingestion layer
   complete.
