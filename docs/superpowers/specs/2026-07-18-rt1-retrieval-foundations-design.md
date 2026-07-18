# RT-1 — Retrieval Foundations (unified chunk record + filterable index)

- **Status:** Implemented — 2026-07-18. First sub-project of the retrieval layer (`docs/architecture/
  retrieval-roadmap.md`). The spine every text-returning retrieval tool depends on.
- **Depends on (ingestion, all complete):**
  - **SP-8** (`ast` artifact `{title, sections:[{heading, paragraphs:[str]}]}` — the ONLY store of
    untruncated section body text; `fdi.core.ast`).
  - **SP-10** (`semantic` artifact `{sections:[{heading, type, paragraph_count}]}`, `type` ∈ 8
    `SectionType`s, produced from `ast` in section order).
  - **SP-12** (`provenance` artifact `{node_provenance:[{node_id, page, bounding_regions}]}`; a
    section's page is the record whose `node_id == "s{i}"`).
  - **SP-13** (`embedding` artifact `{chunks:[{chunk_id, heading, vector}]}`, `chunk_id = "s{i}"`,
    one chunk per AST section — the id RT-1 must stay aligned with).
  - **SP-14** (`IndexStage`, `deps=("embedding",)`, upserts `{id: "{doc_id}:{chunk_id}", vector,
    metadata:{heading, chunk_id}}` into the `VectorStore`; RT-1 enriches that metadata).
  - **SP-2** (stage/DAG framework; `StageContext` exposes `doc_id`, `read(stage)`, `services` — NO
    Postgres/repo access, which shapes decision 5).

## 1. Purpose

The ingestion layer stores **vectors and ids, but no retrievable text**. `POST /search` returns
`{doc_id}:s3` + a heading; turning that into the actual passage means re-fetching the document's whole
`ast` and indexing to section 3 — work every future retrieval tool (vector, keyword, hybrid, graph,
agent) would otherwise re-implement. And Milvus metadata is only `{heading, chunk_id}`, so a search
cannot be scoped to a statement type. RT-1 builds the **foundation**:

1. A per-document **`chunks` artifact** — one record per AST section joining the section's **text**
   (from `ast`), **section type** (from `semantic`), and **page** (from `provenance`) under the
   shared chunk identity `s{i}`. This is the single place any hit (`{doc_id}:s{i}`) resolves to its
   text and citation.
2. **Enriched Milvus metadata** — `IndexStage` gains `section_type` and `page` (from `chunks`) so a
   vector search can filter by statement type at the vector layer.
3. A **`resolve_chunk(store, repo, doc_id, chunk_id)`** helper — the one function RT-2…RT-8 call to
   turn a hit id into `{doc_id, chunk_id, heading, text, section_type, page}` + citation.

Thin slice, pure-core-first, same shape as every ingestion SP: a pure `fdi.core.retrieval.build_chunks`
join + exhaustive unit tests, then a `ChunksStage` (`deps=("ast","semantic","provenance")`) that
auto-chains via the existing DAG, then the `IndexStage` enrichment, then the resolver helper.

## 2. Scope decisions (autonomous; documented)

1. **`chunks` is a NEW derived artifact, not enrichment of an existing one.** A per-document stage
   output `{schema_version:1, chunks:[{chunk_id, heading, text, section_type, page, para_count}]}`,
   content-addressed like every stage. It is the join of three artifacts that already exist and are
   already this shape — no new OCR/embedding call — the same "join existing artifacts" posture as
   `knowledge`/`provenance`.
2. **Chunk identity = `"s{i}"`, produced by the SAME `enumerate`-and-skip rule `EmbeddingStage`
   uses** (`stages/embedding.py`): iterate `ast["sections"]` with `enumerate`; a non-`dict` section is
   skipped entirely (produces no chunk) but still consumes its index `i`, so a later real section
   keeps the `s{i}` its vector already has in Milvus. This exact-alignment is load-bearing: RT-2 joins
   a Milvus hit `{doc_id}:s{i}` back to `chunks[…].chunk_id == "s{i}"`, so `build_chunks` MUST NOT
   re-index or compact. (Verification step in §5.)
3. **`text` is the section body exactly as embedded**: `heading + "\n" + "\n".join(paragraphs)` (the
   string `EmbeddingStage` embeds), so the retrieved text is what the vector actually represents. A
   missing/`None` heading contributes `""` (matching embedding's `heading = heading_raw if isinstance
   str else ""`). `para_count` = number of `str` paragraphs (for display/snippeting).
4. **`section_type` is joined from `semantic` by position among counted sections, with a `None`
   fallback.** `semantic["sections"]` is produced from the same `ast` in the same order; RT-1 aligns
   the k-th chunk to `semantic["sections"][k]["type"]` where k is the chunk's position in the emitted
   chunk list. **Because alignment is positional, `build_chunks` is defensive**: if `semantic` has
   fewer sections, or the entry's `type` is missing/non-`str`, that chunk's `section_type` is `None`
   (never guessed, never raises). §5 requires the executor to confirm `SemanticStage` emits one entry
   per counted AST section in order before relying on the alignment; if that assumption is ever false
   the fallback keeps output well-formed (type just becomes `None`).
5. **`page` is joined from `provenance` by `node_id == chunk_id`** (`"s{i}"`). `provenance`'s
   `node_provenance` may omit a node (no geometry match); a chunk with no matching provenance record
   gets `page: None`. The first `bounding_regions` entry is NOT copied into `chunks` in this slice
   (page number is enough for a citation label; full bbox stays reachable via the `provenance`
   artifact and is RT-5's concern) — keeps `chunks` small.
6. **`IndexStage` enrichment reads the `chunks` artifact** (`deps` becomes `("embedding","chunks")`,
   `version` bumped `1 → 2`). Metadata upserted per vector becomes `{heading, chunk_id, section_type,
   page}` (adds `section_type`, `page`). `doc_key`/`version` are deliberately NOT added here —
   `StageContext` has no Postgres access (decision confirmed against `stage.py`), and RT-2 filters
   those via a Postgres join on the hit's `doc_id` instead. The `version` bump re-runs indexing for
   existing documents (new `input_hash`), re-upserting with the richer metadata — acceptable and
   intended.
7. **`resolve_chunk` lives in `fdi.pipeline`** (`fdi.pipeline.retrieval`), not `core`: it does I/O
   (reads the `chunks` artifact via `latest_run`→`get_json`). Signature `resolve_chunk(*, session,
   store, doc_id, chunk_id) -> dict | None` — returns the chunk record (with `doc_id` added) or `None`
   if the document has no `chunks` artifact or the `chunk_id` is absent. A batch `resolve_chunks(*,
   session, store, ids: list[str])` (ids of the `"{doc_id}:{chunk_id}"` form) resolves a whole search
   result set in one pass, grouping by `doc_id` so each document's `chunks` artifact is read once.
8. **No new HTTP route in RT-1.** RT-1 is foundation only; the first retrieval endpoint is RT-2. (A
   thin `GET /documents/{id}/chunks` is already covered by the generic `GET /documents/{id}/artifacts/
   chunks` reader — no bespoke route needed.)

## 3. Architecture

### 3.1 `fdi.core.retrieval` (pure)

```python
def build_chunks(
    ast: Mapping[str, object],
    semantic: Mapping[str, object],
    provenance: Mapping[str, object],
) -> dict[str, object]:
    """Join ast (text) + semantic (section_type) + provenance (page) into a
    per-document chunk record. Chunk ids ("s{i}") mirror EmbeddingStage's
    enumerate-and-skip exactly. Pure; degrades per-field on missing/malformed
    input (section_type/page -> None), never raises."""
```

Algorithm:
```
page_by_node = {}
for np in provenance.get("node_provenance", []):     # skip non-Mapping / missing node_id
    if Mapping and isinstance(np.get("node_id"), str) and isinstance(np.get("page"), int-not-bool):
        page_by_node[np["node_id"]] = np["page"]

sem_sections = semantic.get("sections") if list else []

chunks = []
k = 0                                                 # position among EMITTED chunks
for i, section in enumerate(ast.get("sections", [])): # i drives chunk_id, mirrors embedding
    if not isinstance(section, Mapping):
        continue                                      # consumes index i, emits nothing
    heading_raw = section.get("heading")
    heading = heading_raw if isinstance(heading_raw, str) else ""
    paragraphs = [p for p in section.get("paragraphs", []) if isinstance(p, str)] if list else []
    text = heading + "\n" + "\n".join(paragraphs)
    chunk_id = f"s{i}"

    section_type = None
    if k < len(sem_sections) and isinstance(sem_sections[k], Mapping):
        t = sem_sections[k].get("type")
        section_type = t if isinstance(t, str) else None

    chunks.append({
        "chunk_id": chunk_id,
        "heading": heading_raw if isinstance(heading_raw, str) else None,
        "text": text,
        "section_type": section_type,
        "page": page_by_node.get(chunk_id),
        "para_count": len(paragraphs),
    })
    k += 1

return {"schema_version": 1, "chunks": chunks}
```

### 3.2 `ChunksStage` (pipeline)

```python
class ChunksStage:
    name = "chunks"
    version = 1
    deps: tuple[str, ...] = ("ast", "semantic", "provenance")

    async def run(self, ctx: StageContext) -> object:
        ast = cast(Mapping[str, object], await ctx.read("ast"))
        semantic = cast(Mapping[str, object], await ctx.read("semantic"))
        provenance = cast(Mapping[str, object], await ctx.read("provenance"))
        return build_chunks(ast, semantic, provenance)
```

`deps=("ast","semantic","provenance")` → the resolver makes `chunks` a successor of all three;
`input_hash = derived_input_hash([ast.output_hash, semantic.output_hash, provenance.output_hash])`.
Registered in `stages/__init__.py`.

### 3.3 `IndexStage` enrichment (v2)

`deps = ("embedding", "chunks")`, `version = 2`. Build a `{chunk_id: {section_type, page}}` lookup
from the `chunks` artifact and merge into each upserted item's metadata:
```python
meta_by_chunk = {c["chunk_id"]: c for c in chunks_artifact["chunks"] if isinstance(c, Mapping)}
...
"metadata": {
    "heading": chunk.get("heading"),
    "chunk_id": chunk["chunk_id"],
    "section_type": meta_by_chunk.get(chunk["chunk_id"], {}).get("section_type"),
    "page": meta_by_chunk.get(chunk["chunk_id"], {}).get("page"),
}
```
`FakeVectorStore` already stores arbitrary metadata dicts; `MilvusVectorStore._ensure_collection`
flattens metadata keys to columns — the executor confirms `section_type`/`page` are accepted (they are
scalar str/int/None, same shape as the existing `heading`) or added to the collection schema if the
Milvus store enumerates a fixed metadata column set (§5).

### 3.4 `fdi.pipeline.retrieval` (orchestrator helper)

```python
async def resolve_chunk(*, session, store, doc_id: str, chunk_id: str) -> dict[str, object] | None
async def resolve_chunks(*, session, store, ids: list[str]) -> dict[str, dict[str, object]]
```
`resolve_chunk` reads the doc's latest `chunks` artifact (`RegistryRepository.latest_run` → `get_json`,
same path as `resolve_entities`), returns the matching chunk dict with `"doc_id"` added, or `None`.
`resolve_chunks` parses each `"{doc_id}:{chunk_id}"` id, groups by `doc_id`, reads each document's
`chunks` artifact once, and returns `{full_id: chunk_record}` (omitting ids that don't resolve). This is
the batch primitive RT-2's `vector_search` uses to attach text to a whole hit list in one pass.

## 4. Error handling

- Missing/non-list `ast["sections"]` → `chunks: []`. A non-`Mapping` section is skipped but consumes
  its `s{i}` index (decision 2). Never raises.
- Missing/short `semantic["sections"]`, or a non-`str` `type` → that chunk's `section_type: None`.
- Missing `provenance` record for `"s{i}"`, or non-`int` page → `page: None`.
- `build_chunks({}, {}, {})` → `{"schema_version":1, "chunks":[]}` (fully defined, tested).
- `resolve_chunk` on a document with no `chunks` artifact, or an unknown `chunk_id` → `None` (not an
  error). `resolve_chunks` omits unresolvable ids rather than raising; a malformed id without a `:` is
  skipped.
- `IndexStage` with zero chunks still skips the upsert (unchanged SP-14 behaviour); a chunk present in
  `embedding` but absent from `chunks` (shouldn't happen — same `ast`) enriches with `section_type:
  None, page: None`, never raises.

## 5. Testing

- **Unit** (pure, no Docker), `backend/tests/unit/test_retrieval_chunks.py`:
  - A 2-section ast + aligned semantic (types `BALANCE_SHEET`, `NOTES`) + provenance (`s0`→page 3,
    `s1`→page 7) → two chunks with correct `chunk_id`/`heading`/`text`/`section_type`/`page`/`para_count`.
  - `text` equals `heading + "\n" + "\n".join(paragraphs)` exactly (byte-identical to what
    `EmbeddingStage` embeds — assert against the same construction).
  - **Enumerate-alignment:** an `ast` whose section index 1 is a non-`dict` (skipped) → the third real
    section keeps `chunk_id == "s2"` (NOT `"s1"`), proving decision 2's index preservation.
  - `semantic` shorter than `ast` sections, and a section whose `type` is non-`str` → those chunks get
    `section_type: None`, others correct.
  - `provenance` missing `s1` → that chunk's `page: None`.
  - `build_chunks({}, {}, {})` → `{"schema_version":1, "chunks":[]}`.
- **Integration** (testcontainers + fakes), `backend/tests/integration/test_chunks_stage.py`: run
  `ocr → … → provenance/semantic → chunks` via `run_stage` over a LOCAL registry with `FakeOcrClient`
  + `FakeSemanticClient`; assert the `chunks` artifact matches the expected records and that
  `"chunks" in resolver.successors("ast")`, `…("semantic")`, `…("provenance")` (membership, per the
  ingestion precedent — grep for any exact-list successor assertion and make it membership).
- **Integration**, `backend/tests/integration/test_index_enrichment.py`: run through `index` (v2) with
  a `FakeVectorStore`; assert the upserted item metadata now carries `section_type` and `page` from
  `chunks`, and that the Milvus id is still `{doc_id}:s{i}`. Confirm the `index` stage_run re-runs
  (new `input_hash`) after the version bump.
- **Integration**, `backend/tests/integration/test_resolve_chunk.py`: intake + process one doc; assert
  `resolve_chunk(...s0)` returns the full record with `doc_id`; an unknown `chunk_id` → `None`;
  `resolve_chunks(["{id}:s0", "{id}:s1", "bogus"])` returns two records keyed by full id, omits bogus,
  and reads the doc's `chunks` artifact once.
- **Config/registry:** confirm `stages/__init__.py` registers `chunks`; no exact-list `successors(...)`
  assertion elsewhere breaks (grep-and-fix per ingestion precedent).
- **Executor pre-checks (verify, don't assume):** (a) `SemanticStage` emits one `sections` entry per
  counted AST section in AST order (read `stages/semantic.py`) so decision 4's positional alignment
  holds; (b) `MilvusVectorStore` accepts new scalar metadata keys `section_type`/`page` or its
  collection schema is extended to include them (read `platform/vectorstore.py`); document findings in
  the stage docstring.

## 6. Explicitly deferred (YAGNI)

- **`doc_key`/`version` in Milvus metadata** — RT-2 filters these via a Postgres join on the hit's
  `doc_id` (the stage has no Postgres access); pushing them into the vector layer is a later
  optimization only if the join proves hot.
- **Bounding-box in `chunks`** — page number suffices for a citation label; full bbox highlighting is
  RT-5's graph/provenance concern, reachable from the `provenance` artifact.
- **`concepts` per chunk** — linking `CanonicalConcept`s to a specific section (fuzzy label↔section
  matching) is RT-6's structured-financial concern, not this foundation.
- **A dedicated `chunk` search route** — RT-1 exposes no HTTP; the artifact is readable via the
  existing generic reader, and `resolve_chunk` is an internal primitive for RT-2…RT-8.
- **Paragraph-level chunks** — one chunk per section (matching the embedding granularity) is the unit;
  finer chunking is a retrieval-quality experiment for later, not a foundation change.

## 7. Done criteria

1. `fdi.core.retrieval.build_chunks(ast, semantic, provenance)` returns `{schema_version:1, chunks:[…]}`
   per §3.1: text = the exact embedded string; `chunk_id` mirrors `EmbeddingStage`'s enumerate-and-skip
   (verified by the skipped-section test); `section_type`/`page` joined with `None` fallback; all §4
   cases degrade, never raise; `build_chunks({}, {}, {})` == `{"schema_version":1,"chunks":[]}`; `core`
   stays pure (no I/O, no import of `fdi.core.ast`/`semantic`/`provenance`).
2. `ChunksStage` (`deps=("ast","semantic","provenance")`, `version=1`) produces the `chunks` artifact
   and is registered; `DagResolver(REGISTRY.all()).successors("ast"/"semantic"/"provenance")` all
   include `"chunks"`.
3. `IndexStage` (`version=2`, `deps=("embedding","chunks")`) upserts metadata enriched with
   `section_type` + `page`, id still `{doc_id}:{chunk_id}`; re-runs for existing docs after the version
   bump.
4. `resolve_chunk` / `resolve_chunks` (`fdi.pipeline.retrieval`) turn a `{doc_id}:{chunk_id}` hit into
   its full chunk record (or `None`/omission), reading each document's `chunks` artifact once.
5. All four gates green (`ruff`, `mypy --strict`, `lint-imports` 1-kept/0-broken, full suite); the two
   executor pre-checks (§5) are verified and documented; deferred items (§6) remain out of scope; the
   retrieval roadmap's RT-1 row flips to done with a "what RT-1 shipped" handoff (the `chunks` artifact
   shape, the `s{i}` identity contract, `resolve_chunks` as RT-2's text-attach primitive, enriched
   Milvus metadata).
