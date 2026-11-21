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
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import httpx

from fdi.core.answer_scoring import EndpointAnswer, EvalReport, KindSummary, score_run
from fdi.core.benchmark import (
    Benchmark,
    BenchmarkError,
    BenchmarkItem,
    fact_lookup_benchmark,
    parse_benchmark,
)

ROOT = Path(__file__).resolve().parents[1]
GT_DIR = ROOT / "docs" / "groundtruth"
SCHEMA = ROOT / "docs" / "architecture" / "field-extraction-schema.v4_3.json"
BENCHMARK_VERSION = "v0"
BENCHMARK_DIR = GT_DIR / "benchmark" / BENCHMARK_VERSION

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


class AskJobAdapter:
    """Answers a benchmark item through today's `/ask`: submit, poll
    `GET /jobs/{id}`, read `/jobs/{id}/result`, and normalise the response
    into an `EndpointAnswer`. Every failure becomes an EndpointAnswer with an
    `error`, never an exception, so one bad item cannot end the run.

    `sleep` is the wait between polls; it is injectable so a test can play
    the worker while the adapter waits."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        poll_interval: float = 2.0,
        timeout: float = 300.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._client = client
        self._poll_interval = poll_interval
        self._timeout = timeout
        self._sleep = sleep

    async def login(self, email: str, password: str) -> None:
        r = await self._client.post("/auth/login", json={"email": email, "password": password})
        r.raise_for_status()
        self._client.headers["Authorization"] = f"Bearer {r.json()['access_token']}"

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
        answer = result.json().get("answer")
        if not isinstance(answer, str):
            return failed(f"job {job_id} result carries no answer text")
        return EndpointAnswer(item_id=item.id, answer=answer)


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
    answers: list[EndpointAnswer] = []
    for n, item in enumerate(items, start=1):
        answer = await adapter.answer(item)
        print(f"[{n}/{len(items)}] {item.id}: {'error' if answer.error else 'answered'}")
        answers.append(answer)
    report = score_run(benchmark, answers)
    write_reports(out_dir, report, stamp, {a.item_id: a.answer for a in answers})
    return report


def write_reports(
    out_dir: Path,
    report: EvalReport,
    stamp: Mapping[str, object],
    answers: Mapping[str, str | None],
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(dump(report_json(report, stamp, answers)),
                                         encoding="utf-8")
    (out_dir / "report.md").write_text(render_markdown(report, stamp), encoding="utf-8")


def report_json(
    report: EvalReport, stamp: Mapping[str, object], answers: Mapping[str, str | None]
) -> dict[str, object]:
    def summary(s: KindSummary) -> dict[str, object]:
        return {**asdict(s), "accuracy": s.accuracy}

    return {
        "stamp": dict(stamp),
        "benchmark_size": report.benchmark_size,
        "overall": summary(report.overall),
        "by_kind": {kind: summary(s) for kind, s in report.by_kind.items()},
        "items": [{**asdict(i), "answer": answers.get(i.item_id)} for i in report.items],
    }


def render_markdown(report: EvalReport, stamp: Mapping[str, object]) -> str:
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
    misses = [i for i in report.items if i.status != "correct"]
    if misses:
        lines += ["", "## Not correct", "", "| item | status | reason |", "|---|---|---|"]
        lines += [
            f"| `{i.item_id}` | {i.status} | {i.reason.replace('|', '/')} |" for i in misses
        ]
    return "\n".join(lines) + "\n"


def git_commit() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True
        )
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return out.stdout.strip()


async def _run(args: argparse.Namespace, benchmark: Benchmark) -> None:
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
    async with httpx.AsyncClient(base_url=args.endpoint, timeout=60.0) as client:
        adapter = AskJobAdapter(client, poll_interval=args.poll_interval, timeout=args.timeout)
        try:
            await adapter.login(email, password)
        except httpx.HTTPError as exc:
            print(f"login as the eval Invitee failed: {exc}")
            return
        report = await run_eval(
            benchmark, adapter, limit=args.limit, stamp=stamp, out_dir=args.out_dir
        )
    o = report.overall
    print(f"{o.correct}/{o.run} correct ({o.accuracy:.1%}), {o.failed} failed; "
          f"reports in {args.out_dir}")


def main(argv: Sequence[str] | None = None) -> int:
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
              "endpoint. Pass --limit N to answer the first N, or --yes for all of them.")
        return 0
    if args.limit is not None and args.limit < 1:
        print("--limit must be at least 1")
        return 0
    asyncio.run(_run(args, benchmark))
    return 0


if __name__ == "__main__":
    sys.exit(main())
