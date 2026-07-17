# SP-7d — Linking: figures (with captions) and footnotes

- **Status:** Draft 2026-07-17.
- **Depends on:** SP-7a (enriched OCR artifact), SP-7b (`StructureStage`, cross-page stitching), SP-7c
  (table-grid canonicalization), SP-2 (stage framework), SP-3 (harness — for the deferred cross-reference
  candidate).
- **Research:** `docs/architecture/research/2026-07-17-sp7-structural.md` §"Decompose per roadmap" — "7d
  linking (captions + footnotes + cross-refs)"; the broader recommendation to treat "figure-caption matching"
  as an explicit post-processing STAGE (Docling's normalized-model exemplar).
- **Part of:** SP-7 Structural Intelligence, phase 4's tail: "caption linking · footnote linking ·
  cross-reference detection." 7a enriched the OCR payload, 7b stitched tables across pages, 7c canonicalized
  table internals; 7d is the last structural sub-project — it SURFACES figures and footnotes as structured
  elements. SP-9 (canonical graph) is next.

## 1. Purpose

Azure Document Intelligence detects figures (charts, images) and already groups each figure's caption text
under it (`figures[].caption.content`), and tags footnote paragraphs with `role: "footnote"` — but SP-7a's
enrichment (`normalize_ocr`) never kept the `figures[]` array at all, so a figure and its caption are
currently invisible past the OCR stage, and a footnote paragraph is indistinguishable from body text once it
reaches `StructureStage` (SP-7b drops `HEADER`/`FOOTER`/`PAGE_NUMBER` roles from `blocks`, but a footnote
survives as an ordinary `body`-shaped block with no marker that it IS a footnote). Financial statements are
dense with both: chart captions ("Fig 3: Segment-wise revenue") and footnoted qualifications ("1. Restated
for FY23 comparatives.") that a downstream reader needs to find as first-class elements, not buried in prose.

Thin slice, pure-core-first: enrich `normalize_ocr` to keep `figures[]` (schema_version 3→4), add a pure
`fdi.core.links.extract_links` that pulls figures + footnotes out of the enriched OCR artifact into one
`linking` artifact, and wire a `LinkingStage` that produces it. **Full cross-reference detection — resolving
an in-text marker like "see Note 5" or "see figure 2" to its actual target — is explicitly deferred**: it
needs marker parsing (regex/NER over free text) plus a resolver that matches parsed markers against section/
figure identifiers, which is a measured (SP-3) problem in its own right, not a data-shape decision. This slice
ships the SURFACE (what figures/footnotes exist, with their text) — the resolver step is next.

## 2. Scope decisions (autonomous; grounded in research)

1. **Keep Azure's figure-caption grouping as-is; do not re-derive it.** Azure's `figures[]` already carries
   `.caption.content` — the caption text Azure itself associated with that figure via its own layout model.
   Re-deriving caption↔figure association spatially (nearest paragraph below a figure's bounding box) would
   duplicate work Azure already did and risks disagreeing with it. v1 trusts Azure's own grouping, same
   posture as 7a/7b/7c trusting Azure's `role`/`kind` fields — a spatial-reassociation candidate is a later,
   MEASURED addition (§6), not assumed better.
2. **Footnotes are surfaced by Azure `role`, not detected.** A paragraph with `role == "footnote"` (mapped to
   canonical `Role.FOOTNOTE` by SP-6's `canonical_role`, already in the `Role` enum unused until now) is a
   footnote. No heuristic (e.g. leading digit + period, small-font detection) is needed or added — Azure's own
   classification is the v1 source of truth, consistent with the whole SP-7 line.
3. **`extract_links` is a pure function, not a stage-level inline block.** Like `merge_cross_page_tables`
   (7b) and `canonicalize_table` (7c), the extraction logic is pure and exhaustively unit-testable with canned
   OCR artifacts — no Docker needed for the bulk of this slice's tests.
4. **This needs the OCR enrichment to carry `figures[]`.** SP-7a/7b/7c never had a reason to keep Azure's
   `figures[]` array (no consumer needed it). Add `figures` to `normalize_ocr` (schema_version 3→4: each Azure
   figure → `{"caption": str, "bounding_regions": [...]}`, `.caption.content` missing or the whole `caption`
   key absent → `""`) and bump `OcrStage.version` 3→4 (regenerates OCR + downstream via the version stamp —
   the SP-2 mechanism, same pattern as every prior 7-series version bump).
5. **`LinkingStage` is a derived stage** (`deps=("ocr",)`, `name="linking"`, `version=1`) producing a
   `linking` artifact `{schema_version:1, figures:[{caption}], footnotes:[{text}]}`. It sits beside
   `normalize` and `structure` (both also `deps=("ocr",)`) — an additional artifact off the same root; nothing
   downstream breaks, and no existing consumer needs re-pointing.
6. **Cross-reference detection, footnote-marker→paragraph linking, and caption→figure spatial
   re-association are explicitly OUT of scope** (§6) — each needs marker/text parsing and a matching strategy
   that is genuinely uncertain (which regex catches "see Note 5" vs. "refer para 5" vs. "(see note 5 below)"
   in real Indian annual-report prose is an empirical question), so each is a SP-3-measured candidate later,
   not an assumption baked into this slice.

## 3. Architecture

### 3.1 Figures enrichment (`fdi.core.ocr_schema.normalize_ocr`, schema_version 3→4)

Each Azure `figures[]` entry becomes:

```python
{"caption": str, "bounding_regions": [{"page_number": int, "polygon": list[float]}]}
```

extracted via `fig.get("caption", {}).get("content", "")` (defensive: missing `caption` key, or `caption`
present but with no `content`, both → `""`) and the existing `_normalize_bounding_regions` helper reused
as-is (already shared by paragraphs/tables). `OcrStage.version = 4` (comment updated: "version=4: SP-7d added
figures (caption + bounding_regions) as schema_version 4"). Additive; `normalize`/`ast`/`structure` self-
adjust via runtime-derived keys (§ Global Constraints) — only `test_ocr_stage.py` hardcodes the OCR version
literal.

### 3.2 `extract_links` (pure, `fdi.core.links`)

```python
def extract_links(ocr: Mapping[str, object]) -> dict[str, object]:
```

Reads the enriched OCR artifact's `figures` (already `{"caption", "bounding_regions"}` post-3.1) and
`paragraphs` (already `{"role", "content", ...}` since 7a). Returns:

```python
{
    "schema_version": 1,
    "figures": [{"caption": f["caption"]} for f in ocr.get("figures", [])],
    "footnotes": [{"text": p["content"]} for p in ocr.get("paragraphs", []) if p.get("role") == "footnote"],
}
```

Pure, deterministic, no I/O, no SDK import — same purity posture as `merge_cross_page_tables` /
`canonicalize_table`. Missing `figures`/`paragraphs` keys (a degenerate/older artifact) → empty lists, no
crash (defensive `.get(..., [])`, matching 7a/7b/7c's posture throughout `core`).

### 3.3 `LinkingStage` (`fdi.pipeline.stages.linking`)

```python
class LinkingStage:
    name = "linking"
    version = 1
    deps: tuple[str, ...] = ("ocr",)

    async def run(self, ctx: StageContext) -> object:
        ocr = cast(Mapping[str, object], await ctx.read("ocr"))
        return extract_links(ocr)
```

Self-registers into `REGISTRY`; imported (registration side effect) from `fdi.pipeline.stages.__init__`
alphabetically between `ast` and `normalize`.

## 4. Error handling

- A figure with no `caption` key, or `caption: {}` (no `content`) → `""`, never `None`/crash (3.1's `.get`
  chain).
- An OCR artifact with no `figures` key at all (an older, pre-7d artifact re-read without regenerating — should
  not happen in practice since a version bump forces regeneration, but defended anyway) → `extract_links`
  returns `figures: []`, not an exception.
- A paragraph with `role` missing entirely (`None`) → excluded from `footnotes` (`p.get("role") == "footnote"`
  is `False` for `None`), same as any other non-footnote role.

## 5. Testing

- **Unit** (no Docker, the bulk):
  - `normalize_ocr` (schema 4): a figure with a caption → `{"caption": <text>, "bounding_regions": [...]}`;
    a figure with no `caption` key → `{"caption": "", ...}`; `schema_version == 4`.
  - `extract_links`: an OCR artifact with 2 figures + 1 footnote paragraph (+ a title paragraph, to prove
    non-footnote roles are excluded) → the expected `figures`/`footnotes` lists exactly; an OCR artifact with
    no figures and no footnote-role paragraphs → both lists empty; missing `figures`/`paragraphs` keys →
    empty lists (defensive default), not a `KeyError`.
- **Integration** (testcontainers, fake OCR): a `FakeOcrClient` canned `AnalyzeResult` with 2 figures (one
  with a caption, one without — proving the `""` default flows end to end) and a footnote paragraph → run
  `ocr → linking` in a LOCAL `StageRegistry`; assert the `linking` artifact's `figures`/`footnotes`;
  `resolver.successors("ocr")` includes `"linking"` (membership check — no existing test asserts an exact
  `successors("ocr")` list, so no other test needs a fix, confirmed by inspection of
  `test_normalize_stage.py`/`test_structure_stage.py`, both already `in`-based).

## 6. Explicitly deferred (YAGNI → measured candidates)

- **Cross-reference detection** — matching an in-text reference ("see Note 5", "refer figure 2 above") to
  its target section/figure/table. Needs marker parsing (regex or a small NER pass over paragraph text) plus
  a resolver matching parsed markers against section headings / figure captions / footnote numbers — genuinely
  uncertain on real Indian annual-report prose, so it is a SP-3-measured candidate, not asserted now.
- **Footnote-marker → referencing-paragraph linking** — associating a footnote's number/symbol with the
  specific body paragraph(s) that reference it (as opposed to just knowing the footnote's text exists). Needs
  the same marker-parsing machinery as cross-reference detection above; deferred alongside it.
- **Caption → nearest-figure spatial association beyond Azure's own caption grouping** — a candidate that
  re-derives figure/caption pairing from bounding-box proximity instead of trusting Azure's `.caption`, for the
  case where Azure's own grouping is wrong. Only worth building once there is ground truth showing Azure's
  grouping actually fails often enough to matter (SP-3).
- Everything already deferred by 7a/7b/7c (horizontal table merge, reading-order repair, accuracy measurement
  of grid canonicalization) remains deferred; unaffected by this slice.

## 7. Done criteria

1. `normalize_ocr` (schema_version 4) preserves `figures` (`caption` + `bounding_regions`, missing caption →
   `""`); `OcrStage.version == 4`.
2. `extract_links` pulls figures (`caption`) and footnote-role paragraphs (`text`) into
   `{schema_version:1, figures, footnotes}`; proven by canned cases (figures + footnotes present; both empty;
   missing keys defensive).
3. `LinkingStage` (`deps=("ocr",)`) produces the `linking` artifact end to end (fake OCR, testcontainers); a
   rerun is a cache hit; `successors("ocr")` includes `"linking"`.
4. All four static gates green (`ruff`, `mypy --strict`, `PYTHONUTF8=1 lint-imports` 1 kept 0 broken, full
   suite); `core` stays pure (`extract_links` has no I/O/SDK import).
