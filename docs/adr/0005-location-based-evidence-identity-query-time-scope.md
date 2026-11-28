---
status: accepted
---

# Evidence identity is location-based; Company and Fiscal-year scope resolve at query time

Every retrieval path (structured facts, hybrid search, structural lookups and cross-document fan-out) returns one shape, **Evidence**. An Evidence's id names **where it is in one Report version**, never how it was found or how well it ranked. So the same passage found by the dense channel and by BM25 is one Evidence, and an id recorded in an answer can be inspected again later to check the claim against its full context. Company and Fiscal year are **not** stored in the vector index. They resolve to Document version ids in Postgres when the query runs, exactly as `doc_key` already did. This decides the design's ADR-007 and ADR-010 as amended by S4 (#33).

## Decisions

- **One id grammar, built and parsed in one function pair** (`fdi.core.evidence.evidence_id` / `parse_evidence_id`):

  | kind | id |
  |---|---|
  | passage | `{doc_id}:s{i}` (the existing chunk id, unchanged) |
  | table | `{doc_id}:t{i}` (the existing table-card id, unchanged) |
  | table_cell | `{doc_id}:t{i}:r{row}:c{col}` (raw grid coordinates, header rows included) |
  | page | `{doc_id}:p{n}` |
  | note | `{doc_id}:note:{basis}:{ref}` (the FOG's `nt:{basis}:{ref}`) |
  | line_item | `{doc_id}:li:{st\|nt}:{basis}:{name}:r{row}` (the FOG line-item id after the doc id) |
  | fact | `{doc_id}:fact:{basis}:{tag}` (it links to the line item it came from) |

  `doc_id` is the Document **version** id, so every id names exactly one Report version. `line_item` is a kind the spec named only as an id form. It is a kind of its own because a statement line is neither a table nor a cell: it has a label, two periods and outgoing REFERENCES.
- **Score and retrieval path describe how Evidence was found, not what it is.** When paths overlap, the copies merge into one Evidence that records every path that found it.
- **The vector index gains two filterable scalars: `basis` and `kind`** (vector schema v3). A basis-, kind- or section-scoped search is filtered inside Milvus. Nothing is over-fetched and then filtered in memory any more, which is what the old 5× section over-fetch did.
- **Company, Fiscal year and Workspace are not in the index.** A Report's Company and Fiscal year are correctable (S2.3). If they were copied into the index, every correction would leave stale rows behind until a reindex. They resolve through the S2 registry to Document version ids when the query runs.
- **Scope is enforced by the service, never by its caller's arguments.** Requests are validated against closed vocabularies and id patterns before any I/O. Filter expressions are built only from resolved ids and enum literals (`scope_filter_expression`), and that function re-checks every literal and raises rather than emit an unsafe one.
- **The default scope is each Report's latest version.** An explicit `doc_key` plus `version`, or `doc_ids`, reaches an earlier version. The pre-S4 routes keep their own "every version unless filtered" semantics through an explicit `all_versions` flag.
- **Rollout is a new collection, not an alter.** Milvus cannot add fields to a live collection. The new collection is `fdi_chunks_v3`. Index stage v4 writes the new fields, and `fdi-retrieval reindex` re-dispatches it for every Report. Embeddings stay cached, so nothing is re-embedded and nothing calls Azure. A store pointed at a pre-v3 collection refuses to read or write it, and its message names the fix.

## Considered Options

- **Location-based ids** (chosen).
- **Hash of content**: stable across identical text, but two identical boilerplate passages on different pages would collapse into one, and any re-OCR would change every id.
- **Opaque per-retrieval ids**: simple, but the same passage found twice becomes two things, and nothing can be inspected later.
- **Company and Fiscal year as index fields**: one fewer Postgres round trip, at the cost of an index that is wrong after every Company or Fiscal-year correction until someone reindexes.

## Consequences

- An Evidence id stays valid as long as the Report version's artifacts do. A stage version bump that renumbers sections or tables (a new `chunks` or `structure` body) moves passage and table ids for re-processed Reports. That is the same contract the chunk ids already had.
- A Company scope over many Reports becomes a `doc_id in [...]` filter. That is fine at the corpus's size (a few Reports per Company). Partitioning is the scale path if it ever is not.
- A basis-scoped search is exact. Text outside every basis region (front matter, the directors' report) has basis `unknown`, and a caller who wants it asks for `unknown` explicitly. The extractor asks for its preferred basis plus `unknown`. If that pool is empty, it retries unfiltered, as its own explicit decision.
