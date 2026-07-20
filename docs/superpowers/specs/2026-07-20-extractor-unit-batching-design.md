# EX-1 — Unit-Batched Extraction + Retrieval Scoping

- **Status:** Designed — 2026-07-20. Closes the gap between the *measured*
  field-extraction accuracy (87.4% current-period, 173/198, produced by an
  out-of-repo audit harness) and what `fdi.pipeline.extractor` actually does
  when the `/extract/document` route runs.
- **Depends on:**
  - **RT-4** (`docs/superpowers/specs/2026-07-18-rt4-hybrid-retrieval-design.md`,
    implemented): `fdi.pipeline.retrieval.hybrid_search(*, session, store,
    embedder, vector_store, query, k, filters=None)` — the sole retrieval
    channel `extract_field` pools from today.
  - **RT-1** (`fdi.core.retrieval`, implemented): `build_chunks` and the
    `s{i}` / `t{i}` / `t{i}r{j}` chunk identities. `find_consolidated_start_page`
    (below) reads the `chunks` artifact those ids live in.
  - **Schema v3.1** (`docs/architecture/field-extraction-schema.json`,
    parsed by `fdi.core.extractor.parse_field_specs`): three-level nesting
    `modules -> retrieval_units -> fields`, 16 / 18 / 198 respectively, whose
    own `retrieval_model` states *"Retrieve-once, extract-many. Retrieval is
    per Retrieval Unit, not per field."* The shipped extractor does not honour
    this; that is the defect this spec closes.

## 1. Purpose

Schema v3.1 declares retrieval as a **per-unit** operation. `extract_field`
implements it **per field**: one `hybrid_search` and one `client.extract` for
each of 198 fields, with none of the retrieval scoping the audit harness proved
necessary. Two consequences:

1. **The 87.4% was never measured on shipped code.** The harness applied a
   consolidated-section page filter, a narrative-only table filter, and an
   adaptive `top_k`; `pipeline/extractor.py` has none of them. Shipped accuracy
   is unmeasured and expected to be materially lower — without the consolidated
   filter, every primary statement has a duplicate at pages 133+ carrying
   *different values*, and nothing prefers the standalone one.
2. **11× the LLM cost.** 198 extraction calls where 18 suffice.

A layered failure attribution (offline replay, corpus → pool → reranked →
answered) established where the remaining accuracy actually sits. It covers the
**196 scoreable** fields — the two `list` fields (subsidiaries, joint ventures)
have no scalar example variant to search a chunk for, so they are excluded from
containment measurement rather than counted as passes:

| Layer | Meaning | Fields |
|---|---|---:|
| OK | answered correctly | 170 |
| L0 Ingestion | answer in no chunk | 5 |
| L1 Retrieval | in corpus, never pooled | 3 |
| **L2 Reranker** | **in pool, cut before top-K** | **0** |
| L3 LLM | in top-K context, still wrong | 18 |

(The harness reported 173/198 correct; the replay's 170 differs because it
excludes the 2 unscoreable `list` fields and counts one field as a miss whose
ground-truth value the containment probe could not detect in text. The layer
*proportions* are what this spec relies on, not the absolute total.)

**The reranker causes zero failures** — `top_k` is sized so close to the pool
(e.g. RU_BS pool=73/ranked=60; RU_COMPANY pool=27/ranked=27) that it discards at
most 13 candidates and in four units discards nothing. Reranker work is
explicitly out of scope; it cannot buy accuracy on this corpus.

Of the 18 L3 failures, **7 are not failures**: they are correct ISO-8601 dates
(`2024-03-31`) scored against `dd-mm-yyyy` examples (`31-03-2024`) by a
substring comparison. No code change addresses them — the extraction was always
right. The genuine 11 are 3 sign inversions, 5 nulls-despite-context, and 3
wrong-row picks.

## 2. Scope

**In scope**

- **Change A** — unit-batched extraction, plus the retrieval scoping the
  harness proved: consolidated-section filter, narrative-only table filter,
  adaptive `top_k`.
- **Change B** — an accounting-parentheses-are-negative rule in the extraction
  system prompt.

**Explicitly out of scope**

- **Reranker tuning.** Measured at zero attributable failures.
- **A scoring/eval module or an in-repo audit harness.** Decided against:
  evaluation tooling inherits its own bugs into the assessment, and this
  session demonstrated the failure mode twice — the harness's date comparison
  manufactured 7 false failures, and the attribution probe produced 6 more
  false negatives until hardened. Accuracy is assessed by running the system,
  dumping its output JSON, and reading it against the schema's `example`
  values directly.
- **`_UNIT_KEYWORD_BOOST`.** The harness boosted `gross block` /
  `accumulated depreciation` / `net block` into RU_PPE's rerank keywords.
  Schema v3.1 **already carries all three** in `RU_PPE.locator_keywords`, and
  because `keyword_overlap` divides by `len(keywords)`, re-appending them
  inflates the denominator and mildly *dilutes* the score. Not ported.
- **L0 ingestion failures** (5 fields: fragmented address/phone OCR,
  unchunked narrative policy text). Separate ingestion concern.

## 3. Design

### 3.1 `fdi.core.extractor` — pure additions

No I/O, no config, no clock/RNG; every function total, per the module's
existing contract.

```python
FieldSpec += locator_keywords: tuple[str, ...] = ()
```

`FieldSpec.keywords` is `field.aliases + unit.locator_keywords`, deduplicated —
the unit's own keywords are not recoverable from it. Unit-level reranking needs
them alone, so the parser carries them separately. The default `()` keeps
hand-built `FieldSpec`s (tests, synthesizing callers) constructing unchanged.

```python
@dataclass(frozen=True)
class UnitSpec:
    unit_id: str
    unit_title: str
    retrieval_query: str
    locator_keywords: tuple[str, ...]
    fields: tuple[FieldSpec, ...]

def group_fields_by_unit(fields: Sequence[FieldSpec]) -> list[UnitSpec]
def unit_top_k(n_fields: int) -> int
def unit_needs_tables(unit: UnitSpec) -> bool
def find_consolidated_start_page(chunks: Sequence[Mapping]) -> int | None
def filter_candidates(candidates, *, consolidated_start_page, needs_tables) -> list[Candidate]
def build_unit_messages(unit: UnitSpec, candidates: Sequence[Candidate]) -> tuple[str, str]
def assemble_unit_results(unit: UnitSpec, raw: Mapping, ranked: Sequence[Candidate]) -> list[dict]
```

- **`group_fields_by_unit`** — groups by `unit_id`, **order-preserving** on
  first appearance, so `extract_document`'s output can be restored to input
  order. Fields with an empty `unit_id` (a hand-built spec) each become their
  own singleton unit rather than being merged into one bogus group.
- **`unit_top_k`** — `max(20, min(60, n_fields * 2 + 10))`. Reproduces the
  harness exactly.
- **`unit_needs_tables`** — true when any field's `data_type` is `decimal`,
  `integer`, or `list`. `list` is deliberate: subsidiary/JV disclosures are
  AOC-1 style **tables**, and treating them as narrative stripped the very
  chunks they need (a bug the harness hit and fixed).
- **`find_consolidated_start_page`** — case-insensitive scan of chunk
  `heading`/`text` for a consolidated-statement marker (`consolidated balance
  sheet`, `consolidated statement of profit and loss`, `on consolidated
  financial statements`), returning the **minimum** page across matches, or
  `None` when the document has no consolidated section. Replaces the harness's
  hardcoded `page >= 133`, which is document-specific and unshippable. The
  schema never requests consolidated data — the string `consolidat` appears
  **zero** times in it — so dropping that region is safe for every unit.
- **`filter_candidates`** — drops candidates with `page >= consolidated_start_page`
  (skipped entirely when it is `None`) and, when `not needs_tables`, drops
  `t{i}` / `t{i}r{j}` chunks.
- **`assemble_unit_results`** — the batched analogue of `assemble_result`. The
  LLM returns one object keyed by `field_path`; a missing or malformed key
  degrades that field through the existing single-field fallback path, so a
  partial response still yields one well-formed record per field.

### 3.2 `fdi.pipeline.extractor` — orchestration

```python
async def extract_unit(*, ctx, unit, client, consolidated_start_page=None) -> list[dict]
async def extract_field(*, ctx, field, client) -> dict          # signature unchanged
async def extract_document(*, ctx, fields, client) -> dict      # signature unchanged
```

`extract_unit` is the new primitive:

```
top_k = unit_top_k(len(unit.fields))
hybrid_search(unit.retrieval_query, k=top_k * _POOL_OVERFETCH)   # _POOL_OVERFETCH = 2
  -> filter_candidates(consolidated_start_page, needs_tables)
  -> rerank_candidates(unit.locator_keywords, top_k=top_k)
  -> build_unit_messages -> one client.extract -> assemble_unit_results
```

The pool is **over-fetched relative to `top_k`, not fixed**. This matters: the
filters run *between* pooling and reranking, so a fixed `_POOL_K = 60` against
an adaptive `top_k` of up to 60 would leave large units (RU_BS) with fewer
post-filter candidates than `top_k` — strictly less context than the harness
supplied, and a silent recall regression. `top_k * 2` mirrors the harness's
`fuse(k=top_k * 2)` and reproduces its observed pool sizes (RU_BS 73, RU_PL 65,
RU_CF 63). The module-level `_POOL_K = 60` is retained only for the
`extract_field` wrapper's single-field path, preserving its current behaviour.

`extract_field` becomes a thin wrapper that wraps its single `FieldSpec` in a
one-field `UnitSpec` and returns the sole record — preserving its exact current
behaviour and every existing caller and test.

`extract_document` keeps its signature, so `fdi.api.routes.extract_document_route`
(`routes.py:859`) and the route contract are untouched. Internally it now:

1. reads the document's `chunks` artifact **once** and computes
   `find_consolidated_start_page`;
2. `group_fields_by_unit(fields)`;
3. `await extract_unit(...)` per unit, threading the boundary through;
4. flattens and **reorders records to the input `fields` order**, so the
   response ordering is unchanged.

### 3.3 Extraction prompt — parentheses rule

One clause appended to the system prompt built by `build_extraction_messages`
(and shared by `build_unit_messages`): in financial statements a value printed
in parentheses is **negative** — `(12.97)` means `-12.97` — and it must be
returned as a negative number.

Targets the three observed sign inversions: `total_oci` (expected `-12.97`, got
`12.97`), `other_comprehensive_income_cumulative` (`-6.56` → `9.85`), and
`income_tax_on_oci` (`-5.05` → `-4.37`).

## 4. Error handling

The module's "never raises" posture (decision 10) is preserved, with its
granularity moved from field to unit. `extract_unit` guards `hybrid_search` and
`client.extract` independently; either failing degrades **that unit's** fields
to well-formed null-valued records and never propagates. Boundary detection
failing or finding nothing yields `None`, making the filter a no-op. A missing
`chunks` artifact yields `None` likewise — extraction proceeds unscoped rather
than erroring.

**Accepted risk.** Batching concentrates failure: one malformed response can
null 40 fields (RU_BS) where previously it nulled one. This is the design the
harness ran to 87.4%, so it is empirically acceptable, but it is a real
trade-off and the reason transient-error retry belongs at the
`extract_unit` seam.

## 5. Testing

**Pure (unit, no I/O):**

- `group_fields_by_unit` — grouping, order preservation, singleton units for
  blank `unit_id`.
- `unit_top_k` — both clamps (`n=1 -> 20`, `n=40 -> 60`) and the linear middle.
- `unit_needs_tables` — true for `decimal`/`integer`/**`list`**, false for an
  all-`string` unit.
- `find_consolidated_start_page` — marker found (minimum page wins), no
  consolidated section → `None`, malformed/missing chunks → `None`.
- `filter_candidates` — page filter, table filter, both, and the `None`
  boundary no-op.
- `assemble_unit_results` — full response, partially-missing `field_path`
  keys, malformed response → one well-formed record per field regardless.
- `parse_field_specs` — populates the new `locator_keywords`.

**Integration:**

- `test_extract_document.py` — the batching regression: for an N-field,
  M-unit spec set, assert `FakeExtractorClient` received **M** calls, not N.
- Existing per-field assertions retained through the `extract_field` wrapper.
- Output ordering matches input `fields` ordering.

**End-to-end verification:**

Bring up `docker compose --profile core --profile vector up -d` (Milvus is a
separate profile; Neo4j/`graph` is not required — `chunks` depends on the
`provenance` *artifact*, not the graph database, so a failing `graph_load` leaf
does not block extraction).

Seed the `ocr` artifact from the existing cached Azure DI result for this
document (`D:\FinancialDocIntelligence\data\documents\0c0c02af-.../azure_result.json`)
through `normalize_ocr`, written under the `ocr` stage's content-addressed key
so `run_stage` treats it as a cache hit. Azure Document Intelligence calls: **0**.
The DAG then runs `structure` onward for real.

**Also seed `semantic` as `{"schema_version": 1, "sections": []}`.** This is
not only a cost decision, it is the *faithful* one:

- The 87.4% benchmark was itself produced with an empty semantic artifact —
  `build_pool_cache.py` calls `build_chunks(ast, {"sections": []}, provenance,
  structure)`. All 2752 cached chunks carry `section_type: None`.
- `section_type` is **inert for extraction**. It appears nowhere in
  `fdi.pipeline.extractor` or `fdi.core.extractor`; `extract_field` scopes
  `hybrid_search` by `doc_key`/`version` only, and `build_vector_results` skips
  its section filter when `section_type is None`.
- Running `semantic` for real costs **one Azure OpenAI call per AST section —
  510 for this document**, ~28× the 18 extraction calls, and would populate
  `section_type` where the benchmark had `None`, making the chunk set differ
  from the one the 87.4% was measured on.

Since `section_type` never changes chunk *text*, embeddings and retrieval are
identical either way; seeding merely avoids 510 pointless calls. Note this
leaves `chunk.section_type` NULL, so the `/retrieve` route's `section_type`
filter and the `knowledge` stage are not meaningfully exercised by this run —
acceptable, as neither is on the extraction path under test.

Then `POST /extract/document` and read the resulting JSON against the schema's
`example` values directly — no scoring script, per the scope decision in §2.

## 6. Success criteria

1. `extract_document` issues **one LLM call per retrieval unit** (18 for schema
   v3.1), verified by test.
2. `extract_document` and `extract_field` signatures unchanged; `routes.py`
   untouched; the full existing suite green.
3. An end-to-end run on the STTL FY2023-24 report produces a field record for
   all 198 fields, assessed by direct reading against the schema examples.
4. No reranker changes.
