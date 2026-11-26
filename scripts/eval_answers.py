"""Measure answer quality: run the benchmark against an answering endpoint.

The retrieval/agent counterpart of `score_pipeline.py`. Three commands:

    # (Re)generate the fact_lookup items from docs/groundtruth/ -- never typed.
    uv run --directory backend python ../scripts/eval_answers.py generate

    # Run them against a live FDI stack's /ask, as the dedicated eval Invitee.
    FDI_EVAL_EMAIL=eval@... FDI_EVAL_PASSWORD=... \\
    uv run --directory backend python ../scripts/eval_answers.py run \\
        --endpoint http://localhost:9514 --limit 5 --out-dir ../eval-runs

    # What changed between two runs, item by item (fixed / regressed /
    # unchanged by task completion), per kind. Refuses reports of different
    # benchmark versions or modes unless --force.
    uv run --directory backend python ../scripts/eval_answers.py diff \\
        OLD/report.json NEW/report.json [--force] [--out diff.md]

The eval Invitee is an ordinary account created with the admin CLI; using a
dedicated one keeps eval usage separable from real Invitees' and subject to
the same rate limits.

`run` talks to the stack only through its public HTTP API, one item at a
time (the per-Invitee concurrency limit), and calls a billed endpoint, so it
refuses to start without `--limit N` or `--yes`. A failed or timed-out item
is recorded with its reason and the run continues.

COST. Each item's wall-clock latency (submit to answer, so it includes up
to one poll interval of waiting, and a failed item's time to fail) is
measured here; its tokens and INR cost are read afterwards from `GET
/jobs/{id}/usage` -- the `llm_usage_event` rows that item's job billed.
Before the first billed call the run prints its expected spend: items x the
average cost of the eval Invitee's most recent succeeded `/ask` jobs (the
last `RECENT_JOBS`, read through the same endpoint). That source is live: it
prices at the stack's current configured rates and reflects today's model
and prompt, not a stale report. A first run has no history, so it has no
estimate: it then needs `--limit`, and `--yes` alone is refused.

RESUMING. `--resume <earlier report.json>` finishes a run that was cut
short: each item that earlier run answered is re-read from its job (its
answer, citations and usage; its latency as measured then) and is not
billed again; only the rest are asked, and only they enter the estimate.
The eval Invitee is signed in again whenever its access token expires.

RETRIEVAL MODE. `run --mode retrieval` spends no LLM tokens: each item with
gold evidence has its question sent to hybrid retrieval (`POST /retrieve`,
top `RETRIEVAL_K`, scoped to the item's Report when it names exactly one --
the route filters by a single `doc_key`, so a multi-Report item searches the
whole corpus), and the report gives Recall@5, Recall@10 and MRR by
(`doc_key`, page), defined in `fdi.core.retrieval_scoring`. No /ask, no
model call; the query embedding is local BGE, so nothing is billed and
neither `--limit` nor `--yes` is required (`--limit N` still caps it to the
first N scorable items). Items without gold evidence are skipped and
counted:

    uv run --directory backend python ../scripts/eval_answers.py run \\
        --mode retrieval --endpoint http://localhost:9514 --out-dir ../eval-runs

LLM JUDGE. `run --judge` (answers mode only; off by default) also asks an
Azure OpenAI judge whether each answered item's `required_claims` (the
research kind) are supported, from the evidence that answer cited -- the
results of the tool calls in its /ask trace -- and nothing else
(`fdi.core.judge`). The judge's verdicts go in their own report columns
(`judge` in report.json, "LLM judge" in report.md); every deterministic
column is exactly what it would be without the flag. An item whose /ask
failed is not sent to the judge: its claims are unsupported. The judge
reuses the stack's own client and FDI_GPT_* settings and records each call
in `llm_usage_event` under the `eval_judge` source, so this needs the
stack's environment (database included) on the machine that runs it. Its
spend is in the up-front estimate (items with claims x the average of the
latest `RECENT_JOBS` judge calls; none yet means no estimate, so `--limit`)
and in the run's total, never in cost per completed task -- that is what
an Invitee's task costs, and the judge is no part of it:

    uv run --directory backend python ../scripts/eval_answers.py run \\
        --endpoint http://localhost:9514 --limit 5 --judge --out-dir ../eval-runs

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
import uuid
from collections.abc import Awaitable, Callable, Generator, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import httpx
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fdi.core.agent import extract_citations
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
    SpendSummary,
    estimate_spend,
    summarise_cost,
)
from fdi.core.judge import (
    ItemJudgement,
    JudgeReport,
    JudgeSummary,
    judge_error,
    judge_messages,
    not_judged,
    parse_judgement,
    summarise_judgements,
)
from fdi.core.pricing import llm_cost_inr
from fdi.core.report_diff import ReportDiff, diff_reports
from fdi.core.retrieval_scoring import (
    RetrievalHit,
    RetrievalReport,
    RetrievalSummary,
    RetrievedHits,
    scorable,
    score_retrieval,
)
from fdi.pipeline.agent import AgentClient
from fdi.platform.agent import AzureOpenAIAgentClient
from fdi.platform.config import Settings
from fdi.platform.db.engine import make_engine, make_session_factory
from fdi.platform.db.repository import RegistryRepository

ROOT = Path(__file__).resolve().parents[1]
GT_DIR = ROOT / "docs" / "groundtruth"
SCHEMA = ROOT / "docs" / "architecture" / "field-extraction-schema.v4_3.json"
BENCHMARK_VERSION = "v0"
BENCHMARK_DIR = GT_DIR / "benchmark" / BENCHMARK_VERSION
# How many of the eval Invitee's latest /ask jobs the spend estimate averages.
RECENT_JOBS = 20
# Hits retrieval mode asks for: the largest K it reports recall at.
RETRIEVAL_K = 10

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
# Signing in as the eval Invitee.
# --------------------------------------------------------------------------


class EvalInviteeAuth(httpx.Auth):
    """Signs every request as the eval Invitee, and signs in again when one
    comes back 401. An access token lives 15 minutes and a full run takes
    longer: without this, every item after the expiry fails unanswered."""

    requires_response_body = True

    def __init__(self, login_url: httpx.URL, email: str, password: str) -> None:
        self._login_url = login_url
        self._credentials = {"email": email, "password": password}
        self._token: str | None = None

    def login_request(self) -> httpx.Request:
        return httpx.Request("POST", self._login_url, json=self._credentials)

    def take_token(self, response: httpx.Response) -> None:
        response.raise_for_status()
        self._token = str(response.json()["access_token"])

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response]:
        if self._token is None:
            self.take_token((yield self.login_request()))
        request.headers["Authorization"] = f"Bearer {self._token}"
        response = yield request
        if response.status_code == 401:
            self.take_token((yield self.login_request()))
            request.headers["Authorization"] = f"Bearer {self._token}"
            yield request


async def sign_in(client: httpx.AsyncClient, email: str, password: str) -> None:
    """Sign `client` in as the eval Invitee now -- so wrong credentials fail
    before the first item -- and keep it signed in for the whole run."""
    base = client.base_url
    auth = EvalInviteeAuth(base.copy_with(raw_path=base.raw_path + b"auth/login"),
                           email, password)
    auth.take_token(await client.send(auth.login_request()))
    client.auth = auth


# --------------------------------------------------------------------------
# The answering-endpoint adapter: today's /ask job flow.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class MeasuredAnswer:
    """An item's answer plus what producing it cost. `job_id` is None when no
    job was ever accepted; `cost.usage` is None when its usage could not be
    read. `evidence` is what the answer cited from, as text: each tool
    result in its trace (/ask's citations are drawn from those results), for
    the LLM judge."""

    answer: EndpointAnswer
    cost: ItemCost
    job_id: str | None
    evidence: tuple[str, ...] = ()


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

    async def login(self, email: str, password: str) -> None:
        await sign_in(self._client, email, password)

    async def measure(self, item: BenchmarkItem) -> MeasuredAnswer:
        """`answer`, timed wall-clock, then priced from the usage its job
        billed."""
        started = self._clock()
        answer, job_id, evidence = await self._attempt(item)
        latency = self._clock() - started
        usage = await self.usage(job_id) if job_id is not None else None
        return MeasuredAnswer(
            answer=answer,
            cost=ItemCost(item_id=item.id, kind=item.kind, latency_s=latency, usage=usage),
            job_id=job_id,
            evidence=evidence,
        )

    async def usage(self, job_id: str) -> ItemUsage | None:
        """What one of our jobs billed; None when the stack would not say
        (the item is then reported unpriced)."""
        try:
            r = await self._client.get(f"/jobs/{job_id}/usage")
        except httpx.HTTPError:
            return None
        if r.status_code != 200:
            return None
        body = r.json()
        return ItemUsage(input_tokens=int(body["input_tokens"]),
                         output_tokens=int(body["output_tokens"]),
                         cost_inr=float(body["total_cost_inr"]))

    async def recent_item_costs(self, jobs: int = RECENT_JOBS) -> list[float]:
        """The INR cost of each of our latest `jobs` succeeded /ask jobs that
        billed anything -- the history the spend estimate averages."""
        r = await self._client.get("/jobs", params={"kind": "ask", "limit": jobs})
        r.raise_for_status()
        costs = []
        for job in r.json()["jobs"]:
            if job.get("status") != "succeeded":
                continue
            usage = await self.usage(job["job_id"])
            if usage is not None and (usage.input_tokens or usage.output_tokens):
                costs.append(usage.cost_inr)
        return costs

    async def answer(self, item: BenchmarkItem) -> EndpointAnswer:
        answer, _job_id, _evidence = await self._attempt(item)
        return answer

    async def recall(self, item: BenchmarkItem, job_id: str, latency_s: float) -> MeasuredAnswer:
        """An item an earlier run already answered, re-read from its job --
        nothing billed again. `latency_s` is what that run measured."""

        def failed(error: str) -> EndpointAnswer:
            return EndpointAnswer(item_id=item.id, answer=None, error=error)

        try:
            answer, evidence = await self._await_answer(item, job_id, failed)
        except httpx.HTTPError as exc:
            answer, evidence = failed(f"{type(exc).__name__}: {exc}"), ()
        usage = await self.usage(job_id)
        return MeasuredAnswer(
            answer=answer,
            cost=ItemCost(item_id=item.id, kind=item.kind, latency_s=latency_s, usage=usage),
            job_id=job_id,
            evidence=evidence,
        )

    async def _attempt(
        self, item: BenchmarkItem
    ) -> tuple[EndpointAnswer, str | None, tuple[str, ...]]:
        """The answer, the job that produced it (None when /ask accepted no
        job), and the evidence it cited. Never raises."""

        def failed(error: str) -> EndpointAnswer:
            return EndpointAnswer(item_id=item.id, answer=None, error=error)

        def http_failure(exc: httpx.HTTPError) -> EndpointAnswer:
            return failed(f"{type(exc).__name__}: {exc}")

        try:
            submitted = await self._client.post("/ask", json={"question": item.question})
        except httpx.HTTPError as exc:
            return http_failure(exc), None, ()
        if submitted.status_code != 202:
            return failed(f"POST /ask returned {submitted.status_code}: {submitted.text}"), None, ()
        job_id = str(submitted.json()["job_id"])
        try:
            answer, evidence = await self._await_answer(item, job_id, failed)
        except httpx.HTTPError as exc:
            answer, evidence = http_failure(exc), ()
        return answer, job_id, evidence

    async def _await_answer(
        self,
        item: BenchmarkItem,
        job_id: str,
        failed: Callable[[str], EndpointAnswer],
    ) -> tuple[EndpointAnswer, tuple[str, ...]]:
        deadline = self._clock() + self._timeout
        while True:
            polled = await self._client.get(f"/jobs/{job_id}")
            if polled.status_code != 200:
                return failed(f"GET /jobs/{job_id} returned {polled.status_code}"), ()
            job = polled.json()
            if job["status"] == "failed":
                return failed(f"job {job_id} failed: {job.get('error')}"), ()
            if job["status"] == "succeeded":
                break
            if self._clock() >= deadline:
                return failed(
                    f"timed out after {self._timeout:g}s; job {job_id} still {job['status']}"
                ), ()
            await self._sleep(self._poll_interval)

        result = await self._client.get(f"/jobs/{job_id}/result")
        if result.status_code != 200:
            return failed(f"GET /jobs/{job_id}/result returned {result.status_code}"), ()
        body = result.json()
        answer = body.get("answer")
        if not isinstance(answer, str):
            return failed(f"job {job_id} result carries no answer text"), ()
        raw = body.get("citations")
        return EndpointAnswer(
            item_id=item.id, answer=answer,
            citations=tuple(_citation(c) for c in raw if isinstance(c, Mapping))
            if isinstance(raw, list) else (),
            tool_trace=_tool_names(body.get("tool_trace")),
        ), _tool_results(body.get("tool_trace"))


def _tool_names(raw: object) -> tuple[str, ...]:
    """The tools an `/ask` tool trace (AskToolTraceEntryView entries) called,
    in call order, repeats kept. An entry with no string `tool` is dropped."""
    if not isinstance(raw, list):
        return ()
    return tuple(
        e["tool"] for e in raw if isinstance(e, Mapping) and isinstance(e.get("tool"), str)
    )


def _tool_results(raw: object) -> tuple[str, ...]:
    """The CITED evidence of an `/ask` answer, as JSON text: each tool result
    in its trace that yields at least one citation, by the same
    `extract_citations` /ask builds its citations with. A result that cites
    nothing -- an empty search, a failed call -- was retrieved, not cited,
    and the judge must not credit a claim to it."""
    if not isinstance(raw, list):
        return ()
    return tuple(
        json.dumps(e["result"], ensure_ascii=False, default=str) for e in raw
        if isinstance(e, Mapping) and isinstance(e.get("tool"), str)
        and extract_citations(str(e["tool"]), _arguments(e), e.get("result"))
    )


def _arguments(entry: Mapping[str, object]) -> Mapping[str, object]:
    arguments = entry.get("arguments")
    return arguments if isinstance(arguments, Mapping) else {}


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
# Retrieval mode: hybrid retrieval only, no answer, no model.
# --------------------------------------------------------------------------


class RetrieveAdapter:
    """Sends a benchmark item's question to `POST /retrieve` and normalises
    the ranked hits into `RetrievedHits`. Like `AskJobAdapter`, a failure
    becomes a `RetrievedHits` with an `error`, never an exception."""

    def __init__(self, client: httpx.AsyncClient, *, k: int = RETRIEVAL_K) -> None:
        self._client = client
        self._k = k

    async def login(self, email: str, password: str) -> None:
        await sign_in(self._client, email, password)

    async def retrieve(self, item: BenchmarkItem) -> RetrievedHits:
        body: dict[str, object] = {"query": item.question, "k": self._k}
        # The route filters by ONE doc_key; an item spanning several Reports
        # is searched corpus-wide rather than favouring one of them.
        if len(item.doc_keys) == 1:
            body["filters"] = {"doc_key": item.doc_keys[0]}
        try:
            r = await self._client.post("/retrieve", json=body)
        except httpx.HTTPError as exc:
            return RetrievedHits(item_id=item.id, error=f"{type(exc).__name__}: {exc}")
        if r.status_code != 200:
            return RetrievedHits(item_id=item.id,
                                 error=f"POST /retrieve returned {r.status_code}: {r.text}")
        raw = r.json().get("results")
        if not isinstance(raw, list):
            return RetrievedHits(item_id=item.id, error="POST /retrieve returned no results list")
        return RetrievedHits(
            item_id=item.id,
            hits=tuple(_retrieval_hit(h) for h in raw if isinstance(h, Mapping)),
        )


def _retrieval_hit(raw: Mapping[str, object]) -> RetrievalHit:
    """One HybridSearchResultView's location. A value of the wrong type is
    dropped, never coerced, exactly as for a citation."""
    doc_key, page = raw.get("doc_key"), raw.get("page")
    return RetrievalHit(
        doc_key=doc_key if isinstance(doc_key, str) else None,
        page=page if isinstance(page, int) and not isinstance(page, bool) else None,
    )


# --------------------------------------------------------------------------
# The optional LLM judge.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class JudgedItem:
    """One judge call's verdicts and what it billed (None when it billed
    nothing readable, e.g. the call itself failed)."""

    judgement: ItemJudgement
    usage: ItemUsage | None


class Judge(Protocol):
    """Judges a research answer's required claims. `name` stamps the
    report; `recent_costs` is the INR cost of the latest judge calls, for the
    spend estimate."""

    name: str

    async def recent_costs(self) -> list[float]: ...

    async def judge(
        self, item: BenchmarkItem, answer: str, evidence: Sequence[str]
    ) -> JudgedItem: ...


class AzureJudge:
    """The judge on the stack's own Azure OpenAI client and FDI_GPT_*
    settings. Every call is recorded in `llm_usage_event` under `eval_judge`,
    correlated by this run's id, and priced at the configured rates. A
    failed call is a judge error, never an exception."""

    SOURCE = "eval_judge"

    def __init__(
        self,
        settings: Settings,
        client: AgentClient,
        run_id: str,
        sessions: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        """`sessions` defaults to the stack's own database."""
        self._settings = settings
        self._client = client
        self._run_id = run_id
        self._sessions = sessions or make_session_factory(make_engine(settings.database_url))
        self.name = f"azure-openai:{settings.gpt_deployment}"

    @staticmethod
    def from_environment(run_id: str) -> AzureJudge | str:
        """The judge, or why it cannot be built here."""
        try:
            settings = Settings()
        except ValidationError as exc:
            return f"the stack's settings do not load here: {exc.error_count()} problem(s)"
        client = AzureOpenAIAgentClient.from_settings(settings)
        if client is None:
            return "FDI_GPT_ENDPOINT / FDI_GPT_KEY are not set"
        return AzureJudge(settings, client, run_id)

    def _price(self, input_tokens: int, output_tokens: int) -> float:
        s = self._settings
        return llm_cost_inr(
            input_tokens, output_tokens, input_usd_per_mtok=s.llm_input_cost_per_mtok_usd,
            output_usd_per_mtok=s.llm_output_cost_per_mtok_usd, usd_to_inr=s.usd_to_inr_rate,
        )

    async def recent_costs(self) -> list[float]:
        async with self._sessions() as session:
            recent = await RegistryRepository(session).recent_llm_usage(
                self.SOURCE, limit=RECENT_JOBS
            )
        return [self._price(i, o) for i, o in recent]

    async def judge(
        self, item: BenchmarkItem, answer: str, evidence: Sequence[str]
    ) -> JudgedItem:
        try:
            response = await asyncio.to_thread(
                self._client.complete, judge_messages(item, answer, evidence), []
            )
        except Exception as exc:  # noqa: BLE001 -- any judge failure is a judge error
            return JudgedItem(
                judge_error(item, f"judge call failed: {type(exc).__name__}: {exc}"), usage=None
            )
        usage = response.usage
        priced = None
        if usage is not None:
            async with self._sessions() as session:
                await RegistryRepository(session).record_llm_usage(
                    str(uuid.uuid4()), doc_id=None, stage_run_id=None, source=self.SOURCE,
                    correlation_id=self._run_id, model=usage.model,
                    input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
                    duration_ms=usage.duration_ms,
                )
                await session.commit()
            priced = ItemUsage(input_tokens=usage.input_tokens,
                               output_tokens=usage.output_tokens,
                               cost_inr=self._price(usage.input_tokens, usage.output_tokens))
        return JudgedItem(parse_judgement(item, response.content or ""), usage=priced)


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


def load_resumable(report_path: Path) -> dict[str, tuple[str, float]]:
    """From an earlier run's report.json: every item that got an answer
    (status not "failed") -> (its job id, its measured latency)."""
    body = json.loads(report_path.read_text(encoding="utf-8"))
    return {
        i["item_id"]: (str(i["job_id"]), float(i["latency_s"]))
        for i in body["items"]
        if i.get("status") != "failed" and i.get("job_id") and i.get("latency_s") is not None
    }


async def run_eval(
    benchmark: Benchmark,
    adapter: AskJobAdapter,
    *,
    limit: int | None,
    stamp: Mapping[str, object],
    out_dir: Path,
    judge: Judge | None = None,
    resume: Mapping[str, tuple[str, float]] | None = None,
) -> EvalReport:
    """Answer the first `limit` items (all when None) one at a time, score
    them, have `judge` (when given) judge those with required claims, and
    write `report.json` + `report.md` into `out_dir`. An item in `resume`
    (item id -> its earlier job id and latency) is re-read from that job,
    not asked again."""
    items = benchmark.items if limit is None else benchmark.items[:limit]
    resume = resume or {}
    measured: list[MeasuredAnswer] = []
    judgements: list[ItemJudgement] = []
    for n, item in enumerate(items, start=1):
        if item.id in resume:
            m = await adapter.recall(item, *resume[item.id])
            outcome = "error" if m.answer.error else "reused"
        else:
            m = await adapter.measure(item)
            outcome = "error" if m.answer.error else "answered"
        print(f"[{n}/{len(items)}] {item.id}: {outcome}")
        if judge is not None and item.gold.required_claims:
            m, judgement = await _judge_item(judge, item, m)
            judgements.append(judgement)
        measured.append(m)
    report = score_run(benchmark, [m.answer for m in measured])
    completed = {i.item_id for i in report.items if i.completed}
    cost = summarise_cost([m.cost for m in measured], completed)
    judged = None if judge is None else summarise_judgements(benchmark, judgements)
    write_reports(out_dir, report, stamp, {m.answer.item_id: m for m in measured}, cost, judged)
    return report


async def _judge_item(
    judge: Judge, item: BenchmarkItem, m: MeasuredAnswer
) -> tuple[MeasuredAnswer, ItemJudgement]:
    """An answer that never came is not sent to the judge: nothing to pay for."""
    if m.answer.error is not None or m.answer.answer is None:
        return m, not_judged(item, m.answer.error or "no answer returned")
    judged = await judge.judge(item, m.answer.answer, m.evidence)
    return replace(m, cost=replace(m.cost, judge_usage=judged.usage)), judged.judgement


def write_reports(
    out_dir: Path,
    report: EvalReport,
    stamp: Mapping[str, object],
    measured: Mapping[str, MeasuredAnswer],
    cost: CostSummary,
    judge: JudgeReport | None = None,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(
        dump(report_json(report, stamp, measured, cost, judge)), encoding="utf-8")
    (out_dir / "report.md").write_text(
        render_markdown(report, stamp, measured, cost, judge), encoding="utf-8")


def _item_cost_fields(m: MeasuredAnswer | None) -> dict[str, object]:
    """An item's latency/usage columns; usage fields are None when unread."""
    usage = m.cost.usage if m else None
    return {
        "job_id": m.job_id if m else None,
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
    judge: JudgeReport | None = None,
) -> dict[str, object]:
    """`judge` adds the judge columns -- a top-level `judge` summary and a
    per-item `judge` -- and only them: no deterministic field changes."""

    def summary(s: KindSummary) -> dict[str, object]:
        return {
            **asdict(s), "accuracy": s.accuracy, "citation_validity": s.citation_validity,
            "evidence_support_rate": s.evidence_support_rate,
            "completion_rate": s.completion_rate,
            "tool_selection_accuracy": s.tool_selection_accuracy,
        }

    def answer(item_id: str) -> str | None:
        m = measured.get(item_id)
        return m.answer.answer if m else None

    def tools_called(item_id: str) -> list[str]:
        m = measured.get(item_id)
        return list(m.answer.tool_trace) if m else []

    judgements = {j.item_id: j for j in judge.items} if judge else {}

    def judge_fields(item_id: str) -> dict[str, object]:
        j = judgements.get(item_id)
        if j is None:
            return {}
        m = measured.get(item_id)
        usage = m.cost.judge_usage if m else None
        return {"judge": asdict(j), "judge_cost_inr": usage.cost_inr if usage else None}

    body: dict[str, object] = {
        "stamp": dict(stamp),
        "benchmark_size": report.benchmark_size,
        "overall": summary(report.overall),
        "by_kind": {kind: summary(s) for kind, s in report.by_kind.items()},
        "cost": {**asdict(cost), "total_cost_inr": cost.total_cost_inr},
        "items": [
            {**asdict(i), "citation_validity": i.citation_validity,
             "answer": answer(i.item_id), "tools_called": tools_called(i.item_id),
             **_item_cost_fields(measured.get(i.item_id)), **judge_fields(i.item_id)}
            for i in report.items
        ],
    }
    if judge is not None:
        body["judge"] = {
            "overall": _judge_summary_json(judge.overall),
            "by_kind": {kind: _judge_summary_json(s) for kind, s in judge.by_kind.items()},
        }
    return body


def _judge_summary_json(s: JudgeSummary) -> dict[str, object]:
    return {**asdict(s), "claim_support_rate": s.claim_support_rate,
            "full_support_rate": s.full_support_rate}


def _seconds(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}"


def _inr(value: float | None) -> str:
    return "-" if value is None else f"INR {value:.4f}"


def render_cost_markdown(
    report: EvalReport,
    measured: Mapping[str, MeasuredAnswer],
    cost: CostSummary,
    judged: bool = False,
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
    if judged:
        lines += [
            f"- **judge**: {_inr(cost.judge_cost_inr)} ({cost.judge_input_tokens} input + "
            f"{cost.judge_output_tokens} output tokens; eval spend, not in cost per "
            "completed task)",
            f"- **total**: {_inr(cost.total_cost_inr)}",
        ]
    if cost.items_without_usage:
        lines.append(f"- **items whose usage could not be read** (not in the cost): "
                     f"{cost.items_without_usage}")
    lines += ["", "Latency is submit to answer, failed items included; it can run up to one "
              "poll interval over the job's own time.",
              "", "| kind | items | P50 latency (s) | P95 latency (s) |", "|---|---:|---:|---:|"]
    lines += [latency_row(kind, s) for kind, s in cost.latency_by_kind.items()]
    lines.append(latency_row("**overall**", cost.latency_overall))

    def spend_row(kind: str, s: SpendSummary | CostSummary) -> str:
        return (f"| {kind} | {s.input_tokens} | {s.output_tokens} | {_inr(s.cost_inr)} "
                f"| {s.completed} | {_inr(s.cost_per_completed_task_inr)} |")

    lines += ["", "| kind | input tokens | output tokens | cost | completed "
              "| cost per completed task |", "|---|---:|---:|---:|---:|---:|"]
    lines += [spend_row(kind, s) for kind, s in cost.spend_by_kind.items()]
    lines.append(spend_row("**overall**", cost))
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


def render_tool_selection_markdown(
    report: EvalReport, measured: Mapping[str, MeasuredAnswer]
) -> list[str]:
    """Nothing when no item run names expected tools."""
    if not report.overall.tool_selection_items:
        return []

    def pct(value: float | None) -> str:
        return "-" if value is None else f"{value:.1%}"

    lines = [
        "", "## Tool selection", "",
        "Share of an item's expected tools that /ask called (order, repeats and extra "
        "tools ignored), averaged over the items that name expected tools; `-` where "
        "none do.",
        "", "| kind | items | tool selection |", "|---|---:|---:|",
    ]
    rows = [*report.by_kind.items(), ("**overall**", report.overall)]
    lines += [f"| {kind} | {s.tool_selection_items} | {pct(s.tool_selection_accuracy)} |"
              for kind, s in rows]
    lines += ["", "| item | tool selection | missing | called |", "|---|---:|---|---|"]
    for i in report.items:
        if i.tool_selection is None:
            continue
        m = measured.get(i.item_id)
        called = ", ".join(m.answer.tool_trace) if m and m.answer.tool_trace else "-"
        lines.append(f"| `{i.item_id}` | {pct(i.tool_selection)} "
                     f"| {', '.join(i.missing_tools) or '-'} | {called} |")
    return lines


def spend_line(estimate: SpendEstimate) -> str:
    e = estimate
    if e.average_cost_inr is None:
        return (f"{e.items} item(s) will call /ask, a billed endpoint; no recent /ask "
                "usage to estimate from (a first run).")
    ask_total = e.items * e.average_cost_inr
    ask = (f"{e.items} item(s) x INR {e.average_cost_inr:.4f}, the average of "
           f"{e.sample_size} recent /ask job(s)")
    if not e.judge_items:
        return f"expected spend: INR {ask_total:.2f} = {ask}."
    if e.total_cost_inr is None or e.judge_average_cost_inr is None:
        return (f"/ask: INR {ask_total:.2f} = {ask}; {e.judge_items} item(s) will also call "
                "the LLM judge, with no recent judge calls to estimate it from (a first "
                "judged run).")
    return (f"expected spend: INR {e.total_cost_inr:.2f} -- /ask INR {ask_total:.2f} = "
            f"{ask}; judge INR {e.judge_items * e.judge_average_cost_inr:.2f} = "
            f"{e.judge_items} item(s) x INR {e.judge_average_cost_inr:.4f}, the average of "
            f"{e.judge_sample_size} recent judge call(s).")


def render_markdown(
    report: EvalReport,
    stamp: Mapping[str, object],
    measured: Mapping[str, MeasuredAnswer],
    cost: CostSummary,
    judge: JudgeReport | None = None,
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
        "evidence page. Only citations carrying a `doc_key` and page can be valid; "
        "today's /ask table and concept citations carry neither, so they never count. "
        "An unanswerable item has no gold evidence: it completes when it asserts no "
        "figure and says the information was not found, and its support is always 0.",
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
    lines += render_tool_selection_markdown(report, measured)
    if judge is not None:
        lines += render_judge_markdown(judge)
    lines += render_cost_markdown(report, measured, cost, judged=judge is not None)
    return "\n".join(lines) + "\n"


def render_judge_markdown(judge: JudgeReport) -> list[str]:
    def pct(value: float | None) -> str:
        return "-" if value is None else f"{value:.1%}"

    lines = [
        "", "## LLM judge", "",
        "An LLM's opinion, not a score: whether each required claim is made by the answer "
        "AND supported by the evidence it cited, judged from that evidence alone. Nothing "
        "above reads it. A judge error is the judge's failure, left out of the rates; an "
        "item that never answered supports no claim.",
        "", "| kind | items | judge errors | claims | supported | claim support "
        "| fully supported | full support |", "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    rows = [*judge.by_kind.items(), ("**overall**", judge.overall)]
    lines += [
        f"| {kind} | {s.items} | {s.judge_errors} | {s.claims} | {s.supported_claims} "
        f"| {pct(s.claim_support_rate)} | {s.fully_supported} | {pct(s.full_support_rate)} |"
        for kind, s in rows
    ]
    shortfalls: list[tuple[str, str, str]] = []
    for j in judge.items:
        if j.error is not None:
            shortfalls.append((j.item_id, "(not judged)", j.error))
        shortfalls += [(j.item_id, v.claim, v.reason) for v in j.verdicts if not v.supported]
    if shortfalls:
        lines += ["", "| item | unsupported claim | why |", "|---|---|---|"]
        lines += [f"| `{item_id}` | {claim.replace('|', '/')} | {why.replace('|', '/')} |"
                  for item_id, claim, why in shortfalls]
    return lines


async def run_retrieval_eval(
    benchmark: Benchmark,
    adapter: RetrieveAdapter,
    *,
    limit: int | None,
    stamp: Mapping[str, object],
    out_dir: Path,
) -> RetrievalReport:
    """Retrieve for the first `limit` SCORABLE items (all when None) one at a
    time, score them, and write `report.json` + `report.md` into `out_dir`."""
    scorable_items = [item for item in benchmark.items if scorable(item)]
    items = scorable_items if limit is None else scorable_items[:limit]
    retrieved: list[RetrievedHits] = []
    for n, item in enumerate(items, start=1):
        r = await adapter.retrieve(item)
        print(f"[{n}/{len(items)}] {item.id}: {'error' if r.error else f'{len(r.hits)} hit(s)'}")
        retrieved.append(r)
    report = score_retrieval(benchmark, retrieved)
    write_retrieval_reports(out_dir, report, stamp)
    return report


def write_retrieval_reports(
    out_dir: Path, report: RetrievalReport, stamp: Mapping[str, object]
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(dump(retrieval_report_json(report, stamp)),
                                         encoding="utf-8")
    (out_dir / "report.md").write_text(render_retrieval_markdown(report, stamp),
                                       encoding="utf-8")


def retrieval_report_json(
    report: RetrievalReport, stamp: Mapping[str, object]
) -> dict[str, object]:
    def summary(s: RetrievalSummary) -> dict[str, object]:
        return {**asdict(s), "recall_at_5": s.recall_at_5, "recall_at_10": s.recall_at_10,
                "mrr": s.mrr}

    return {
        "stamp": dict(stamp),
        "benchmark_size": report.benchmark_size,
        "skipped": report.skipped,
        "overall": summary(report.overall),
        "by_kind": {kind: summary(s) for kind, s in report.by_kind.items()},
        "items": [
            {**asdict(i), "recalled_at_5": i.recalled_at_5,
             "recalled_at_10": i.recalled_at_10, "reciprocal_rank": i.reciprocal_rank}
            for i in report.items
        ],
    }


def render_retrieval_markdown(report: RetrievalReport, stamp: Mapping[str, object]) -> str:
    lines = ["# Retrieval eval", ""]
    lines += [f"- **{key}**: `{value}`" for key, value in stamp.items()]
    lines += [
        f"- **items run**: {report.overall.run} of {report.benchmark_size}",
        f"- **skipped (no gold evidence)**: {report.skipped}",
        "",
        "Recall@K = items with at least one gold (doc_key, page) in the top K hits, "
        "over items run (a multi-Report item needs a gold page of every Report); "
        "MRR = mean 1/rank of the first gold hit, 0 when absent. "
        "A failed retrieval call counts against every column.",
        "",
        "| kind | run | failed | recall@5 | recall@10 | MRR |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    rows = [*report.by_kind.items(), ("**overall**", report.overall)]
    lines += [
        f"| {kind} | {s.run} | {s.failed} | {s.recall_at_5:.1%} | {s.recall_at_10:.1%} "
        f"| {s.mrr:.3f} |"
        for kind, s in rows
    ]
    failed = [i for i in report.items if i.failed]
    if failed:
        lines += ["", "## Failed", "", "| item | reason |", "|---|---|"]
        lines += [f"| `{i.item_id}` | {(i.error or '').replace('|', '/')} |" for i in failed]
    missed = [i for i in report.items if not i.failed and not i.recalled_at_10]
    if missed:
        lines += ["", "## No gold page in the top 10", "", "| item | hits |", "|---|---:|"]
        lines += [f"| `{i.item_id}` | {i.hits} |" for i in missed]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# diff
# --------------------------------------------------------------------------


def render_diff_markdown(diff: ReportDiff, old_name: str, new_name: str) -> str:
    def side(name: str, version: str | None, mode: str | None) -> str:
        return f"`{name}` (benchmark `{version}`, mode `{mode}`)"

    lines = [
        "# Answer-quality report diff", "",
        f"- **old**: {side(old_name, diff.old_version, diff.old_mode)}",
        f"- **new**: {side(new_name, diff.new_version, diff.new_mode)}",
    ]
    lines += [f"- **forced past**: {m}" for m in diff.mismatches]
    lines += [
        "",
        "Fixed / regressed = task completion (correct AND a valid citation) gained / lost; "
        "an item whose completion did not change is unchanged, whatever its status did.",
        "", "| kind | fixed | regressed | unchanged | only old | only new | net |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    rows = [*diff.by_kind.items(), ("**overall**", diff.overall)]
    lines += [
        f"| {kind} | {s.fixed} | {s.regressed} | {s.unchanged} | {s.only_old} "
        f"| {s.only_new} | {s.net:+d} |"
        for kind, s in rows
    ]
    for change, title in (("fixed", "Fixed"), ("regressed", "Regressed"),
                          ("only_old", "Only in old"), ("only_new", "Only in new")):
        changed = [c for c in diff.items if c.change == change]
        if not changed:
            continue
        lines += ["", f"## {title}", "",
                  "| item | kind | old status | new status |", "|---|---|---|---|"]
        lines += [f"| `{c.item_id}` | {c.kind} | {c.old_status or '-'} | {c.new_status or '-'} |"
                  for c in changed]
    return "\n".join(lines) + "\n"


def _diff(args: argparse.Namespace) -> None:
    try:
        old, new = (json.loads(Path(p).read_text(encoding="utf-8")) for p in (args.old, args.new))
    except (OSError, ValueError) as exc:
        print(f"cannot read the reports: {exc}")
        return
    diff = diff_reports(old, new)
    if diff.problems:
        print(f"cannot read the reports: {'; '.join(diff.problems)}")
        return
    if diff.mismatches and not args.force:
        print(f"cannot compare: {'; '.join(diff.mismatches)} (pass --force to compare anyway)")
        return
    text = render_diff_markdown(diff, str(args.old), str(args.new))
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
    print(text, end="")


def git_commit() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True
        )
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return out.stdout.strip()


def take_eval_credentials(args: argparse.Namespace) -> tuple[str, str] | None:
    """The eval Invitee's email and password, from the flags or FDI_EVAL_*.

    FDI_EVAL_* are taken out of this process's environment as they are read:
    they are this script's, not the stack's, and `Settings` -- which --judge
    builds -- rejects every FDI_ variable it does not know."""
    email = args.email or os.environ.get("FDI_EVAL_EMAIL")
    password = args.password or os.environ.get("FDI_EVAL_PASSWORD")
    os.environ.pop("FDI_EVAL_EMAIL", None)
    os.environ.pop("FDI_EVAL_PASSWORD", None)
    if not email or not password:
        return None
    return email, password


async def _run(
    args: argparse.Namespace,
    benchmark: Benchmark,
    transport: httpx.AsyncBaseTransport | None = None,
    judge: Judge | None = None,
) -> None:
    credentials = take_eval_credentials(args)
    if credentials is None:
        print("eval Invitee credentials missing: pass --email/--password or set "
              "FDI_EVAL_EMAIL/FDI_EVAL_PASSWORD")
        return
    email, password = credentials
    stamp = {
        "benchmark_version": benchmark.version,
        "git_commit": git_commit(),
        "endpoint": args.endpoint,
        "mode": args.mode,
        "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    if args.mode == "retrieval":
        await _run_retrieval(args, benchmark, stamp, email, password, transport)
        return
    # Off unless asked for, whatever a caller injected.
    if not args.judge:
        judge = None
    elif judge is None:
        built = AzureJudge.from_environment(run_id=str(uuid.uuid4()))
        if isinstance(built, str):
            print(f"cannot start the LLM judge: {built}")
            return
        judge = built
    if judge is not None:
        stamp["judge"] = judge.name
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
        to_run = benchmark.items if args.limit is None else benchmark.items[: args.limit]
        resume = load_resumable(args.resume) if args.resume else {}
        if args.resume:
            stamp["resumed"] = {"from": str(args.resume),
                                "items": sum(1 for i in to_run if i.id in resume)}
        to_ask = [i for i in to_run if i.id not in resume]
        judged_items = sum(1 for i in to_run if i.gold.required_claims) if judge else 0
        recent_judge = await judge.recent_costs() if judge and judged_items else []
        # Printed before the first billed call, which run_eval makes.
        estimate = estimate_spend(len(to_ask), recent, judged_items=judged_items,
                                  recent_judge_costs=recent_judge)
        print(spend_line(estimate))
        if estimate.total_cost_inr is None and args.limit is None:
            print("--yes needs an expected spend, and there is none yet: run a small "
                  "--limit first to seed one.")
            return
        report = await run_eval(
            benchmark, adapter, limit=args.limit, stamp=stamp, out_dir=args.out_dir,
            judge=judge, resume=resume,
        )
    o = report.overall
    print(f"{o.correct}/{o.run} correct ({o.accuracy:.1%}), {o.failed} failed; "
          f"reports in {args.out_dir}")


async def _run_retrieval(
    args: argparse.Namespace,
    benchmark: Benchmark,
    stamp: Mapping[str, object],
    email: str,
    password: str,
    transport: httpx.AsyncBaseTransport | None,
) -> None:
    """Retrieval mode: nothing billed, so no spend estimate and no gate."""
    async with httpx.AsyncClient(
        base_url=args.endpoint, timeout=60.0, transport=transport
    ) as client:
        adapter = RetrieveAdapter(client)
        try:
            await adapter.login(email, password)
        except httpx.HTTPError as exc:
            print(f"could not reach the stack as the eval Invitee: {exc}")
            return
        report = await run_retrieval_eval(
            benchmark, adapter, limit=args.limit, stamp=stamp, out_dir=args.out_dir
        )
    o = report.overall
    print(f"recall@5 {o.recall_at_5:.1%}, recall@10 {o.recall_at_10:.1%}, MRR {o.mrr:.3f} "
          f"over {o.run} item(s), {o.failed} failed, {report.skipped} skipped (no gold "
          f"evidence); reports in {args.out_dir}")


def main(
    argv: Sequence[str] | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    judge: Judge | None = None,
) -> int:
    """`transport` replaces the network and `judge` the Azure judge `--judge`
    turns on, for tests; None is the real one."""
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate", help="regenerate fact_lookup items from ground truth")
    gen.add_argument("--out", type=Path, default=BENCHMARK_DIR / "fact_lookup.json")

    run = sub.add_parser("run", help="run the benchmark against /ask (or /retrieve)")
    run.add_argument("--benchmark", type=Path, default=BENCHMARK_DIR)
    run.add_argument("--endpoint", required=True, help="the FDI API base URL")
    run.add_argument("--out-dir", required=True, type=Path)
    run.add_argument("--email", help="eval Invitee email (default: $FDI_EVAL_EMAIL)")
    run.add_argument("--password", help="eval Invitee password (default: $FDI_EVAL_PASSWORD)")
    run.add_argument("--limit", type=int, help="answer only the first N items")
    run.add_argument("--yes", action="store_true", help="answer every item (billed)")
    run.add_argument("--resume", type=Path,
                     help="an earlier report.json: re-read the items it answered from "
                          "their jobs (not billed again) and ask only the rest")
    run.add_argument("--timeout", type=float, default=300.0, help="seconds per item")
    run.add_argument("--poll-interval", type=float, default=2.0)
    run.add_argument("--mode", choices=("answers", "retrieval"), default="answers",
                     help="answers: /ask (billed); retrieval: POST /retrieve only, "
                          "Recall@K and MRR, no model call")
    run.add_argument("--judge", action="store_true",
                     help="also have an LLM judge score required claims (billed; separate "
                          "report columns)")

    diff = sub.add_parser("diff", help="compare two report.json files item by item")
    diff.add_argument("old", type=Path, help="the earlier report.json")
    diff.add_argument("new", type=Path, help="the later report.json")
    diff.add_argument("--force", action="store_true",
                      help="compare even across benchmark versions or modes")
    diff.add_argument("--out", type=Path, help="also write the markdown here")
    args = parser.parse_args(argv)

    if args.command == "generate":
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(dump(generate_fact_lookup()), encoding="utf-8")
        print(f"wrote {args.out}")
        return 0
    if args.command == "diff":
        _diff(args)
        return 0

    if args.judge and args.mode != "answers":
        print("--judge scores answers; retrieval mode has none to judge")
        return 0
    try:
        benchmark = load_benchmark(args.benchmark)
    except BenchmarkError as exc:
        print(f"benchmark rejected: {exc}")
        return 0
    if args.mode == "answers" and args.limit is None and not args.yes:
        print(f"{len(benchmark.items)} item(s) would each call /ask, a billed Azure OpenAI "
              "endpoint. Pass --limit N to answer the first N, or --yes for all of them; "
              "the expected spend is printed before the first call.")
        return 0
    if args.limit is not None and args.limit < 1:
        print("--limit must be at least 1")
        return 0
    asyncio.run(_run(args, benchmark, transport, judge))
    return 0


if __name__ == "__main__":
    sys.exit(main())
