"""Measure answer quality: run the benchmark against an answering endpoint.

The retrieval/agent counterpart of `score_pipeline.py`. Two commands:

    # (Re)generate the fact_lookup items from docs/groundtruth/ -- never typed.
    uv run --directory backend python ../scripts/eval_answers.py generate

    # Run them against a live FDI stack's /ask, as the dedicated eval Invitee.
    FDI_EVAL_EMAIL=eval@... FDI_EVAL_PASSWORD=... \\
    uv run --directory backend python ../scripts/eval_answers.py run \\
        --endpoint http://localhost:9514 --limit 5 --out-dir ../eval-runs

The eval Invitee is an ordinary account created with the admin CLI; using a
dedicated one keeps eval usage separable from real Invitees' and subject to
the same rate limits.

`run` talks to the stack only through its public HTTP API, one item at a
time (the per-Invitee concurrency limit), and calls a billed endpoint, so it
refuses to start without `--limit N` or `--yes`. A failed or timed-out item
is recorded with its reason and the run continues.

COST. Each item's wall-clock latency (submit to answer) is measured here;
its tokens and INR cost are read afterwards from `GET /usage/{correlation
id}` -- the `llm_usage_event` rows billed under that item's job. Before the
first billed call the run prints its expected spend: items x the average
cost of the eval Invitee's most recent succeeded `/ask` jobs (the last
`RECENT_JOBS`, read through the same endpoint). That source is live: it
prices at the stack's current configured rates and reflects today's model
and prompt, not a stale report. A first run has no history and says so --
run a small `--limit` first to seed it.

Exit code is always 0: this is a measurement tool, not a gate. The report it
writes is the number.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx

from fdi.core.answer_scoring import (
    Citation,
    EndpointAnswer,
    EvalReport,
    KindSummary,
    score_run,
)
from fdi.core.benchmark import (
    Benchmark,
    BenchmarkError,
    BenchmarkItem,
    fact_lookup_benchmark,
    parse_benchmark,
)
from fdi.core.eval_cost import (
    CostSummary,
    ItemCost,
    ItemUsage,
    LatencySummary,
    SpendEstimate,
    estimate_spend,
    summarise_cost,
)

ROOT = Path(__file__).resolve().parents[1]
GT_DIR = ROOT / "docs" / "groundtruth"
SCHEMA = ROOT / "docs" / "architecture" / "field-extraction-schema.v4_3.json"
BENCHMARK_VERSION = "v0"
BENCHMARK_DIR = GT_DIR / "benchmark" / BENCHMARK_VERSION
# How many of the eval Invitee's latest /ask jobs the spend estimate averages.
RECENT_JOBS = 20

# --------------------------------------------------------------------------
# generate
# --------------------------------------------------------------------------


def schema_labels(schema: Mapping[str, object]) -> dict[str, str]:
    """tag -> human field name, over every node of the standard schema."""
    labels: dict[str, str] = {}

    def walk(node: object) -> None:
        if not isinstance(node, Mapping):
            return
        tag, name = node.get("tag"), node.get("field_name")
        if isinstance(tag, str) and tag and isinstance(name, str) and name:
            labels[tag] = name
        children = node.get("fields")
        for child in children if isinstance(children, list) else []:
            walk(child)

    modules = schema.get("modules")
    for module in modules if isinstance(modules, list) else []:
        units = module.get("retrieval_units") if isinstance(module, Mapping) else None
        for unit in units if isinstance(units, list) else []:
            fields = unit.get("fields") if isinstance(unit, Mapping) else None
            for field in fields if isinstance(fields, list) else []:
                walk(field)
    return labels


def generate_fact_lookup(gt_dir: Path = GT_DIR, schema: Path = SCHEMA) -> dict[str, object]:
    bodies = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(gt_dir.glob("*.json"))]
    labels = schema_labels(json.loads(schema.read_text(encoding="utf-8")))
    return fact_lookup_benchmark(BENCHMARK_VERSION, bodies, labels)


def dump(body: object) -> str:
    return json.dumps(body, indent=2, ensure_ascii=False) + "\n"


# --------------------------------------------------------------------------
# The answering-endpoint adapter: today's /ask job flow.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class MeasuredAnswer:
    """An item's answer plus what producing it cost. `correlation_id` is the
    job's (None when no job was ever accepted); `cost.usage` is None when
    its usage could not be read."""

    answer: EndpointAnswer
    cost: ItemCost
    correlation_id: str | None


class AskJobAdapter:
    """Answers a benchmark item through today's `/ask`: submit, poll
    `GET /jobs/{id}`, read `/jobs/{id}/result`, and normalise the response
    into an `EndpointAnswer`. Every failure becomes an EndpointAnswer with an
    `error`, never an exception, so one bad item cannot end the run.

    `sleep` is the wait between polls; it is injectable so a test can play
    the worker while the adapter waits. `clock` times each item for
    `measure`."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        poll_interval: float = 2.0,
        timeout: float = 300.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._poll_interval = poll_interval
        self._timeout = timeout
        self._sleep = sleep
        self._clock = clock
        self._correlation_id: str | None = None

    async def login(self, email: str, password: str) -> None:
        r = await self._client.post("/auth/login", json={"email": email, "password": password})
        r.raise_for_status()
        self._client.headers["Authorization"] = f"Bearer {r.json()['access_token']}"

    async def measure(self, item: BenchmarkItem) -> MeasuredAnswer:
        """`answer`, timed wall-clock, then priced from the usage billed
        under its job's correlation id."""
        self._correlation_id = None
        started = self._clock()
        answer = await self.answer(item)
        latency = self._clock() - started
        cid = self._correlation_id
        usage = await self.usage(cid) if cid is not None else None
        return MeasuredAnswer(
            answer=answer,
            cost=ItemCost(item_id=item.id, kind=item.kind, latency_s=latency, usage=usage),
            correlation_id=cid,
        )

    async def usage(self, correlation_id: str) -> ItemUsage | None:
        """What was billed under one of our jobs' correlation ids; None when
        the stack would not say (the item is then reported unpriced)."""
        try:
            r = await self._client.get(f"/usage/{correlation_id}")
        except httpx.HTTPError:
            return None
        if r.status_code != 200:
            return None
        body = r.json()
        return ItemUsage(input_tokens=int(body["input_tokens"]),
                         output_tokens=int(body["output_tokens"]),
                         cost_inr=float(body["cost_inr"]))

    async def recent_item_costs(self, jobs: int = RECENT_JOBS) -> list[float]:
        """The INR cost of each of our latest `jobs` succeeded /ask jobs that
        billed anything -- the history the spend estimate averages."""
        r = await self._client.get("/jobs", params={"kind": "ask", "limit": jobs})
        r.raise_for_status()
        costs = []
        for job in r.json()["jobs"]:
            cid = job.get("correlation_id")
            if job.get("status") != "succeeded" or not cid:
                continue
            usage = await self.usage(cid)
            if usage is not None and (usage.input_tokens or usage.output_tokens):
                costs.append(usage.cost_inr)
        return costs

    async def answer(self, item: BenchmarkItem) -> EndpointAnswer:
        try:
            return await self._answer(item)
        except httpx.HTTPError as exc:
            return EndpointAnswer(item_id=item.id, answer=None,
                               error=f"{type(exc).__name__}: {exc}")

    async def _answer(self, item: BenchmarkItem) -> EndpointAnswer:
        def failed(error: str) -> EndpointAnswer:
            return EndpointAnswer(item_id=item.id, answer=None, error=error)

        submitted = await self._client.post("/ask", json={"question": item.question})
        if submitted.status_code != 202:
            return failed(f"POST /ask returned {submitted.status_code}: {submitted.text}")
        job_id = submitted.json()["job_id"]
        self._correlation_id = submitted.json().get("correlation_id")

        deadline = time.monotonic() + self._timeout
        while True:
            polled = await self._client.get(f"/jobs/{job_id}")
            if polled.status_code != 200:
                return failed(f"GET /jobs/{job_id} returned {polled.status_code}")
            job = polled.json()
            if job["status"] == "failed":
                return failed(f"job {job_id} failed: {job.get('error')}")
            if job["status"] == "succeeded":
                break
            if time.monotonic() >= deadline:
                return failed(
                    f"timed out after {self._timeout:g}s; job {job_id} still {job['status']}"
                )
            await self._sleep(self._poll_interval)

        result = await self._client.get(f"/jobs/{job_id}/result")
        if result.status_code != 200:
            return failed(f"GET /jobs/{job_id}/result returned {result.status_code}")
        body = result.json()
        answer = body.get("answer")
        if not isinstance(answer, str):
            return failed(f"job {job_id} result carries no answer text")
        raw = body.get("citations")
        return EndpointAnswer(
            item_id=item.id, answer=answer,
            citations=tuple(_citation(c) for c in raw if isinstance(c, Mapping))
            if isinstance(raw, list) else (),
        )


def _citation(raw: Mapping[str, object]) -> Citation:
    """One `/ask` citation (AskCitationView: every key present, most null)
    as a `Citation`. A value of the wrong type is dropped, never coerced: a
    citation with a garbled page must not become valid by accident."""

    def text(key: str) -> str | None:
        value = raw.get(key)
        return value if isinstance(value, str) else None

    def integer(key: str) -> int | None:
        value = raw.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    return Citation(
        doc_key=text("doc_key"), page=integer("page"), doc_id=text("doc_id"),
        table_index=integer("table_index"), row_label=text("row_label"),
    )


# --------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------


def load_benchmark(directory: Path) -> Benchmark:
    """Every `*.json` kind file in `directory`, as one benchmark. Raises
    BenchmarkError when a file is malformed or the files disagree."""
    files = sorted(directory.glob("*.json"))
    if not files:
        raise BenchmarkError(f"no benchmark files in {directory}")
    parts = []
    for path in files:
        try:
            parts.append(parse_benchmark(json.loads(path.read_text(encoding="utf-8"))))
        except BenchmarkError as exc:
            raise BenchmarkError(f"{path.name}: {exc}") from None
    versions = {p.version for p in parts}
    if len(versions) != 1:
        raise BenchmarkError(f"benchmark files disagree on version: {sorted(versions)}")
    items = tuple(item for part in parts for item in part.items)
    ids = [item.id for item in items]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise BenchmarkError(f"item ids repeated across files: {duplicates}")
    return Benchmark(version=versions.pop(), items=items)


async def run_eval(
    benchmark: Benchmark,
    adapter: AskJobAdapter,
    *,
    limit: int | None,
    stamp: Mapping[str, object],
    out_dir: Path,
) -> EvalReport:
    """Answer the first `limit` items (all when None) one at a time, score
    them, and write `report.json` + `report.md` into `out_dir`."""
    items = benchmark.items if limit is None else benchmark.items[:limit]
    measured: list[MeasuredAnswer] = []
    for n, item in enumerate(items, start=1):
        m = await adapter.measure(item)
        print(f"[{n}/{len(items)}] {item.id}: {'error' if m.answer.error else 'answered'}")
        measured.append(m)
    report = score_run(benchmark, [m.answer for m in measured])
    completed = {i.item_id for i in report.items if i.completed}
    cost = summarise_cost([m.cost for m in measured], completed)
    write_reports(out_dir, report, stamp, {m.answer.item_id: m for m in measured}, cost)
    return report


def write_reports(
    out_dir: Path,
    report: EvalReport,
    stamp: Mapping[str, object],
    measured: Mapping[str, MeasuredAnswer],
    cost: CostSummary,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(dump(report_json(report, stamp, measured, cost)),
                                         encoding="utf-8")
    (out_dir / "report.md").write_text(render_markdown(report, stamp, measured, cost),
                                       encoding="utf-8")


def _item_cost_fields(m: MeasuredAnswer | None) -> dict[str, object]:
    """An item's latency/usage columns; usage fields are None when unread."""
    usage = m.cost.usage if m else None
    return {
        "correlation_id": m.correlation_id if m else None,
        "latency_s": m.cost.latency_s if m else None,
        "input_tokens": usage.input_tokens if usage else None,
        "output_tokens": usage.output_tokens if usage else None,
        "cost_inr": usage.cost_inr if usage else None,
    }


def report_json(
    report: EvalReport,
    stamp: Mapping[str, object],
    measured: Mapping[str, MeasuredAnswer],
    cost: CostSummary,
) -> dict[str, object]:
    def summary(s: KindSummary) -> dict[str, object]:
        return {
            **asdict(s), "accuracy": s.accuracy, "citation_validity": s.citation_validity,
            "evidence_support_rate": s.evidence_support_rate,
            "completion_rate": s.completion_rate,
        }

    def answer(item_id: str) -> str | None:
        m = measured.get(item_id)
        return m.answer.answer if m else None

    return {
        "stamp": dict(stamp),
        "benchmark_size": report.benchmark_size,
        "overall": summary(report.overall),
        "by_kind": {kind: summary(s) for kind, s in report.by_kind.items()},
        "cost": asdict(cost),
        "items": [
            {**asdict(i), "citation_validity": i.citation_validity,
             "answer": answer(i.item_id),
             **_item_cost_fields(measured.get(i.item_id))}
            for i in report.items
        ],
    }


def _seconds(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}"


def _inr(value: float | None) -> str:
    return "-" if value is None else f"INR {value:.4f}"


def render_cost_markdown(
    report: EvalReport, measured: Mapping[str, MeasuredAnswer], cost: CostSummary
) -> list[str]:
    def latency_row(kind: str, s: LatencySummary) -> str:
        return f"| {kind} | {s.count} | {_seconds(s.p50_s)} | {_seconds(s.p95_s)} |"

    lines = [
        "", "## Latency and cost", "",
        f"- **cost**: {_inr(cost.cost_inr)} ({cost.input_tokens} input + "
        f"{cost.output_tokens} output tokens)",
        f"- **completed**: {cost.completed} of {report.overall.run}",
        f"- **cost per completed task**: {_inr(cost.cost_per_completed_task_inr)}",
    ]
    if cost.items_without_usage:
        lines.append(f"- **items whose usage could not be read** (not in the cost): "
                     f"{cost.items_without_usage}")
    lines += ["", "| kind | items | P50 latency (s) | P95 latency (s) |", "|---|---:|---:|---:|"]
    lines += [latency_row(kind, s) for kind, s in cost.latency_by_kind.items()]
    lines.append(latency_row("**overall**", cost.latency_overall))
    lines += ["", "| item | status | latency (s) | input tokens | output tokens | cost |",
              "|---|---|---:|---:|---:|---:|"]
    for i in report.items:
        m = measured.get(i.item_id)
        usage = m.cost.usage if m else None
        lines.append(
            f"| `{i.item_id}` | {i.status} | {_seconds(m.cost.latency_s if m else None)} "
            f"| {usage.input_tokens if usage else '-'} "
            f"| {usage.output_tokens if usage else '-'} "
            f"| {_inr(usage.cost_inr if usage else None)} |"
        )
    return lines


def spend_line(estimate: SpendEstimate) -> str:
    if estimate.total_cost_inr is None or estimate.average_cost_inr is None:
        return (f"{estimate.items} item(s) will call /ask, a billed endpoint; no recent /ask "
                "usage to estimate from (a first run? try a small --limit first).")
    return (f"expected spend: INR {estimate.total_cost_inr:.2f} = {estimate.items} item(s) x "
            f"INR {estimate.average_cost_inr:.4f}, the average of {estimate.sample_size} "
            "recent /ask job(s).")


def render_markdown(
    report: EvalReport,
    stamp: Mapping[str, object],
    measured: Mapping[str, MeasuredAnswer],
    cost: CostSummary,
) -> str:
    lines = ["# Answer-quality eval", ""]
    lines += [f"- **{key}**: `{value}`" for key, value in stamp.items()]
    lines += [
        f"- **items run**: {report.overall.run} of {report.benchmark_size}",
        "",
        "| kind | run | correct | wrong | failed | accuracy |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    rows = [*report.by_kind.items(), ("**overall**", report.overall)]
    lines += [
        f"| {kind} | {s.run} | {s.correct} | {s.wrong} | {s.failed} | {s.accuracy:.1%} |"
        for kind, s in rows
    ]
    lines += [
        "",
        "## Task completion",
        "",
        "Completed = every gold value stated AND at least one citation on a gold "
        "evidence page.",
        "",
        "| kind | run | completed | completion | supported | evidence support "
        "| citations | valid | citation validity |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    lines += [
        f"| {kind} | {s.run} | {s.completed} | {s.completion_rate:.1%} | {s.supported} "
        f"| {s.evidence_support_rate:.1%} | {s.citations} | {s.valid_citations} "
        f"| {s.citation_validity:.1%} |"
        for kind, s in rows
    ]
    misses = [i for i in report.items if i.status != "correct"]
    if misses:
        lines += ["", "## Not correct", "", "| item | status | reason |", "|---|---|---|"]
        lines += [
            f"| `{i.item_id}` | {i.status} | {i.reason.replace('|', '/')} |" for i in misses
        ]
    unsupported = [i for i in report.items if i.status == "correct" and not i.completed]
    if unsupported:
        lines += ["", "## Correct but not completed", "",
                  "No citation on a gold evidence page.", "",
                  "| item | citations | valid |", "|---|---:|---:|"]
        lines += [f"| `{i.item_id}` | {i.citations} | {i.valid_citations} |"
                  for i in unsupported]
    lines += render_cost_markdown(report, measured, cost)
    return "\n".join(lines) + "\n"


def git_commit() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True
        )
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return out.stdout.strip()


async def _run(
    args: argparse.Namespace,
    benchmark: Benchmark,
    transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    email = args.email or os.environ.get("FDI_EVAL_EMAIL")
    password = args.password or os.environ.get("FDI_EVAL_PASSWORD")
    if not email or not password:
        print("eval Invitee credentials missing: pass --email/--password or set "
              "FDI_EVAL_EMAIL/FDI_EVAL_PASSWORD")
        return
    stamp = {
        "benchmark_version": benchmark.version,
        "git_commit": git_commit(),
        "endpoint": args.endpoint,
        "mode": "ask",
        "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    async with httpx.AsyncClient(
        base_url=args.endpoint, timeout=60.0, transport=transport
    ) as client:
        adapter = AskJobAdapter(client, poll_interval=args.poll_interval, timeout=args.timeout)
        try:
            await adapter.login(email, password)
            recent = await adapter.recent_item_costs()
        except httpx.HTTPError as exc:
            print(f"could not reach the stack as the eval Invitee: {exc}")
            return
        items = len(benchmark.items) if args.limit is None else min(args.limit,
                                                                     len(benchmark.items))
        # Printed before the first billed call, which run_eval makes.
        print(spend_line(estimate_spend(items, recent)))
        report = await run_eval(
            benchmark, adapter, limit=args.limit, stamp=stamp, out_dir=args.out_dir
        )
    o = report.overall
    print(f"{o.correct}/{o.run} correct ({o.accuracy:.1%}), {o.failed} failed; "
          f"reports in {args.out_dir}")


def main(
    argv: Sequence[str] | None = None, *, transport: httpx.AsyncBaseTransport | None = None
) -> int:
    """`transport` replaces the network, for tests; None is the real one."""
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate", help="regenerate fact_lookup items from ground truth")
    gen.add_argument("--out", type=Path, default=BENCHMARK_DIR / "fact_lookup.json")

    run = sub.add_parser("run", help="run the benchmark against /ask")
    run.add_argument("--benchmark", type=Path, default=BENCHMARK_DIR)
    run.add_argument("--endpoint", required=True, help="the FDI API base URL")
    run.add_argument("--out-dir", required=True, type=Path)
    run.add_argument("--email", help="eval Invitee email (default: $FDI_EVAL_EMAIL)")
    run.add_argument("--password", help="eval Invitee password (default: $FDI_EVAL_PASSWORD)")
    run.add_argument("--limit", type=int, help="answer only the first N items")
    run.add_argument("--yes", action="store_true", help="answer every item (billed)")
    run.add_argument("--timeout", type=float, default=300.0, help="seconds per item")
    run.add_argument("--poll-interval", type=float, default=2.0)
    args = parser.parse_args(argv)

    if args.command == "generate":
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(dump(generate_fact_lookup()), encoding="utf-8")
        print(f"wrote {args.out}")
        return 0

    try:
        benchmark = load_benchmark(args.benchmark)
    except BenchmarkError as exc:
        print(f"benchmark rejected: {exc}")
        return 0
    if args.limit is None and not args.yes:
        print(f"{len(benchmark.items)} item(s) would each call /ask, a billed Azure OpenAI "
              "endpoint. Pass --limit N to answer the first N, or --yes for all of them; "
              "the expected spend is printed before the first call.")
        return 0
    if args.limit is not None and args.limit < 1:
        print("--limit must be at least 1")
        return 0
    asyncio.run(_run(args, benchmark, transport))
    return 0


if __name__ == "__main__":
    sys.exit(main())
