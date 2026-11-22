"""Retrieval-only mode: each scorable item's question through `POST
/retrieve`, scored by Recall@K and MRR, with no /ask and no model call."""

import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))

from eval_answers import main  # noqa: E402


def _benchmark(directory: Path) -> Path:
    """Three items: q0 and q1 scoped to ACME with gold on page 7, and one
    unanswerable item with no gold evidence, which retrieval cannot score."""

    def item(n: int) -> dict[str, object]:
        return {
            "id": f"fact_lookup:acme:q{n}", "kind": "fact_lookup",
            "question": f"What were ACME's total assets? (q{n})",
            "doc_keys": ["acme-2025-26"],
            "gold": {"answerable": True,
                     "values": [{"value": 100.0, "unit": "INR crore", "tolerance": 0.5}],
                     "evidence": [{"doc_key": "acme-2025-26", "page": 7}]},
        }

    directory.mkdir()
    (directory / "fact_lookup.json").write_text(
        json.dumps({"version": "v-test", "items": [item(0), item(1)]}), encoding="utf-8"
    )
    (directory / "unanswerable.json").write_text(json.dumps({"version": "v-test", "items": [
        {"id": "unanswerable:acme:x", "kind": "unanswerable",
         "question": "What was ACME's crypto exposure?", "gold": {"answerable": False}},
    ]}), encoding="utf-8")
    return directory


def _hit(page: int) -> dict[str, object]:
    """One HybridSearchResultView."""
    return {"doc_id": "d-1", "doc_key": "acme-2025-26", "version": 1, "chunk_id": f"c-{page}",
            "heading": None, "page": page, "section_type": None, "text": "...", "score": 0.1}


def _retrieve_stack(retrieve_bodies: list[dict[str, object]]) -> httpx.MockTransport:
    """Login, then /retrieve: q0's gold page comes back at rank 7, q1's call
    fails. Anything else -- /ask above all -- fails the test."""

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/login":
            return httpx.Response(200, json={"access_token": "t"})
        if request.method == "POST" and request.url.path == "/retrieve":
            assert request.headers["Authorization"] == "Bearer t"
            body = json.loads(request.content)
            retrieve_bodies.append(body)
            if body["query"].endswith("(q1)"):
                return httpx.Response(503, json={"detail": "retrieve is not configured"})
            return httpx.Response(200, json={"results": [_hit(p) for p in
                                                         (1, 2, 3, 4, 5, 6, 7, 8, 9, 10)]})
        raise AssertionError(f"retrieval mode made a forbidden call: "
                             f"{request.method} {request.url.path}")

    return httpx.MockTransport(handle)


def test_retrieval_mode_scores_recall_and_mrr_without_any_ask_call_or_spend_gate(
    capsys: pytest.CaptureFixture[str], tmp_path: Path,
) -> None:
    retrieve_bodies: list[dict[str, object]] = []
    out_dir = tmp_path / "out"
    # Neither --limit nor --yes: retrieval bills nothing, so nothing is gated.
    code = main(
        ["run", "--mode", "retrieval", "--endpoint", "http://fdi.test",
         "--out-dir", str(out_dir), "--email", "e", "--password", "p",
         "--benchmark", str(_benchmark(tmp_path / "bench"))],
        transport=_retrieve_stack(retrieve_bodies),
    )
    assert code == 0

    # One call per scorable item, top 10, scoped to the item's Report.
    assert retrieve_bodies == [
        {"query": "What were ACME's total assets? (q0)", "k": 10,
         "filters": {"doc_key": "acme-2025-26"}},
        {"query": "What were ACME's total assets? (q1)", "k": 10,
         "filters": {"doc_key": "acme-2025-26"}},
    ]

    body = json.loads((out_dir / "report.json").read_text(encoding="utf-8"))
    assert body["stamp"]["mode"] == "retrieval"
    assert body["skipped"] == 1
    overall = body["overall"]
    # q0: gold at rank 7 -> in @10 not @5, rr 1/7; q1 failed -> 0 everywhere.
    assert (overall["run"], overall["failed"]) == (2, 1)
    assert (overall["recall_at_5"], overall["recall_at_10"]) == (0.0, 0.5)
    assert overall["mrr"] == pytest.approx(1 / 14)
    assert body["by_kind"]["fact_lookup"]["recall_at_10"] == 0.5
    q0, q1 = body["items"]
    assert (q0["first_gold_rank"], q0["recalled_at_5"], q0["recalled_at_10"]) == (7, False, True)
    assert "503" in q1["error"]

    summary = (out_dir / "report.md").read_text(encoding="utf-8")
    assert "- **mode**: `retrieval`" in summary
    assert "| fact_lookup | 2 | 1 | 0.0% | 50.0% | 0.071 |" in summary
    assert "skipped (no gold evidence)**: 1" in summary
    assert "`fact_lookup:acme:q1`" in summary.split("## Failed")[1]


def test_retrieval_mode_honours_limit_over_scorable_items(
    capsys: pytest.CaptureFixture[str], tmp_path: Path,
) -> None:
    retrieve_bodies: list[dict[str, object]] = []
    main(
        ["run", "--mode", "retrieval", "--endpoint", "http://fdi.test",
         "--out-dir", str(tmp_path / "out"), "--email", "e", "--password", "p",
         "--limit", "1", "--benchmark", str(_benchmark(tmp_path / "bench"))],
        transport=_retrieve_stack(retrieve_bodies),
    )
    assert len(retrieve_bodies) == 1
    assert "[1/1]" in capsys.readouterr().out


def test_the_default_answers_mode_report_is_stamped_with_its_mode(tmp_path: Path) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/login":
            return httpx.Response(200, json={"access_token": "t"})
        if request.url.path == "/jobs":
            return httpx.Response(200, json={"jobs": []})
        if request.url.path == "/retrieve":
            raise AssertionError("answers mode called /retrieve")
        return httpx.Response(503, json={"detail": "agent_client"})

    main(
        ["run", "--endpoint", "http://fdi.test", "--out-dir", str(tmp_path / "out"),
         "--limit", "1", "--email", "e", "--password", "p",
         "--benchmark", str(_benchmark(tmp_path / "bench"))],
        transport=httpx.MockTransport(handle),
    )
    body = json.loads((tmp_path / "out" / "report.json").read_text(encoding="utf-8"))
    assert body["stamp"]["mode"] == "answers"
    assert "- **mode**: `answers`" in (tmp_path / "out" / "report.md").read_text(
        encoding="utf-8")


def test_an_item_scoped_to_several_reports_is_retrieved_unscoped(tmp_path: Path) -> None:
    """/retrieve filters by one doc_key; a cross-company item spans two."""
    directory = tmp_path / "bench"
    directory.mkdir()
    (directory / "cross_company.json").write_text(json.dumps({"version": "v-test", "items": [
        {"id": "cross_company:x", "kind": "cross_company",
         "question": "Whose revenue was larger, ACME's or BETA's?",
         "doc_keys": ["acme-2025-26", "beta-2025-26"],
         "gold": {"answerable": True,
                  "values": [{"value": 100.0, "unit": "INR crore", "tolerance": 0.5}],
                  "evidence": [{"doc_key": "acme-2025-26", "page": 7}]}},
    ]}), encoding="utf-8")
    retrieve_bodies: list[dict[str, object]] = []
    main(
        ["run", "--mode", "retrieval", "--endpoint", "http://fdi.test",
         "--out-dir", str(tmp_path / "out"), "--email", "e", "--password", "p",
         "--benchmark", str(directory)],
        transport=_retrieve_stack(retrieve_bodies),
    )
    assert retrieve_bodies == [
        {"query": "Whose revenue was larger, ACME's or BETA's?", "k": 10},
    ]
