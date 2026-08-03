"""Basis segmentation: label which pages of an Indian annual report are
Standalone (parent-only) vs Consolidated (group) financial statements.

Indian companies with subsidiaries file TWO near-duplicate statement sets --
Standalone and Consolidated -- with different numbers for the same line
items. `detect_basis_regions` walks the `ast` artifact's section headings
(in document order), anchoring on the regulator-mandated, company-invariant
SA-700 / Schedule III headings (the independent auditor's report captions
and the primary-statement captions), and resolves each anchor's page via the
`provenance` artifact's `node_provenance` (section i's node id is `f"s{i}"`).
Each anchor OPENS a region of its basis that runs until the next anchor of a
DIFFERENT basis (exclusive) or the document's last page.

Pure, no I/O; total -- never raises, whatever shape `ast`/`provenance` are.
This module generalizes the old `find_consolidated_start_page` single-cutoff
filter (`fdi.core.extractor`) into a labeled-region model: a table/section in
`unknown` (front/back matter, or a document with no anchors at all) is
surfaced to both bases rather than silently dropped.
"""

import re
from collections.abc import Mapping

# Anchors are deliberately SPECIFIC (full caption text, not a bare
# "consolidated"/"standalone" substring) -- a bare word also occurs inside
# narrative (accounting policies, related-party notes) in either section, and
# matching it would misclassify prose as a region boundary.
_CONSOLIDATED_ANCHORS = (
    "independent auditor's report on the consolidated financial statements",
    "independent auditors' report on the consolidated financial statements",
    "consolidated balance sheet",
    "consolidated statement of profit and loss",
    "consolidated statement of cash flow",
    "consolidated cash flow",
)

# The Standalone counterparts are the same captions with "standalone" in
# place of "consolidated" -- which already includes "independent auditor's
# report on the standalone financial statements" (both apostrophe variants).
_STANDALONE_ANCHORS = tuple(a.replace("consolidated", "standalone") for a in _CONSOLIDATED_ANCHORS)

_WHITESPACE_RE = re.compile(r"\s+")


def _normalize(heading: str) -> str:
    """Lowercase, collapse-whitespace normalization for anchor matching."""
    return _WHITESPACE_RE.sub(" ", heading.strip().lower())


def _classify_heading(heading: str) -> str | None:
    """`"consolidated"` / `"standalone"` if the normalized heading contains a
    matching anchor phrase, else `None`."""
    normalized = _normalize(heading)
    if any(anchor in normalized for anchor in _CONSOLIDATED_ANCHORS):
        return "consolidated"
    if any(anchor in normalized for anchor in _STANDALONE_ANCHORS):
        return "standalone"
    return None


def _node_pages(provenance: Mapping[str, object]) -> dict[str, int]:
    """`node_id -> page` for every usable `node_provenance` entry. Never
    raises: a malformed `provenance`/entries list/entry degrades that entry
    (or the whole map) to absent, never a crash."""
    out: dict[str, int] = {}
    entries = provenance.get("node_provenance")
    if not isinstance(entries, list):
        return out
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        node_id = entry.get("node_id")
        page = entry.get("page")
        if isinstance(node_id, str) and isinstance(page, int) and not isinstance(page, bool):
            out[node_id] = page
    return out


def _all_pages(provenance: Mapping[str, object]) -> list[int]:
    """Every resolvable page across `node_provenance`, in artifact order --
    used to find the document's min/max page for whole-document regions."""
    entries = provenance.get("node_provenance")
    if not isinstance(entries, list):
        return []
    pages: list[int] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        page = entry.get("page")
        if isinstance(page, int) and not isinstance(page, bool):
            pages.append(page)
    return pages


_DEFAULT_ARTIFACT: dict[str, object] = {
    "schema_version": 1,
    "regions": [],
    "default_basis": "unknown",
}


def detect_basis_regions(
    ast: Mapping[str, object], provenance: Mapping[str, object]
) -> dict[str, object]:
    """Segment a document into standalone/consolidated page regions.

    Walks `ast["sections"]` in order, classifying each section's heading
    against the SA-700/Schedule III anchor phrases and resolving its page via
    `provenance["node_provenance"]` (node id `f"s{i}"`). An anchor with no
    resolvable page is skipped (merged away), never blocking. Each surviving
    anchor opens a region of its basis running to the next anchor of a
    DIFFERENT basis (exclusive) or the document's last page.

    A document with anchors of only one basis collapses to a single region
    of that basis spanning the document's min/max resolvable page (not just
    the anchor's own page) -- covering front matter too, since e.g. a
    standalone-only filing has no consolidated section to bound. A document
    with no anchors at all returns `regions: []` (see module docstring).

    Total: never raises. Malformed `ast`/`provenance` (wrong types, missing
    keys, non-list sections/entries) degrade to `{"regions": [],
    "default_basis": "unknown"}`.
    """
    if not isinstance(ast, Mapping) or not isinstance(provenance, Mapping):
        return dict(_DEFAULT_ARTIFACT)

    sections_raw = ast.get("sections")
    if not isinstance(sections_raw, list):
        return dict(_DEFAULT_ARTIFACT)

    node_pages = _node_pages(provenance)

    anchors: list[tuple[int, str, int]] = []  # (page, basis, section_index)
    for i, section in enumerate(sections_raw):
        if not isinstance(section, Mapping):
            continue
        heading = section.get("heading")
        if not isinstance(heading, str):
            continue
        basis = _classify_heading(heading)
        if basis is None:
            continue
        page = node_pages.get(f"s{i}")
        if page is None:
            continue
        anchors.append((page, basis, i))

    if not anchors:
        return dict(_DEFAULT_ARTIFACT)

    anchors.sort(key=lambda a: (a[0], a[2]))

    all_pages = _all_pages(provenance)
    doc_min_page = min(all_pages) if all_pages else anchors[0][0]
    doc_max_page = max(all_pages) if all_pages else anchors[-1][0]

    has_consolidated = any(basis == "consolidated" for _, basis, _ in anchors)
    has_standalone = any(basis == "standalone" for _, basis, _ in anchors)

    if not (has_consolidated and has_standalone):
        # Only one basis appears at all -- the whole document is one region
        # of that basis, spanning its full min/max page (not just the
        # anchor's own page), so front/back matter is covered too.
        only_basis = anchors[0][1]
        return {
            "schema_version": 1,
            "regions": [
                {"start_page": doc_min_page, "end_page": doc_max_page, "basis": only_basis}
            ],
            "default_basis": "unknown",
        }

    # Both bases anchor somewhere: collapse consecutive same-basis anchors
    # (a run of anchors for the same basis opens only one region), then each
    # remaining anchor's region runs to the next anchor's page - 1, or the
    # document's last page for the final region.
    collapsed: list[tuple[int, str]] = []
    for page, basis, _ in anchors:
        if collapsed and collapsed[-1][1] == basis:
            continue
        collapsed.append((page, basis))

    regions: list[dict[str, object]] = []
    for idx, (page, basis) in enumerate(collapsed):
        start = page
        end = collapsed[idx + 1][0] - 1 if idx + 1 < len(collapsed) else doc_max_page
        if end < start:
            # Degenerate case (e.g. two different-basis anchors resolve to
            # the same page): never emit an inverted region.
            end = start
        regions.append({"start_page": start, "end_page": end, "basis": basis})

    return {"schema_version": 1, "regions": regions, "default_basis": "unknown"}


def page_basis(basis_artifact: Mapping[str, object], page: int | None) -> str:
    """The basis of the region containing `page`, else the artifact's
    `default_basis` (itself defaulted to `"unknown"` if missing/malformed).
    `page=None` -> `"unknown"`. Total: never raises on any input shape."""
    if page is None:
        return "unknown"
    if not isinstance(basis_artifact, Mapping):
        return "unknown"

    default_raw = basis_artifact.get("default_basis")
    default = default_raw if isinstance(default_raw, str) else "unknown"

    regions = basis_artifact.get("regions")
    if not isinstance(regions, list):
        return default

    for region in regions:
        if not isinstance(region, Mapping):
            continue
        start = region.get("start_page")
        end = region.get("end_page")
        basis = region.get("basis")
        if not isinstance(start, int) or isinstance(start, bool):
            continue
        if not isinstance(end, int) or isinstance(end, bool):
            continue
        if not isinstance(basis, str):
            continue
        if start <= page <= end:
            return basis

    return default


def basis_regions(basis_artifact: Mapping[str, object]) -> list[tuple[str, int, int]]:
    """Well-formed `(basis, start_page, end_page)` regions, in document order.
    Total: a malformed artifact yields []."""
    out: list[tuple[str, int, int]] = []
    regions = basis_artifact.get("regions") if isinstance(basis_artifact, Mapping) else None
    if not isinstance(regions, list):
        return out
    for region in regions:
        if not isinstance(region, Mapping):
            continue
        start = region.get("start_page")
        end = region.get("end_page")
        basis = region.get("basis")
        if not isinstance(start, int) or isinstance(start, bool):
            continue
        if not isinstance(end, int) or isinstance(end, bool):
            continue
        if not isinstance(basis, str) or not basis or end < start:
            continue
        out.append((basis, start, end))
    return out


def resolve_effective_basis(
    basis_artifact: Mapping[str, object], preference: str
) -> str:
    """The ONE basis a whole document should be read against.

    Basis must be decided per DOCUMENT, not per retrieval unit. The per-unit
    fallback ("if filtering to the preference would empty this pool, keep it
    unfiltered") is correct in isolation and wrong in aggregate: on a
    consolidated-only filing it lets a unit whose pool happens to contain a
    standalone page filter to standalone while its neighbour falls back to
    consolidated. Measured on RIL FY2025-26, that produced total non-current
    assets from page 58 and total current liabilities from page 100 -- two
    different statement sets -- and a balance sheet out by 998,799.

    Resolution: honour `preference` when the document actually HAS that basis;
    otherwise adopt the basis it does have (the largest region when several
    compete). Returns `preference` unchanged when there is nothing to go on, so
    a document with no basis artifact behaves exactly as before.
    """
    regions = basis_regions(basis_artifact)
    if not regions or preference == "both":
        return preference
    if any(basis == preference for basis, _, _ in regions):
        return preference
    widest = max(regions, key=lambda r: (r[2] - r[1], -r[1]))
    return widest[0]


def basis_is_ambiguous(basis_artifact: Mapping[str, object]) -> bool:
    """True when the document carries MORE THAN ONE basis, i.e. when falling
    back to an unfiltered pool could silently mix statement sets. A document
    with a single basis (or none) cannot mix, so the fallback stays safe there.
    """
    return len({basis for basis, _, _ in basis_regions(basis_artifact)}) > 1
