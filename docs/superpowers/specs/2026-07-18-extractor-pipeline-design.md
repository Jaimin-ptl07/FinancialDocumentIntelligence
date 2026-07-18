# Extractor Pipeline — schema-driven RAG field extraction

- **Status:** Design — 2026-07-18. The layer ABOVE retrieval: given a document and the field-extraction
  schema, it retrieves candidate context per field, reranks it, prompts Azure OpenAI to extract the
  value, and returns JSON with the source. User-selected shape: (1) **schema-driven batch** (loop every
  `rag_extractable` field of a module/document), (2) **model-free blend rerank**, (3) **hybrid +
  tables/concepts candidate pool**.
- **Depends on (retrieval layer RT-1…RT-8, complete):**
  - **RT-4** `fdi.pipeline.retrieval.hybrid_search(*, session, store, embedder, vector_store, query, k,
    filters)` — the primary candidate-pool source (RRF vector+keyword, citation-carrying).
  - **RT-6** `fdi.pipeline.tables.get_tables`/`concept_lookup` — the numeric-field pool enrichment.
  - **RT-8** the LLM-seam precedent (`fdi.pipeline.agent` Protocol + `FakeAgentClient`;
    `fdi.platform.agent.AzureOpenAIAgentClient`, monkeypatchable, `from_settings` returns `None` when
    `FDI_GPT_*` unset). The extractor's `ExtractorClient` mirrors this exactly.
  - **The schema** `docs/architecture/field-extraction-schema.json` (v2.0.0): a module-wise catalogue;
    each field carries `field_name`, `source_type`, `rag_extractable`, `data_type`, `retrieval_query`,
    `keywords`, `extraction_prompt`, `enum_values?`, `normalization`, `source_hint`. Output contract:
    `{value, source:{page, snippet, evidence_ids}, confidence}`. Only `rag_extractable=true` +
    `source_type="document_content"` fields are answered by this pipeline; the rest
    (metadata/computed/constant/external_lookup/not_extracted) are pipeline-sourced, out of scope here.

## 1. Purpose

The retrieval layer built seven tools that FIND relevant context; nothing yet turns "find context about
the Balance Sheet" into the actual extracted value `{value: [145], source: {...}, confidence: 0.9}`.
The extraction schema already specifies, per field, exactly how: a `retrieval_query` (+ `keywords`) to
gather context and an `extraction_prompt` to answer from it, returning value + source + confidence. The
extractor is the thin, schema-driven pipeline that executes that contract over the retrieval tools:

```
for each rag_extractable document_content field:
  pool     = hybrid_search(field.retrieval_query, {doc_key})  (+ get_tables/concept_lookup if numeric)
  ranked   = rerank(pool, field.keywords)[:top_k]             (model-free blend, pure)
  result   = ExtractorClient.extract(field.extraction_prompt, ranked)  → {value, source, confidence}
return {field_name: result, ...}
```

Same discipline as retrieval: pure algorithms in `fdi.core.extractor` (spec parsing, rerank, prompt
assembly), an orchestrator in `fdi.pipeline.extractor`, the real LLM in `fdi.platform.extractor`, a
route in `fdi.api`; four green gates; a mockable LLM seam so the whole pipeline is tested offline.

## 2. Scope decisions (autonomous unless marked user-selected; documented)

1. **Schema-driven batch** *(user-selected)*. `extract_document(*, doc_key, field_specs, module?)`
   loops every field, calling the single-field primitive `extract_field`. A `module` filter bounds the
   run to one schema module (e.g. only "1-statement-identification") — important because a full
   document is many LLM calls (one per field); the module filter keeps a request bounded and cheap.
2. **Only `rag_extractable=true` AND `source_type="document_content"` fields are extracted.**
   `parse_field_specs` filters the rest out entirely (metadata/computed/constant/external_lookup/
   not_extracted are pipeline-sourced — the schema's own `routing` note says so). A filtered-out field
   never reaches retrieval or the LLM.
3. **Model-free blend rerank** *(user-selected)*, pure (`fdi.core.extractor.rerank_candidates`):
   `score = retrieval_score + keyword_weight * keyword_overlap`, where `keyword_overlap` is the
   fraction of the field's `keywords` (casefolded) that appear as substrings in the candidate's text
   (0.0 when the field has no keywords — then rerank is a stable sort by retrieval_score alone). Sort
   descending, stable (ties keep retrieval order), take `top_k`. `keyword_weight` default `1.0`,
   `top_k` default `8` — module-level constants, no new config unless tuning proves necessary. Pure and
   deterministic → exhaustively unit-tested.
4. **Hybrid + tables/concepts pool** *(user-selected)*. `pool = hybrid_search(retrieval_query,
   filters={"doc_key": doc_key}, k=pool_k)`; **if the field is numeric** (`data_type ∈ {decimal,
   integer}` per `is_numeric`), additionally fetch `get_tables(doc_id)` for the document's canonicalized
   table grids and fold each grid (rendered to text, carrying its page/table_index) into the pool as
   extra candidates, so the LLM sees actual cell values. `concept_lookup` is used only when the field
   maps to a `CanonicalConcept` (best-effort, decision 5) — most schema fields do not, so `get_tables`
   is the primary numeric enrichment. Candidates from different tools are normalized to one shape
   (decision 6) before rerank.
5. **`concept_lookup` is best-effort, not required.** A schema field is NOT a `CanonicalConcept`;
   there is no reliable field→concept map. For numeric fields whose `retrieval_query`/`keywords`
   clearly name a canonical concept, a small optional lookup MAY add its rows — but the default
   numeric enrichment is `get_tables`. If no concept matches, nothing is added and the field still
   extracts from `hybrid_search` + tables. (Wiring a field→concept map is deferred to a follow-on.)
6. **Unified candidate shape** (`fdi.core.extractor`): every pool entry, whatever tool produced it, is
   normalized to `{evidence_id, text, page, score, source_tool}` — `evidence_id` is the chunk id
   (`{doc_id}:{chunk_id}`) for hybrid hits or `table:{doc_id}:{table_index}` for a table; `page` is the
   citation page (or `None`); `score` is the tool's own score (hybrid: RRF score; table: a fixed base
   so tables enter the pool but don't dominate). Rerank operates on this shape only — decoupled from
   each tool's native output.
7. **The `ExtractorClient` seam mirrors RT-8's agent seam.** Protocol `extract(*, system: str, user:
   str) -> Mapping[str, object]` in `fdi.pipeline.extractor` (returns the raw `{value, source,
   confidence}` dict the LLM produced); `FakeExtractorClient` (pipeline) replays scripted results;
   `AzureOpenAIExtractorClient` (platform) calls `chat.completions.create(...,
   response_format={"type":"json_object"}, temperature=0)`, monkeypatchable module-level `AzureOpenAI`
   import, `from_settings` returns `None` when `FDI_GPT_*` unset. Reuses `FDI_GPT_*` — no new LLM
   config.
8. **The output per field follows the schema's `output_contract`**: `{value, source: {page, snippet,
   evidence_ids}, confidence}`. `value` is whatever the LLM returned (post-processing/normalization
   against `data_type`/`enum_values` is a documented follow-on — §6 — this slice returns the raw
   extracted value + validates it is JSON-shaped, not that it matches `data_type`). `source.evidence_ids`
   defaults to the `evidence_id`s of the reranked candidates fed to the LLM (the LLM MAY narrow them);
   `source.page`/`snippet` come from the LLM's answer, falling back to the top candidate's page/text.
   `confidence` is the LLM's, clamped to `[0.0, 1.0]`, defaulting to `0.0` when absent/malformed.
9. **Schema loading is config-pathed, parsed in core, read in the route.** `FDI_EXTRACTION_SCHEMA_PATH`
   (config trio; default `docs/architecture/field-extraction-schema.json`) names the JSON file. The
   route reads the file (I/O), `fdi.core.extractor.parse_field_specs(schema_dict, module?)` parses it
   (pure). `extract_document` takes the already-parsed `list[FieldSpec]` — so the pipeline function is
   data-driven and tests pass a tiny in-memory schema, never touching the 2666-line file.
10. **A field that finds no context still returns a well-formed record** — `{value: None, source:
    {page: None, snippet: None, evidence_ids: []}, confidence: 0.0}` — never raises, never omits the
    field. A tool that errors for one field (e.g. vector store unconfigured) degrades that field's pool
    (fewer candidates), not the whole batch. `extract_field` never raises; `extract_document` collects
    a result for every field.

## 3. Architecture

### 3.1 `fdi.core.extractor` (pure)

```python
@dataclass(frozen=True)
class FieldSpec:
    field_name: str
    data_type: str
    retrieval_query: str
    keywords: tuple[str, ...]
    extraction_prompt: str
    enum_values: tuple[str, ...] | None
    module_id: int
    module_name: str

@dataclass(frozen=True)
class Candidate:
    evidence_id: str
    text: str
    page: int | None
    score: float
    source_tool: str

def parse_field_specs(schema: Mapping[str, object], module: str | None = None) -> list[FieldSpec]:
    """Flatten the schema's modules → FieldSpec list, keeping only
    rag_extractable=true AND source_type='document_content'; optional module filter
    (by module name or folder). Pure; skips malformed field entries, never raises."""

def is_numeric(data_type: str) -> bool:      # data_type in {"decimal", "integer"}

def keyword_overlap(text: str, keywords: Sequence[str]) -> float:
    """Fraction of keywords (casefolded) present as substrings in text; 0.0 if no keywords."""

def rerank_candidates(
    candidates: Sequence[Candidate], keywords: Sequence[str], *, top_k: int = 8,
    keyword_weight: float = 1.0,
) -> list[Candidate]:
    """Stable-sort by (score + keyword_weight * keyword_overlap(text, keywords)) desc; take top_k."""

def build_extraction_messages(field: FieldSpec, candidates: Sequence[Candidate]) -> tuple[str, str]:
    """(system, user) messages. system: extract ONLY from context, return JSON
    {value, source:{page,snippet,evidence_ids}, confidence}, value=null if absent.
    user: field.extraction_prompt + each candidate labeled with its evidence_id + page + text."""

def assemble_result(field, raw: Mapping[str, object], ranked: Sequence[Candidate]) -> dict:
    """The LLM's raw dict → the output_contract record, with evidence_ids/page/snippet/
    confidence fallbacks (decision 8) and clamped confidence. Pure; never raises."""
```

### 3.2 `fdi.pipeline.extractor` (orchestrator + seam)

```python
@runtime_checkable
class ExtractorClient(Protocol):
    def extract(self, *, system: str, user: str) -> Mapping[str, object]: ...

class FakeExtractorClient:  # scripted results for the default suite
    def __init__(self, results: list[Mapping[str, object]]) -> None: ...

@dataclass
class ExtractContext:       # what execute needs, built by the route from deps
    session: AsyncSession
    store: ArtifactStore
    embedder: EmbeddingClient | None
    vector_store: VectorStore | None
    doc_id: str             # resolved from doc_key (latest version)

async def extract_field(*, ctx: ExtractContext, field: FieldSpec,
                        client: ExtractorClient) -> dict[str, object]:
    """pool (hybrid_search + get_tables if numeric) → Candidate list → rerank_candidates →
    build_extraction_messages → client.extract → assemble_result. Never raises (decision 10)."""

async def extract_document(*, ctx: ExtractContext, fields: Sequence[FieldSpec],
                           client: ExtractorClient) -> dict[str, object]:
    """Loop fields, call extract_field; return {schema_version:1, doc_key, doc_id, module,
    field_count, fields:[{field_name, value, source, confidence}, ...]}."""
```

- Pool assembly: call `hybrid_search`; map each hit to `Candidate(evidence_id=f"{doc_id}:{chunk_id}",
  text, page, score, "hybrid")`. If `is_numeric(field.data_type)`: call `get_tables(doc_id)`, render
  each grid to text, add `Candidate(evidence_id=f"table:{doc_id}:{table_index}", text=grid_text,
  page, score=_TABLE_BASE_SCORE, "table")`.
- `doc_key`→`doc_id`: `DocumentRepository.list_by_doc_key(doc_key)` → latest version's `id` (the route
  resolves this; 404 if the doc_key is unknown).

### 3.3 `fdi.platform.extractor` (real LLM)

`AzureOpenAIExtractorClient` — `extract(*, system, user)` → `chat.completions.create(model=deployment,
messages=[system,user], response_format={"type":"json_object"}, temperature=0)`, parse `choices[0].
message.content` as JSON (returns `{}` on unparseable, so `assemble_result` degrades gracefully).
Module-level `AzureOpenAI` import (monkeypatchable). `from_settings(settings) -> … | None` (None when
`gpt_endpoint`/`gpt_key` unset). Reuses `FDI_GPT_*`.

### 3.4 API — `POST /extract/document`

- Request `ExtractRequest`: `{doc_key: str, module: str | None = None}`.
- Handler: 503 if the injected `ExtractorClient` is `None` (like `/ask`/`/search`); resolve
  `doc_key`→`doc_id` (404 if unknown); read `FDI_EXTRACTION_SCHEMA_PATH`, `parse_field_specs(schema,
  module)`; build `ExtractContext` from `SessionDep`/`StoreDep`/`EmbedderDep`/`VectorStoreDep`; call
  `extract_document`; return the record.
- Wiring: `app.py` lifespan builds `app.state.extractor_client = AzureOpenAIExtractorClient.
  from_settings(settings)` and `app.state.extraction_schema_path = settings.extraction_schema_path`;
  `deps.py` adds `ExtractorClientDep` + a schema-path dep (same lifespan→state→dep pattern).

### 3.5 Config

`Settings.extraction_schema_path: str = "docs/architecture/field-extraction-schema.json"`
(`FDI_EXTRACTION_SCHEMA_PATH`), mirrored across `config.py` / `compose.yml` / `.env.example` (inert-config
trio). No new LLM config — reuses `FDI_GPT_*`.

## 4. Error handling

- Unknown `doc_key` → 404 (route). Empty/whitespace `retrieval_query` → empty pool → `value: None`.
- Unconfigured vector store → `hybrid_search` degrades to keyword-only (RT-4 already); if the whole
  pool is empty, `value: None`, never an error.
- `get_tables` returns no tables / the field isn't numeric → no table candidates added.
- LLM returns non-JSON / missing keys → `assemble_result` fills `value: None`, `confidence: 0.0`,
  `evidence_ids` = the fed candidates' ids (decision 8). Never raises.
- A malformed schema field (missing `retrieval_query`/`extraction_prompt`) → skipped by
  `parse_field_specs`, not extracted.
- `extract_field` never raises; `extract_document` always returns one record per surviving field.
- Empty module (no rag_extractable document_content fields) → `field_count: 0, fields: []`.

## 5. Testing

- **Unit** (pure, no Docker), `backend/tests/unit/test_extractor.py`:
  - `parse_field_specs`: a mini-schema with a mix of `rag_extractable`/`source_type`/module → keeps
    only the document_content+rag_extractable fields; module filter selects one module; malformed
    field skipped.
  - `keyword_overlap`: 0/partial/full overlap, casefold, empty keywords → 0.0.
  - `rerank_candidates`: keyword boost changes order; stable on ties; `top_k` truncation; empty input.
  - `build_extraction_messages`: system mentions JSON contract + "only the context"; user contains the
    extraction_prompt and each candidate's evidence_id/page/text.
  - `assemble_result`: full LLM dict passthrough; missing keys → `value None`/`confidence 0.0`/
    `evidence_ids` from candidates; confidence clamp (`1.7`→`1.0`, `"x"`→`0.0`); page/snippet fallback.
  - `is_numeric` for each data_type.
- **Integration** (testcontainers + fakes), `backend/tests/integration/test_extract_document.py`:
  intake + fully process one doc (FakeOcr with a headed section + a numeric table); `FakeExtractorClient`
  scripted to return a value citing a real evidence_id; run `extract_document` with a 2-field in-memory
  schema (one text field, one numeric field) → assert both fields returned, the numeric field's pool
  included a `table:` candidate, values/sources/confidence match the script, and the whole thing made
  NO Azure call. A field with an unmatchable query → `value None`, well-formed.
- **API** (`test_api.py`): `POST /extract/document` happy path (scripted FakeExtractorClient via the
  app fixture) → 200 with the record; unknown `doc_key` → 404; unconfigured client → 503.
- **Opt-in real Azure** (`@pytest.mark.azure`, `tests/azure/test_real_extraction.py`): the real
  `AzureOpenAIExtractorClient` against one field, asserting a JSON-shaped result — skipped without
  `FDI_RUN_AZURE_TEST=1`, mirroring `test_real_semantic.py`.
- **Config**: `test_config.py` asserts `extraction_schema_path` default.

## 6. Explicitly deferred (YAGNI)

- **Post-processing / normalization + `data_type`/`enum_values` validation** — the schema's step 4.
  This slice returns the LLM's raw `value` + validates JSON shape; applying each field's `normalization`
  rule and coercing/validating against `data_type`/`enum_values` is a clean follow-on over the returned
  record.
- **Field→`CanonicalConcept` mapping for `concept_lookup`** (decision 5) — `get_tables` is the numeric
  enrichment for now.
- **Whole-document (all-modules) batch, parallel field extraction, caching, persistence** — the route
  is per-module and sequential; parallelizing fields, caching results per (doc, field, schema-version),
  and persisting extracted records are scale/perf follow-ons.
- **The non-RAG fields** (metadata/computed/constant/external_lookup) — filled by the ingestion/outer
  pipeline from filename/upstream sources, never by this extractor (schema `routing`).
- **LLM rerank / cross-encoder** — the model-free blend is the chosen reranker.

## 7. Done criteria

1. `fdi.core.extractor` (pure) provides `parse_field_specs` (filters rag_extractable+document_content,
   module filter), `keyword_overlap`, `rerank_candidates` (blend, stable, top_k), `is_numeric`,
   `build_extraction_messages`, `assemble_result` (output_contract + fallbacks + confidence clamp) — all
   pure, unit-tested, never raise; `core` imports no pipeline/platform.
2. `fdi.pipeline.extractor` provides the `ExtractorClient` Protocol + `FakeExtractorClient`, and
   `extract_field`/`extract_document` that pool via `hybrid_search` (+ `get_tables` for numeric fields),
   rerank via core, and call the client — never raising, one record per field (decision 10).
3. `fdi.platform.extractor.AzureOpenAIExtractorClient` (JSON-mode, monkeypatchable, reuses `FDI_GPT_*`,
   `from_settings` None-gated).
4. `POST /extract/document {doc_key, module?}` returns `{doc_key, doc_id, module, field_count, fields:[…]}`
   with each field `{field_name, value, source:{page, snippet, evidence_ids}, confidence}`; 404 unknown
   doc_key; 503 unconfigured client. `FDI_EXTRACTION_SCHEMA_PATH` in the config trio; default schema is
   the committed docs file.
5. All four gates green (`ruff`, `mypy --strict`, `lint-imports` 1-kept/0-broken, full suite); the
   default suite makes NO Azure call (FakeExtractorClient) and needs no Milvus/Neo4j (fakes); real Azure
   only behind `@pytest.mark.azure`; deferred items (§6) out of scope.
