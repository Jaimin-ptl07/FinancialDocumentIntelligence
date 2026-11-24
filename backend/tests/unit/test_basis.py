from typing import Any

from fdi.core.basis import bases_present, detect_basis_regions, page_basis


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


def test_bases_present_names_each_statement_basis_once_standalone_first() -> None:
    both = {
        "regions": [
            {"start_page": 120, "end_page": 200, "basis": "consolidated"},
            {"start_page": 60, "end_page": 119, "basis": "standalone"},
            {"start_page": 201, "end_page": 260, "basis": "consolidated"},
        ],
        "default_basis": "unknown",
    }
    consolidated_only = {
        "regions": [{"start_page": 1, "end_page": 300, "basis": "consolidated"}],
    }

    assert bases_present(both) == ["standalone", "consolidated"]
    assert bases_present(consolidated_only) == ["consolidated"]
    assert bases_present({"regions": [], "default_basis": "unknown"}) == []
    bad: Any = None
    assert bases_present(bad) == []


# --- Anchor recall on the corpus's real headings (#57) ------------------------


def _doc(headings: list[tuple[int, str]], last_page: int) -> dict[str, object]:
    """The basis artifact for a Report whose section i is `headings[i]`
    (page, heading text), with `last_page` its final page."""
    ast = {"sections": [{"heading": text} for _, text in headings]}
    provenance = _provenance(
        [(f"s{i}", page) for i, (page, _) in enumerate(headings)] + [("t0", last_page)]
    )
    return detect_basis_regions(ast, provenance)


def _regions(artifact: dict[str, object]) -> list[tuple[str, int, int]]:
    regions: list[dict[str, Any]] = artifact["regions"]  # type: ignore[assignment]
    return [(r["basis"], r["start_page"], r["end_page"]) for r in regions]


# Narrative and contents headings that name a basis without opening it.
_NOT_ANCHORS = [
    (3, "Value Added Statement (Consolidated)"),
    (5, "10-Year Financial Highlights (Consolidated)"),
    (5, "Standalone Performance"),
    (42, "Consolidated Financial Statement"),
]


def test_ril_whose_standalone_statements_do_not_say_standalone_has_both_regions() -> None:
    # RIL FY2025-26: the standalone statements are captioned plain "Balance
    # Sheet"; only the auditor's report and the notes name the basis.
    artifact = _doc(
        [
            *_NOT_ANCHORS,
            (52, "Independent Auditor's Report"),
            (53, "Auditor's Responsibility for the Audit of the Standalone Financial Statements"),
            (58, "Balance Sheet As at 31st March, 2026"),
            (59, "Statement of Profit and Loss For the year ended 31st March, 2026"),
            (62, "Notes To the Standalone Financial Statements for the year ended "
                 "31st March, 2026"),
            (93, "Standalone Financial Statements"),
            (94, "To The Members of Reliance Industries Limited Report on the Audit of the "
                 "Consolidated Financial Statements"),
            (100, "Consolidated Balance Sheet As at 31st March, 2026"),
            (104, "Notes To the Consolidated Financial Statements for the year ended "
                  "31st March, 2026"),
        ],
        last_page=147,
    )

    assert _regions(artifact) == [("standalone", 53, 93), ("consolidated", 94, 147)]
    assert bases_present(artifact) == ["standalone", "consolidated"]


def test_tcs_regions_open_at_each_auditors_report() -> None:
    artifact = _doc(
        [
            (3, "Standalone Financial Statements"),
            (3, "Consolidated Financial Statements"),
            (157, "Report on the Audit of the Consolidated Financial Statements Opinion"),
            (163, "Annexure A to the Independent Auditor's Report on the Consolidated Financial "
                  "Statements of Tata Consultancy Services Limited"),
            (166, "Consolidated Balance Sheet"),
            (166, "NOTES FORMING PART OF CONSOLIDATED FINANCIAL STATEMENTS"),
            (239, "Report on the Audit of the Standalone Financial Statements Opinion"),
            (252, "Standalone Balance Sheet"),
            (258, "Notes forming part of Standalone Financial Statements"),
        ],
        last_page=360,
    )

    assert _regions(artifact) == [("consolidated", 157, 238), ("standalone", 239, 360)]


def test_silver_touch_statements_of_assets_and_liabilities_anchor_their_basis() -> None:
    artifact = _doc(
        [
            (83, "Information other than the Standalone Financial statements and Auditor's "
                 "report thereon."),
            (91, "AUDITED STATEMENT OF STANDALONE ASSETS AND LIABILITIES AS ON 31st March 2024"),
            (93, "SILVER TOUCH TECHNOLOGIES LIMITED CIN-L72200GJ1995PLC024465 AUDITED "
                 "STANDALONE CASH FLOW STATEMENT FOR THE YEAR"),
            (131, "Notes forming part of the standalone financial statements"),
            (134, "Auditor's Responsibilities for the Audit of the Consolidated "
                  "Financial Statements"),
            (142, "SILVER TOUCH TECHNOLOGIES LIMITED CIN-L72200GJ1995PLC024465 AUDITED "
                  "CONSOLIDATED STATEMENT OF ASSETS AND LIABILITIES"),
            (143, "CONSOLIDATED AUDITED STATEMENT OF PROFIT & LOSS A/C FOR THE YEAR ENDED ON "
                  "31st March 2024"),
            (144, "AUDITED CONSOLIDATED CASH FLOW STATEMENT FOR THE YEAR"),
        ],
        last_page=185,
    )

    assert _regions(artifact) == [("standalone", 91, 133), ("consolidated", 134, 185)]


def test_remc_whose_report_is_on_standalone_ind_as_statements_is_standalone_only() -> None:
    artifact = _doc(
        [
            (49, "Report on the Audit of the Standalone IND-AS Financial Statements"),
            (60, "Balance Sheet As at 31st March, 2025"),
        ],
        last_page=101,
    )

    assert _regions(artifact) == [("standalone", 49, 101)]


def test_narrative_and_contents_headings_open_no_region() -> None:
    assert _regions(_doc(_NOT_ANCHORS, last_page=50)) == []
