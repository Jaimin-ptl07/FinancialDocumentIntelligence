from typing import Any

from fdi.core.basis import detect_basis_regions, page_basis


def _provenance(entries: list[tuple[str, int]]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "node_provenance": [{"node_id": node_id, "page": page} for node_id, page in entries],
    }


def test_standalone_then_consolidated_two_regions() -> None:
    ast: dict[str, object] = {
        "title": "ACME LTD",
        "sections": [
            {"heading": None, "paragraphs": []},
            {"heading": "Independent Auditor's Report on the Standalone Financial Statements"},
            {"heading": "Standalone Balance Sheet"},
            {"heading": "Independent Auditor's Report on the Consolidated Financial Statements"},
            {"heading": "Consolidated Balance Sheet"},
        ],
    }
    provenance = _provenance(
        [
            ("s1", 10),
            ("s2", 12),
            ("s3", 140),
            ("s4", 142),
            ("t7", 200),  # sets the document's max page beyond the last anchor
        ]
    )

    out = detect_basis_regions(ast, provenance)

    assert out == {
        "schema_version": 1,
        "regions": [
            {"start_page": 10, "end_page": 139, "basis": "standalone"},
            {"start_page": 140, "end_page": 200, "basis": "consolidated"},
        ],
        "default_basis": "unknown",
    }


def test_standalone_only_doc_is_one_region() -> None:
    ast: dict[str, object] = {
        "sections": [
            {"heading": "Independent Auditor's Report on the Standalone Financial Statements"},
            {"heading": "Notes to Accounts"},
        ],
    }
    provenance = _provenance([("s0", 5), ("s1", 50)])

    out = detect_basis_regions(ast, provenance)

    assert out == {
        "schema_version": 1,
        "regions": [{"start_page": 5, "end_page": 50, "basis": "standalone"}],
        "default_basis": "unknown",
    }


def test_consolidated_only_doc_is_one_region() -> None:
    ast: dict[str, object] = {
        "sections": [
            {"heading": "Independent Auditor's Report on the Consolidated Financial Statements"},
        ],
    }
    provenance = _provenance([("s0", 8), ("t1", 30)])

    out = detect_basis_regions(ast, provenance)

    assert out == {
        "schema_version": 1,
        "regions": [{"start_page": 8, "end_page": 30, "basis": "consolidated"}],
        "default_basis": "unknown",
    }


def test_no_anchors_is_empty_regions_and_unknown_default() -> None:
    ast: dict[str, object] = {
        "sections": [
            {"heading": "Directors' Report"},
            {"heading": "Corporate Governance"},
        ],
    }
    provenance = _provenance([("s0", 1), ("s1", 2)])

    out = detect_basis_regions(ast, provenance)

    assert out == {"schema_version": 1, "regions": [], "default_basis": "unknown"}


def test_anchor_with_no_resolvable_page_is_skipped() -> None:
    """A heading anchor whose section has no node_provenance entry (no
    resolvable page) is merged/skipped, not a crash and not a phantom
    region."""
    ast: dict[str, object] = {
        "sections": [
            {"heading": "Independent Auditor's Report on the Standalone Financial Statements"},
        ],
    }
    provenance = _provenance([])  # no entries at all -> s0's page is unresolved

    out = detect_basis_regions(ast, provenance)

    assert out == {"schema_version": 1, "regions": [], "default_basis": "unknown"}


def test_malformed_ast_never_raises() -> None:
    assert detect_basis_regions({}, {}) == {
        "schema_version": 1,
        "regions": [],
        "default_basis": "unknown",
    }
    bad_ast: Any = "not-a-mapping"
    bad_provenance: Any = 12345
    assert detect_basis_regions(bad_ast, bad_provenance) == {
        "schema_version": 1,
        "regions": [],
        "default_basis": "unknown",
    }
    assert detect_basis_regions({"sections": "not-a-list"}, {}) == {
        "schema_version": 1,
        "regions": [],
        "default_basis": "unknown",
    }
    assert detect_basis_regions(
        {"sections": [None, {"heading": 123}, "garbage"]}, {"node_provenance": "nope"}
    ) == {"schema_version": 1, "regions": [], "default_basis": "unknown"}


def test_page_basis_hit_and_miss() -> None:
    artifact = {
        "schema_version": 1,
        "regions": [
            {"start_page": 10, "end_page": 139, "basis": "standalone"},
            {"start_page": 140, "end_page": 200, "basis": "consolidated"},
        ],
        "default_basis": "unknown",
    }

    assert page_basis(artifact, 50) == "standalone"
    assert page_basis(artifact, 150) == "consolidated"
    assert page_basis(artifact, 140) == "consolidated"  # boundary is inclusive
    assert page_basis(artifact, 139) == "standalone"  # boundary is inclusive
    assert page_basis(artifact, 5) == "unknown"  # before any region -> default
    assert page_basis(artifact, 9999) == "unknown"  # after every region -> default


def test_page_basis_none_page_is_unknown() -> None:
    artifact = {
        "regions": [{"start_page": 1, "end_page": 10, "basis": "standalone"}],
        "default_basis": "unknown",
    }
    assert page_basis(artifact, None) == "unknown"


def test_page_basis_never_raises_on_malformed_input() -> None:
    bad_artifact: Any = "not-a-mapping"
    assert page_basis(bad_artifact, 5) == "unknown"
    assert page_basis({}, 5) == "unknown"
    assert page_basis({"regions": "nope", "default_basis": "unknown"}, 5) == "unknown"
    assert (
        page_basis({"regions": [None, {"start_page": "x", "end_page": 5, "basis": 1}]}, 3)
        == "unknown"
    )
