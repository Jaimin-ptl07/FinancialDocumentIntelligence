"""fdi.core.evidence: the Evidence id grammar, Evidence assembly, Fiscal-year
grouping, Report status, and the vector-store filter builder (S4)."""

import pytest

from fdi.core.evidence import (
    Evidence,
    EvidenceKind,
    EvidenceRef,
    ReportMeta,
    ReportOutcome,
    ReportStatus,
    cell_ref,
    evidence_body,
    evidence_from_hit,
    evidence_id,
    fact_ref,
    group_by_fiscal_year,
    legacy_hit,
    merge_evidence,
    page_ref,
    parse_evidence_id,
    passage_ref,
    ref_for_chunk,
    ref_for_fog_node,
    report_status,
    scope_filter_expression,
    table_ref,
)

DOC = "0b6f1a52-9b3c-4f3e-8a55-2d6c1f1e7a10"


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        (passage_ref(DOC, 12), f"{DOC}:s12"),
        (table_ref(DOC, 3), f"{DOC}:t3"),
        (cell_ref(DOC, 3, 4, 2), f"{DOC}:t3:r4:c2"),
        (page_ref(DOC, 101), f"{DOC}:p101"),
        (EvidenceRef(DOC, EvidenceKind.NOTE, basis="consolidated", name="13(a)"),
         f"{DOC}:note:consolidated:13(a)"),
        (EvidenceRef(DOC, EvidenceKind.LINE_ITEM, parent="st", basis="standalone",
                     name="profit_and_loss", row=7),
         f"{DOC}:li:st:standalone:profit_and_loss:r7"),
        (EvidenceRef(DOC, EvidenceKind.LINE_ITEM, parent="nt", basis="consolidated",
                     name="2.1", row=0),
         f"{DOC}:li:nt:consolidated:2.1:r0"),
        (fact_ref(DOC, "consolidated", "AUTOPL001"), f"{DOC}:fact:consolidated:AUTOPL001"),
    ],
)
def test_every_kind_round_trips(ref: EvidenceRef, expected: str) -> None:
    assert evidence_id(ref) == expected
    assert parse_evidence_id(expected) == ref
    assert ref.id == expected


@pytest.mark.parametrize(
    "bad",
    [
        None, 12, "", "no-colon", f"{DOC}:", f"{DOC}:x1", f"{DOC}:s", f"{DOC}:s01",
        f"{DOC}:s-1", f"{DOC}:t3:r4", f"{DOC}:t3:c2:r4", f"{DOC}:note:consolidated",
        f"{DOC}:note:Consolidated:1", f"{DOC}:li:xx:consolidated:pl:r1",
        f"{DOC}:fact:consolidated:bad tag", f"{DOC}:fact:consolidated:a\"b",
        'bad"doc:s1', "doc id:s1", f"{DOC}:s1:extra", "a" * 600,
    ],
)
def test_malformed_ids_are_rejected(bad: object) -> None:
    assert parse_evidence_id(bad) is None


def test_fog_and_chunk_ids_bridge_to_evidence_ids() -> None:
    assert ref_for_chunk(DOC, "s4") == passage_ref(DOC, 4)
    assert ref_for_chunk(DOC, "t9") == table_ref(DOC, 9)
    assert ref_for_chunk(DOC, None) is None
    note = ref_for_fog_node(DOC, "nt:consolidated:22")
    assert note is not None and note.id == f"{DOC}:note:consolidated:22"
    assert note.fog_id == "nt:consolidated:22"
    item = ref_for_fog_node(DOC, "li:st:consolidated:balance_sheet:r3")
    assert item is not None and item.fog_id == "li:st:consolidated:balance_sheet:r3"
    assert ref_for_fog_node(DOC, "st:consolidated:balance_sheet") is None


REPORT = ReportMeta(DOC, doc_key="tcs-ar", version=2, company_id="c1", fiscal_year="FY2025-26")


def _hit(chunk_id: str, score: float = 0.5, **extra: object) -> dict[str, object]:
    return {"doc_id": DOC, "chunk_id": chunk_id, "heading": "H", "page": 4,
            "section_type": "NOTES", "text": f"text {chunk_id}", "score": score,
            "basis": "consolidated", "kind": "passage", **extra}


def test_evidence_from_hit_carries_location_and_report() -> None:
    e = evidence_from_hit(_hit("t2"), REPORT, path="hybrid")
    assert e is not None
    assert e.evidence_id == f"{DOC}:t2"
    assert e.kind is EvidenceKind.TABLE and e.table_index == 2
    assert e.report.fiscal_year == "FY2025-26"
    assert e.basis == "consolidated" and e.retrieval_paths == ("hybrid",)
    assert evidence_from_hit(_hit("t2r1"), REPORT, path="hybrid") is None


def test_same_location_found_twice_is_one_evidence() -> None:
    a = evidence_from_hit(_hit("s1", score=0.2), REPORT, path="hybrid")
    b = evidence_from_hit(_hit("s1", score=0.9), REPORT, path="structural")
    c = evidence_from_hit(_hit("s2"), REPORT, path="hybrid")
    assert a and b and c
    merged = merge_evidence([a, c], [b])
    assert [e.evidence_id for e in merged] == [f"{DOC}:s1", f"{DOC}:s2"]
    assert merged[0].retrieval_paths == ("hybrid", "structural")
    assert merged[0].score == 0.9


def test_legacy_hit_keeps_the_pre_s4_shape() -> None:
    e = evidence_from_hit(_hit("s3"), REPORT, path="hybrid")
    assert e is not None
    hit = legacy_hit(e)
    for key in ("doc_id", "doc_key", "version", "chunk_id", "heading", "page",
                "section_type", "text", "score"):
        assert key in hit
    assert hit["chunk_id"] == "s3" and hit["evidence_id"] == f"{DOC}:s3"
    assert evidence_body(e)["retrieval_path"] == "hybrid"


def test_report_status_distinguishes_not_processed_from_no_match() -> None:
    assert report_status(processed=False, evidence_count=0) is ReportStatus.NOT_PROCESSED
    assert report_status(processed=True, evidence_count=0) is ReportStatus.NO_MATCH
    assert report_status(processed=True, evidence_count=2) is ReportStatus.MATCHED


def test_group_by_fiscal_year_keeps_registry_order_and_empty_years() -> None:
    r26 = REPORT
    r25 = ReportMeta("d25", fiscal_year="FY2024-25")
    undated = ReportMeta("dx")
    e26 = Evidence(f"{DOC}:s1", EvidenceKind.PASSAGE, r26, "x")
    outcomes = [
        ReportOutcome(r26, ReportStatus.MATCHED, 1),
        ReportOutcome(undated, ReportStatus.NOT_PROCESSED),
        ReportOutcome(r25, ReportStatus.NO_MATCH),
    ]
    groups = group_by_fiscal_year(outcomes, [e26])
    assert [g.fiscal_year for g in groups] == ["FY2025-26", "FY2024-25", None]
    assert groups[0].evidence == (e26,)
    assert groups[1].evidence == () and groups[1].reports[0].status is ReportStatus.NO_MATCH


def test_scope_filter_expression_is_built_from_safe_literals_only() -> None:
    assert scope_filter_expression() == ""
    expr = scope_filter_expression(
        doc_ids=[DOC, DOC], bases=["unknown", "consolidated"], kinds=["table"],
        section_types=["NOTES"],
    )
    assert expr == (
        f'doc_id in ["{DOC}"] and basis in ["consolidated", "unknown"] '
        'and kind in ["table"] and section_type in ["NOTES"]'
    )
    for evil in ['x" or doc_id != "', "a,b", "x y", ""]:
        with pytest.raises(ValueError, match="unsafe"):
            scope_filter_expression(doc_ids=[evil])
