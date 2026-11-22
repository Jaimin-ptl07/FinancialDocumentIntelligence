"""The committed benchmark is generated, never typed, and the runner never
calls a billed endpoint without an explicit item limit."""

import json
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

from fdi.core.answer_scoring import Citation, EndpointAnswer, EvalReport, score_run
from fdi.core.benchmark import (
    ISSUER_DOC_KEYS,
    Benchmark,
    BenchmarkItem,
    Evidence,
    Gold,
    GoldValue,
)
from fdi.core.eval_cost import ItemCost, ItemUsage, summarise_cost
from fdi.core.judge import parse_judgement

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))

from eval_answers import (  # noqa: E402
    BENCHMARK_DIR,
    AskJobAdapter,
    JudgedItem,
    MeasuredAnswer,
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


def _groundtruth() -> list[dict[str, object]]:
    return [json.loads(p.read_text(encoding="utf-8"))
            for p in (BENCHMARK_DIR.parents[1]).glob("*.json")]


def test_the_committed_benchmark_loads_with_one_item_per_ground_truth_value() -> None:
    gt_values = sum(len(gt["values"]) for gt in _groundtruth())  # type: ignore[arg-type]
    bench = load_benchmark(BENCHMARK_DIR)
    assert bench.version == "v0"
    assert sum(item.kind == "fact_lookup" for item in bench.items) == gt_values
    assert {item.kind for item in bench.items} == {
        "fact_lookup", "cross_company", "calculation", "research", "unanswerable",
    }
    assert not any("RITES" in item.question for item in bench.items)


def test_hand_keyed_items_point_only_at_ground_truth_pages() -> None:
    """Hand-keyed gold rests on `docs/groundtruth/`: every evidence location
    is a page a ground-truth file was read from, and every cross_company
    figure is a ground-truth value printed on that Report's page. Research
    items are the exception: their evidence is a note or narrative page,
    keyed from the Report itself (docs/groundtruth/README.md)."""
    printed: dict[tuple[str, int], set[float]] = {}
    for gt in _groundtruth():
        page = re.search(r"PDF page (\d+)", str(gt["_source"]))
        assert page is not None
        key = (ISSUER_DOC_KEYS[str(gt["issuer"])], int(page[1]))
        printed.setdefault(key, set()).update(gt["values"].values())  # type: ignore[attr-defined]

    hand_keyed = [i for i in load_benchmark(BENCHMARK_DIR).items
                  if i.kind not in ("fact_lookup", "research")]
    for item in hand_keyed:
        assert {(e.doc_key, e.page) for e in item.gold.evidence} <= set(printed), item.id
    for item in (i for i in hand_keyed if i.kind == "cross_company"):
        on_pages = set().union(*(printed[(e.doc_key, e.page)] for e in item.gold.evidence))
        assert {v.value for v in item.gold.values} <= on_pages, item.id


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


def _fake_stack(capsys: pytest.CaptureFixture[str], printed_before_ask: list[str]):
    """A scripted FDI API: two earlier succeeded /ask jobs that cost 0.20 and
    0.40 INR (and a failed one, which is no evidence of cost), then answers
    to this run's items, each billed 1,200 + 80 tokens = 0.25 INR."""
    answers = iter(["ACME's total assets were ₹100 crore.", "Total assets: ₹90 crore."])
    usage = {
        "o1": (900, 40, 0.20), "o2": (1_800, 90, 0.40),
        "j0": (1_200, 80, 0.25), "j1": (1_200, 80, 0.25),
    }
    submitted: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/auth/login":
            return httpx.Response(200, json={"access_token": "t"})
        if path == "/jobs" and request.method == "GET":
            assert request.url.params["kind"] == "ask"
            return httpx.Response(200, json={"jobs": [
                {"job_id": "o1", "status": "succeeded", "kind": "ask", "correlation_id": "old-1"},
                {"job_id": "o2", "status": "succeeded", "kind": "ask", "correlation_id": "old-2"},
                {"job_id": "o3", "status": "failed", "kind": "ask", "correlation_id": "old-3"},
            ]})
        if path.endswith("/usage"):
            job_id = path.removeprefix("/jobs/").removesuffix("/usage")
            in_tok, out_tok, cost = usage[job_id]
            return httpx.Response(200, json={
                "job_id": job_id, "correlation_id": "c", "call_count": 2,
                "input_tokens": in_tok, "output_tokens": out_tok, "duration_ms": 10,
                "total_cost_inr": cost, "by_source": [],
            })
        if path == "/ask":
            if not submitted:
                printed_before_ask.append(capsys.readouterr().out)
            n = len(submitted)
            submitted.append(f"j{n}")
            return httpx.Response(202, json={"job_id": f"j{n}", "correlation_id": f"cid-{n}",
                                             "status": "queued"})
        if path.endswith("/result"):
            # Both answers cite the gold page; only q0's figure is right.
            return httpx.Response(200, json={
                "answer": next(answers), "tool_trace": [],
                "citations": [{"doc_key": "acme-2025-26", "page": 7}],
            })
        if path.startswith("/jobs/"):
            n = path.removeprefix("/jobs/j")
            return httpx.Response(200, json={"job_id": f"j{n}", "status": "succeeded",
                                             "kind": "ask", "correlation_id": f"cid-{n}"})
        raise AssertionError(f"unexpected {request.method} {path}")

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


async def test_the_ask_adapter_keeps_the_tool_names_of_the_trace_in_call_order() -> None:
    """The wire shape is AskToolTraceEntryView; only the tool name is the
    observation. An entry with no usable name is dropped, never guessed."""

    def entry(round_num: int, tool: object) -> dict[str, object]:
        return {"round": round_num, "tool": tool, "arguments": {"query": "revenue"},
                "result": {"hits": []}, "error": None}

    result = {
        "answer": "Revenue was ₹2,71,423 crore.", "citations": [],
        "tool_trace": [entry(0, "search"), entry(0, "get_tables"), entry(1, "search"),
                       entry(1, None), "not an entry"],
    }
    async with httpx.AsyncClient(transport=_ask_stack(result), base_url="http://t") as http:
        answer = await AskJobAdapter(http, poll_interval=0.0).answer(_ITEM)

    assert answer.tool_trace == ("search", "get_tables", "search")


async def test_an_ask_result_with_no_tool_trace_key_called_no_tools() -> None:
    result = {"answer": "Revenue was ₹2,71,423 crore."}
    async with httpx.AsyncClient(transport=_ask_stack(result), base_url="http://t") as http:
        answer = await AskJobAdapter(http, poll_interval=0.0).answer(_ITEM)

    assert answer.tool_trace == ()


_NO_COST = summarise_cost([], ())


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
    body = json.loads(dump(report_json(_two_item_report(), {}, {}, _NO_COST)))
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
    summary = render_markdown(_two_item_report(), {}, {}, _NO_COST)
    assert "| fact_lookup | 2 | 1 | 50.0% | 1 | 50.0% | 3 | 1 | 33.3% |" in summary
    assert "| **overall** | 2 | 1 | 50.0% | 1 | 50.0% | 3 | 1 | 33.3% |" in summary
    unsupported = summary.split("## Correct but not completed")[1].split("\n## ")[0]
    assert "`q2`" in unsupported
    assert "`q1`" not in unsupported


def _tool_report() -> tuple[EvalReport, dict[str, MeasuredAnswer]]:
    """r1 names two tools and called one of them; q1 names none."""
    bench = Benchmark(version="v-test", items=(
        _ITEM,
        replace(_ITEM, id="r1", kind="research",
                gold=replace(_ITEM.gold, expected_tools=("search", "get_tables"))),
    ))
    answers = [
        EndpointAnswer(item_id="q1", answer="₹2,71,423 crore", tool_trace=("search",)),
        EndpointAnswer(item_id="r1", answer="₹2,71,423 crore",
                       tool_trace=("search", "document_structure")),
    ]
    measured = {
        a.item_id: MeasuredAnswer(answer=a, cost=ItemCost(a.item_id, "k", 1.0, None), job_id=None)
        for a in answers
    }
    return score_run(bench, answers), measured


def test_the_json_report_carries_tool_selection_only_where_tools_are_expected() -> None:
    report, measured = _tool_report()
    body = json.loads(dump(report_json(report, {}, measured, _NO_COST)))
    q1, r1 = body["items"]
    assert (q1["tool_selection"], q1["tools_called"]) == (None, ["search"])
    assert (r1["tool_selection"], r1["missing_tools"]) == (0.5, ["get_tables"])
    assert r1["tools_called"] == ["search", "document_structure"]
    assert body["by_kind"]["fact_lookup"]["tool_selection_accuracy"] is None
    assert body["by_kind"]["research"]["tool_selection_accuracy"] == 0.5
    assert body["overall"]["tool_selection_items"] == 1
    assert body["overall"]["tool_selection_accuracy"] == 0.5


def test_the_markdown_summary_has_a_tool_selection_table_with_absent_kinds_dashed() -> None:
    report, measured = _tool_report()
    summary = render_markdown(report, {}, measured, _NO_COST)
    section = summary.split("## Tool selection")[1].split("\n## ")[0]
    assert "| fact_lookup | 0 | - |" in section
    assert "| research | 1 | 50.0% |" in section
    assert "| **overall** | 1 | 50.0% |" in section
    assert "| `r1` | 50.0% | get_tables | search, document_structure |" in section


def test_the_markdown_summary_has_no_tool_selection_section_when_no_item_names_tools() -> None:
    assert "## Tool selection" not in render_markdown(_two_item_report(), {}, {}, _NO_COST)


def _written_report(path: Path, report: EvalReport, version: str = "v-test") -> str:
    stamp = {"benchmark_version": version, "git_commit": "abc", "mode": "ask"}
    path.write_text(dump(report_json(report, stamp, {}, _NO_COST)), encoding="utf-8")
    return str(path)


def test_diff_prints_what_changed_between_two_written_reports(
    capsys: pytest.CaptureFixture[str], tmp_path: Path,
) -> None:
    """OLD: q1 completed, q2 correct but cited off its page. NEW: q2 gains
    its gold citation (fixed) and q1 loses its number (regressed)."""
    bench = Benchmark(version="v-test", items=(_ITEM, replace(_ITEM, id="q2")))
    gold = Citation(doc_key="tcs-2025-26", page=362)
    new = score_run(bench, [
        EndpointAnswer(item_id="q1", answer="₹2,55,324 crore", citations=(gold,)),
        EndpointAnswer(item_id="q2", answer="₹2,71,423 crore", citations=(gold,)),
    ])
    out = tmp_path / "diff.md"
    code = main(["diff", _written_report(tmp_path / "old.json", _two_item_report()),
                 _written_report(tmp_path / "new.json", new), "--out", str(out)])
    assert code == 0

    printed = capsys.readouterr().out
    assert "| fact_lookup | 1 | 1 | 0 | 0 | 0 | +0 |" in printed
    assert "| `q2` | fact_lookup | correct | correct |" in printed.split("## Fixed")[1]
    assert "| `q1` | fact_lookup | correct | wrong |" in printed.split("## Regressed")[1]
    assert out.read_text(encoding="utf-8") in printed


def test_diff_refuses_reports_of_different_benchmark_versions_unless_forced(
    capsys: pytest.CaptureFixture[str], tmp_path: Path,
) -> None:
    old = _written_report(tmp_path / "old.json", _two_item_report(), version="v0")
    new = _written_report(tmp_path / "new.json", _two_item_report(), version="v1")

    assert main(["diff", old, new]) == 0
    printed = capsys.readouterr().out
    assert "cannot compare" in printed
    assert "OLD 'v0', NEW 'v1'" in printed

    main(["diff", old, new, "--force"])
    printed = capsys.readouterr().out
    assert "| **overall** | 0 | 0 | 2 | 0 | 0 | +0 |" in printed
    assert "forced past" in printed


def test_diff_refuses_an_unreadable_report_even_when_forced(
    capsys: pytest.CaptureFixture[str], tmp_path: Path,
) -> None:
    old = _written_report(tmp_path / "old.json", _two_item_report())
    broken = tmp_path / "broken.json"
    broken.write_text(json.dumps({"stamp": {}, "items": "nope"}), encoding="utf-8")

    assert main(["diff", old, str(broken), "--force"]) == 0
    printed = capsys.readouterr().out
    assert "cannot read the reports: NEW report 'items' must be a list" in printed
    assert "## Fixed" not in printed


def test_every_report_metric_is_broken_down_by_kind() -> None:
    """Accuracy, completion, latency AND spend each get a row per item kind."""
    refusal = BenchmarkItem(id="u1", kind="unanswerable", question="Infosys's revenue?",
                            gold=Gold(answerable=False))
    bench = Benchmark(version="v-test", items=(_ITEM, refusal))
    report = score_run(bench, [
        EndpointAnswer(item_id="q1", answer="₹2,71,423 crore",
                       citations=(Citation(doc_key="tcs-2025-26", page=362),)),
        EndpointAnswer(item_id="u1", answer="Infosys is not in the available Reports."),
    ])
    cost = summarise_cost([
        ItemCost("q1", "fact_lookup", 2.0, ItemUsage(1_000, 50, 0.30)),
        ItemCost("u1", "unanswerable", 1.0, ItemUsage(400, 20, 0.10)),
    ], {"q1", "u1"})

    body = json.loads(dump(report_json(report, {}, {}, cost)))
    assert set(body["by_kind"]) == {"fact_lookup", "unanswerable"}
    assert body["by_kind"]["unanswerable"]["completed"] == 1
    assert body["cost"]["spend_by_kind"]["unanswerable"]["cost_inr"] == pytest.approx(0.10)

    summary = render_markdown(report, {}, {}, cost)
    assert "| unanswerable | 1 | 1 | 0 | 0 | 100.0% |" in summary
    assert "| unanswerable | 1 | 1 | 100.0% | 0 | 0.0% | 0 | 0 | 0.0% |" in summary
    assert "| unanswerable | 1 | 1.00 | 1.00 |" in summary
    assert ("| kind | input tokens | output tokens | cost | completed "
            "| cost per completed task |") in summary
    assert "| unanswerable | 400 | 20 | INR 0.1000 | 1 | INR 0.1000 |" in summary
    assert "| **overall** | 1400 | 70 | INR 0.4000 | 2 | INR 0.2000 |" in summary


async def test_an_ask_result_with_no_citations_key_has_no_citations() -> None:
    result = {"answer": "Revenue was ₹2,71,423 crore."}
    async with httpx.AsyncClient(transport=_ask_stack(result), base_url="http://t") as http:
        answer = await AskJobAdapter(http, poll_interval=0.0).answer(_ITEM)

    assert answer.answer == "Revenue was ₹2,71,423 crore."
    assert answer.citations == ()


def _two_item_benchmark(directory: Path) -> Path:
    def item(n: int) -> dict[str, object]:
        return {
            "id": f"fact_lookup:acme:q{n}", "kind": "fact_lookup",
            "question": f"What were ACME's total assets? (q{n})",
            "gold": {"answerable": True,
                     "values": [{"value": 100.0, "unit": "INR crore", "tolerance": 0.5}],
                     "evidence": [{"doc_key": "acme-2025-26", "page": 7}]},
        }

    directory.mkdir()
    (directory / "fact_lookup.json").write_text(
        json.dumps({"version": "v-test", "items": [item(0), item(1)]}), encoding="utf-8"
    )
    return directory


def test_run_prints_its_expected_spend_before_the_first_billed_call_and_reports_cost(
    capsys: pytest.CaptureFixture[str], tmp_path: Path,
) -> None:
    printed_before_ask: list[str] = []
    out_dir = tmp_path / "out"
    code = main(
        ["run", "--endpoint", "http://fdi.test", "--out-dir", str(out_dir), "--limit", "2",
         "--email", "eval@example.test", "--password", "pw",
         "--benchmark", str(_two_item_benchmark(tmp_path / "bench"))],
        transport=_fake_stack(capsys, printed_before_ask),
    )
    assert code == 0

    # 2 items x the 0.30 INR average of the two recent succeeded jobs.
    assert printed_before_ask, "no /ask was ever submitted"
    assert "expected spend: INR 0.60" in printed_before_ask[0]
    assert "2 recent /ask job(s)" in printed_before_ask[0]

    body = json.loads((out_dir / "report.json").read_text(encoding="utf-8"))
    first = body["items"][0]
    assert first["job_id"] == "j0"
    assert (first["input_tokens"], first["output_tokens"]) == (1_200, 80)
    assert first["cost_inr"] == pytest.approx(0.25)
    assert first["latency_s"] >= 0.0
    cost = body["cost"]
    assert cost["cost_inr"] == pytest.approx(0.50)
    # Only q0 is completed, so it carries both items' spend.
    assert cost["completed"] == 1
    assert cost["cost_per_completed_task_inr"] == pytest.approx(0.50)
    assert cost["latency_by_kind"]["fact_lookup"]["count"] == 2

    summary = (out_dir / "report.md").read_text(encoding="utf-8")
    assert "| kind | items | P50 latency (s) | P95 latency (s) |" in summary
    assert "cost per completed task**: INR 0.5000" in summary
    assert "| `fact_lookup:acme:q0` | correct |" in summary


def test_with_no_recent_usage_the_run_says_it_cannot_estimate_and_still_runs_its_limit(
    capsys: pytest.CaptureFixture[str], tmp_path: Path,
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/login":
            return httpx.Response(200, json={"access_token": "t"})
        if request.url.path == "/jobs":
            return httpx.Response(200, json={"jobs": []})
        return httpx.Response(503, json={"detail": "agent_client"})

    main(
        ["run", "--endpoint", "http://fdi.test", "--out-dir", str(tmp_path / "out"),
         "--limit", "1", "--email", "e", "--password", "p",
         "--benchmark", str(_two_item_benchmark(tmp_path / "bench"))],
        transport=httpx.MockTransport(handle),
    )
    out = capsys.readouterr().out
    assert "no recent /ask usage to estimate from" in out
    assert "[1/1]" in out


def test_with_no_recent_usage_yes_alone_is_refused_before_any_billed_call(
    capsys: pytest.CaptureFixture[str], tmp_path: Path,
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/login":
            return httpx.Response(200, json={"access_token": "t"})
        if request.url.path == "/jobs":
            return httpx.Response(200, json={"jobs": []})
        raise AssertionError(f"billed or unexpected call: {request.method} {request.url.path}")

    main(
        ["run", "--endpoint", "http://fdi.test", "--out-dir", str(tmp_path / "out"),
         "--yes", "--email", "e", "--password", "p",
         "--benchmark", str(_two_item_benchmark(tmp_path / "bench"))],
        transport=httpx.MockTransport(handle),
    )
    assert "run a small --limit first" in capsys.readouterr().out
    assert not (tmp_path / "out").exists()


# --------------------------------------------------------------------------
# The optional LLM judge: off by default, its own columns, its own spend.
# --------------------------------------------------------------------------


def _research_benchmark(directory: Path) -> Path:
    """r0 answered (and judged); r1's /ask job fails, so its claims are
    unsupported without a judge call; f0 has no claims and is never judged."""

    def research(n: int) -> dict[str, object]:
        return {
            "id": f"research:acme:r{n}", "kind": "research",
            "question": f"What does ACME's other income note say? (r{n})",
            "gold": {"answerable": True,
                     "evidence": [{"doc_key": "acme-2025-26", "page": 31}],
                     "required_claims": ["Interest is the largest component.",
                                         "Dividends rose."]},
        }

    directory.mkdir()
    (directory / "research.json").write_text(json.dumps(
        {"version": "v-test", "items": [research(0), research(1)]}), encoding="utf-8")
    (directory / "fact_lookup.json").write_text(json.dumps({"version": "v-test", "items": [{
        "id": "fact_lookup:acme:f0", "kind": "fact_lookup", "question": "Total assets?",
        "gold": {"answerable": True,
                 "values": [{"value": 100.0, "unit": "INR crore", "tolerance": 0.5}],
                 "evidence": [{"doc_key": "acme-2025-26", "page": 7}]},
    }]}), encoding="utf-8")
    return directory


def _research_stack() -> httpx.MockTransport:
    """Recent /ask history: one job at 0.30 INR. Every job this run submits
    bills 0.25 INR; f0 and r0 succeed, r1's job fails. r0's tool trace is
    the evidence it cited."""

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/auth/login":
            return httpx.Response(200, json={"access_token": "t"})
        if path == "/jobs" and request.method == "GET":
            return httpx.Response(200, json={"jobs": [{"job_id": "o1", "status": "succeeded"}]})
        if path.endswith("/usage"):
            cost = 0.30 if path == "/jobs/o1/usage" else 0.25
            return httpx.Response(200, json={"input_tokens": 1_000, "output_tokens": 50,
                                             "total_cost_inr": cost})
        if path == "/ask":
            question = json.loads(request.content)["question"]
            job = "jr1" if "(r1)" in question else ("jr0" if "(r0)" in question else "jf0")
            return httpx.Response(202, json={"job_id": job})
        if path == "/jobs/jr1":
            return httpx.Response(200, json={"status": "failed", "error": "boom"})
        if path in ("/jobs/jr0", "/jobs/jf0"):
            return httpx.Response(200, json={"status": "succeeded"})
        if path == "/jobs/jr0/result":
            return httpx.Response(200, json={
                "answer": "Interest is the largest component of other income.",
                "citations": [{"doc_key": "acme-2025-26", "page": 31}],
                "tool_trace": [
                    {"round": 0, "tool": "search", "arguments": {"query": "other income"},
                     "result": {"results": [{"doc_key": "acme-2025-26", "page": 31,
                                             "text": "Interest 13,302"}]}, "error": None},
                    # Retrieved but cites nothing: not evidence the judge may use.
                    {"round": 0, "tool": "search", "arguments": {"query": "dividends"},
                     "result": {"results": []}, "error": None},
                    {"round": 1, "tool": "get_tables", "arguments": {},
                     "result": {"error": "no such doc"}, "error": "no such doc"},
                ],
            })
        if path == "/jobs/jf0/result":
            return httpx.Response(200, json={
                "answer": "Total assets were ₹100 crore.",
                "citations": [{"doc_key": "acme-2025-26", "page": 7}], "tool_trace": [],
            })
        raise AssertionError(f"unexpected {request.method} {path}")

    return httpx.MockTransport(handle)


class _FakeJudge:
    """Supports claim 0 only; each call bills 0.04 INR. Recent history: two
    judge calls at 0.02 and 0.06 INR."""

    name = "fake-judge"

    def __init__(self) -> None:
        self.seen: list[tuple[str, str, tuple[str, ...]]] = []

    async def recent_costs(self) -> list[float]:
        return [0.02, 0.06]

    async def judge(
        self, item: BenchmarkItem, answer: str, evidence: Sequence[str]
    ) -> JudgedItem:
        self.seen.append((item.id, answer, tuple(evidence)))
        reply = json.dumps({"claims": [{"index": 0, "supported": True, "reason": "in note"},
                                       {"index": 1, "supported": False, "reason": "absent"}]})
        return JudgedItem(judgement=parse_judgement(item, reply),
                          usage=ItemUsage(input_tokens=3_000, output_tokens=60, cost_inr=0.04))


def _run_research(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], name: str, *extra: str,
    judge: _FakeJudge | None = None,
) -> tuple[dict[str, Any], str, str]:
    out_dir = tmp_path / name
    capsys.readouterr()
    main(["run", "--endpoint", "http://fdi.test", "--out-dir", str(out_dir), "--limit", "3",
          "--email", "e", "--password", "p", "--benchmark",
          str(_research_benchmark(tmp_path / f"{name}-bench")), *extra],
         transport=_research_stack(), judge=judge)
    body = json.loads((out_dir / "report.json").read_text(encoding="utf-8"))
    summary = (out_dir / "report.md").read_text(encoding="utf-8")
    return body, summary, capsys.readouterr().out


def test_without_the_flag_there_is_no_judge_call_and_no_judge_column(
    capsys: pytest.CaptureFixture[str], tmp_path: Path,
) -> None:
    judge = _FakeJudge()
    body, summary, _ = _run_research(tmp_path, capsys, "plain", judge=judge)
    assert judge.seen == []
    assert "judge" not in body
    assert all("judge" not in item for item in body["items"])
    assert "LLM judge" not in summary
    assert body["cost"]["judge_cost_inr"] == 0.0


def test_the_judge_sees_answered_items_with_claims_and_their_cited_evidence(
    capsys: pytest.CaptureFixture[str], tmp_path: Path,
) -> None:
    judge = _FakeJudge()
    _run_research(tmp_path, capsys, "judged", "--judge", judge=judge)
    # Only r0: f0 has no claims, and r1 never answered.
    assert [(item_id, answer) for item_id, answer, _ in judge.seen] == [
        ("research:acme:r0", "Interest is the largest component of other income."),
    ]
    evidence = judge.seen[0][2]
    assert len(evidence) == 1 and "Interest 13,302" in evidence[0]


def test_judge_columns_sit_beside_unchanged_deterministic_columns(
    capsys: pytest.CaptureFixture[str], tmp_path: Path,
) -> None:
    plain, _, _ = _run_research(tmp_path, capsys, "plain")
    judged, summary, _ = _run_research(tmp_path, capsys, "judged", "--judge", judge=_FakeJudge())

    def deterministic(body: dict[str, Any]) -> object:
        return (body["overall"], body["by_kind"],
                [{k: v for k, v in i.items() if k not in ("latency_s", "judge", "judge_cost_inr")}
                 for i in body["items"]])

    assert deterministic(judged) == deterministic(plain)

    # r0: 1 of 2 claims; r1 (failed): 0 of 2, unjudged. f0: no claims, no column.
    research = judged["judge"]["by_kind"]["research"]
    assert (research["items"], research["judge_errors"], research["claims"],
            research["supported_claims"], research["fully_supported"]) == (2, 0, 4, 1, 0)
    assert research["claim_support_rate"] == 0.25
    assert set(judged["judge"]["by_kind"]) == {"research"}
    r0 = next(i for i in judged["items"] if i["item_id"] == "research:acme:r0")
    assert [v["supported"] for v in r0["judge"]["verdicts"]] == [True, False]
    f0 = next(i for i in judged["items"] if i["item_id"] == "fact_lookup:acme:f0")
    assert "judge" not in f0
    assert judged["stamp"]["judge"] == "fake-judge"

    section = summary.split("## LLM judge")[1].split("\n## ")[0]
    assert "| research | 2 | 0 | 4 | 1 | 25.0% | 0 | 0.0% |" in section


def test_judge_spend_is_estimated_up_front_and_counted_in_the_totals(
    capsys: pytest.CaptureFixture[str], tmp_path: Path,
) -> None:
    """Estimate: 3 items x 0.30 = 0.90 for /ask, plus 2 items with claims x
    mean(0.02, 0.06) = 0.08 for the judge -- 0.98. Actual: three /ask jobs
    at 0.25 (r1's failed job billed too) = 0.75, and one judge call, 0.04."""
    body, summary, printed = _run_research(tmp_path, capsys, "judged", "--judge",
                                           judge=_FakeJudge())
    assert "expected spend: INR 0.98" in printed
    assert "judge INR 0.08 = 2 item(s) x INR 0.0400" in printed
    cost = body["cost"]
    assert cost["cost_inr"] == pytest.approx(0.75)
    assert cost["judge_cost_inr"] == pytest.approx(0.04)
    assert cost["total_cost_inr"] == pytest.approx(0.79)
    r0 = next(i for i in body["items"] if i["item_id"] == "research:acme:r0")
    assert r0["judge_cost_inr"] == pytest.approx(0.04)
    assert "judge**: INR 0.0400" in summary
    assert "total**: INR 0.7900" in summary


def test_the_judge_is_refused_in_retrieval_mode(capsys: pytest.CaptureFixture[str]) -> None:
    main(["run", "--endpoint", "http://127.0.0.1:9", "--out-dir", "unused",
          "--mode", "retrieval", "--judge"], judge=_FakeJudge())
    assert "--judge scores answers" in capsys.readouterr().out
