"""The committed benchmark is generated, never typed, and the runner never
calls a billed endpoint without an explicit item limit."""

import json
import sys
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from fdi.core.answer_scoring import Citation, EndpointAnswer, EvalReport, score_run
from fdi.core.benchmark import Benchmark, BenchmarkItem, Evidence, Gold, GoldValue

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))

from eval_answers import (  # noqa: E402
    BENCHMARK_DIR,
    AskJobAdapter,
    dump,
    generate_fact_lookup,
    load_benchmark,
    main,
    render_markdown,
    report_json,
)

_ITEM = BenchmarkItem(
    id="q1", kind="fact_lookup", question="What was TCS's revenue?",
    gold=Gold(answerable=True, values=(GoldValue(271423.0, "INR crore", 0.5),),
              evidence=(Evidence("tcs-2025-26", 362),)),
)


def test_the_committed_fact_lookup_items_are_exactly_what_ground_truth_generates() -> None:
    """A hand edit to the benchmark, or ground truth that moved without a
    regenerate, shows up here. Fix: `eval_answers.py generate`."""
    committed = (BENCHMARK_DIR / "fact_lookup.json").read_text(encoding="utf-8")
    assert committed == dump(generate_fact_lookup())


def test_the_committed_benchmark_loads_with_one_item_per_ground_truth_value() -> None:
    gt_values = sum(
        len(json.loads(p.read_text(encoding="utf-8"))["values"])
        for p in (BENCHMARK_DIR.parents[1]).glob("*.json")
    )
    bench = load_benchmark(BENCHMARK_DIR)
    assert bench.version == "v0"
    assert len(bench.items) == gt_values
    assert all(item.kind == "fact_lookup" for item in bench.items)
    assert not any("RITES" in item.question for item in bench.items)


def test_run_refuses_to_call_ask_without_a_limit_or_confirmation(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # An unroutable endpoint: any HTTP attempt would raise, not print.
    code = main(["run", "--endpoint", "http://127.0.0.1:9", "--out-dir", "unused"])
    assert code == 0
    assert "Pass --limit N" in capsys.readouterr().out


def _ask_stack(result: Mapping[str, object]) -> httpx.MockTransport:
    """Today's /ask job flow, already finished: submit -> succeeded -> result."""

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/ask":
            return httpx.Response(202, json={"job_id": "j1"})
        if request.url.path == "/jobs/j1":
            return httpx.Response(200, json={"status": "succeeded"})
        if request.url.path == "/jobs/j1/result":
            return httpx.Response(200, json=result)
        return httpx.Response(404)

    return httpx.MockTransport(handle)


async def test_the_ask_adapter_normalises_todays_citation_shape() -> None:
    """The wire shape is AskCitationView: every key present, most of them
    null for any one tool's citation."""
    wire_nulls = {"doc_id": None, "doc_key": None, "version": None, "heading": None,
                  "page": None, "chunk_id": None, "table_index": None, "row_label": None,
                  "column_header": None, "node_id": None}
    result = {
        "answer": "Revenue was ₹2,71,423 crore.",
        "citations": [
            {**wire_nulls, "doc_id": "d-1", "doc_key": "tcs-2025-26", "version": 3,
             "heading": "Statement of Profit and Loss", "page": 362, "chunk_id": "c-9"},
            {**wire_nulls, "doc_key": "tcs-2025-26", "page": 362, "table_index": 4,
             "row_label": "Revenue from operations", "column_header": "FY2025-26"},
            {**wire_nulls, "doc_id": "d-2"},
        ],
        "tool_trace": [],
    }
    async with httpx.AsyncClient(transport=_ask_stack(result), base_url="http://t") as http:
        answer = await AskJobAdapter(http, poll_interval=0.0).answer(_ITEM)

    assert answer.error is None
    assert answer.citations == (
        Citation(doc_key="tcs-2025-26", page=362, doc_id="d-1"),
        Citation(doc_key="tcs-2025-26", page=362, table_index=4,
                 row_label="Revenue from operations"),
        Citation(doc_id="d-2"),
    )


async def test_the_ask_adapter_skips_citation_values_of_the_wrong_type() -> None:
    result = {
        "answer": "Revenue was ₹2,71,423 crore.",
        "citations": ["not a citation", {"doc_key": 7, "page": True, "table_index": "4"}],
        "tool_trace": [],
    }
    async with httpx.AsyncClient(transport=_ask_stack(result), base_url="http://t") as http:
        answer = await AskJobAdapter(http, poll_interval=0.0).answer(_ITEM)

    assert answer.citations == (Citation(),)


def _two_item_report() -> EvalReport:
    """q1 correct and cited on the gold page; q2 correct but cited off it."""
    bench = Benchmark(version="v-test", items=(_ITEM, replace(_ITEM, id="q2")))
    gold, off = Citation(doc_key="tcs-2025-26", page=362), Citation(doc_key="tcs-2025-26", page=9)
    return score_run(bench, [
        EndpointAnswer(item_id="q1", answer="₹2,71,423 crore", citations=(gold, off)),
        EndpointAnswer(item_id="q2", answer="₹2,71,423 crore", citations=(off,)),
    ])


def test_the_json_report_carries_citation_and_completion_per_item_kind_and_overall() -> None:
    # Through JSON, as a reader of report.json sees it.
    body = json.loads(dump(report_json(_two_item_report(), {}, {})))
    q1, q2 = body["items"]
    assert (q1["citations"], q1["valid_citations"], q1["citation_validity"]) == (2, 1, 0.5)
    assert (q1["supported"], q1["completed"]) == (True, True)
    assert (q2["status"], q2["supported"], q2["completed"]) == ("correct", False, False)
    for summary in (body["overall"], body["by_kind"]["fact_lookup"]):
        assert (summary["completed"], summary["supported"]) == (1, 1)
        assert summary["completion_rate"] == 0.5
        assert summary["evidence_support_rate"] == 0.5
        assert summary["citation_validity"] == 1 / 3


def test_the_markdown_summary_has_a_completion_table_and_lists_unsupported_answers() -> None:
    summary = render_markdown(_two_item_report(), {})
    assert "| fact_lookup | 2 | 1 | 50.0% | 1 | 50.0% | 3 | 1 | 33.3% |" in summary
    assert "| **overall** | 2 | 1 | 50.0% | 1 | 50.0% | 3 | 1 | 33.3% |" in summary
    unsupported = summary.split("## Correct but not completed")[1]
    assert "`q2`" in unsupported
    assert "`q1`" not in unsupported


async def test_an_ask_result_with_no_citations_key_has_no_citations() -> None:
    result = {"answer": "Revenue was ₹2,71,423 crore."}
    async with httpx.AsyncClient(transport=_ask_stack(result), base_url="http://t") as http:
        answer = await AskJobAdapter(http, poll_interval=0.0).answer(_ITEM)

    assert answer.answer == "Revenue was ₹2,71,423 crore."
    assert answer.citations == ()
