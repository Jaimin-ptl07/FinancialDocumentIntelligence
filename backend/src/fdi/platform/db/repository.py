"""Registry + eval persistence. The caller owns the transaction — commit is
theirs, not ours."""

import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import TypedDict

import sqlalchemy
from sqlalchemy import Float, case, distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from fdi.core.eval_types import EvalOutcome
from fdi.core.facts import RunFacts
from fdi.platform.db.models import (
    ApiJob,
    AppUser,
    Document,
    EvalCaseResult,
    EvalRun,
    LlmUsageEvent,
    OcrUsageEvent,
    RefreshToken,
    StageRun,
)


class CorpusTotals(TypedDict):
    """Whole-corpus figures for `GET /analytics`. A TypedDict, not a bare
    `dict[str, object]`, so the route can read `totals["total_bytes"]` as an
    `int` without a cast and mypy still checks the key names.

    `chunk_count` does NOT live here (RT-9): the Postgres `chunk` table is
    gone, and this repository is pure-Postgres -- the route reads that
    figure from the injected `VectorStore` instead (`VectorStore.count()`)."""

    document_count: int
    doc_key_count: int
    total_bytes: int
    latest_ingested_at: datetime | None


class StageThroughput(TypedDict):
    """One row of the per-stage pipeline summary. `median_seconds` is None
    when the stage has no `done` runs to take a median over."""

    name: str
    done: int
    running: int
    queued: int
    failed: int
    median_seconds: float | None


class CostBySource(TypedDict):
    """One usage source's raw totals for one document (costing feature).
    `source` is "ocr" or one of the llm_usage_event.source values.
    page_count is 0 for every LLM source; input_tokens/output_tokens are 0
    for "ocr". INR conversion happens at the API layer via fdi.core.pricing
    -- this stays in raw units so a pricing change recomputes without
    touching this query."""

    source: str
    call_count: int
    page_count: int
    input_tokens: int
    output_tokens: int
    duration_ms: int


class CorpusCostTotals(TypedDict):
    """Raw usage totals, corpus-wide or per-stage (costing feature). Every
    real billable event counts -- unlike stage_throughput/document_outcome_
    counts this is NOT latest-per-stage: a document reprocessed for real
    (checksum unchanged, stage version bumped) contributes both charges,
    matching actual money spent."""

    ocr_page_count: int
    llm_input_tokens: int
    llm_output_tokens: int


class UsageEventRecord(TypedDict):
    """One real OCR or LLM call for a document (costing feature) -- the
    finest-grained record the schema keeps; document_cost_by_source's
    per-source totals are an aggregate over exactly these rows. `kind` is
    "ocr" or "llm"; `source` is "ocr" for an OCR event or the
    llm_usage_event.source value for an LLM event. page_count is 0 for
    every LLM event; input_tokens/output_tokens are 0 for an OCR event."""

    id: str
    kind: str
    source: str
    model: str
    page_count: int
    input_tokens: int
    output_tokens: int
    duration_ms: int
    stage_run_id: str | None
    correlation_id: str | None
    created_at: datetime


class DocumentCostSummary(TypedDict):
    """Raw usage totals for one document, as one row of a corpus-wide
    "which filings cost the most" listing (costing feature). Only
    documents with at least one billable OCR/LLM event appear -- /ask's
    doc_id=NULL rows belong to no single document and are excluded here
    (they still count in corpus_cost_totals/corpus_cost_by_source)."""

    doc_id: str
    doc_key: str | None
    filename: str | None
    ocr_page_count: int
    llm_input_tokens: int
    llm_output_tokens: int


def _latest_stage_runs() -> sqlalchemy.Subquery:
    """Every `stage_run` tagged with its rank within (doc_id, stage_name),
    newest first, plus its wall-clock duration in seconds.

    Callers filter on `rn == 1` to get the LATEST run per (doc_id,
    stage_name) -- the same rule as `RegistryRepository.latest_run_status_counts`,
    and for the same reason: a document whose `ocr` was retried three times
    must contribute one `ocr` to any summary, not three, or a flaky stage
    looks like a bigger pipeline than it is.

    `seconds` is cast to float8 rather than left as the `numeric` that
    `extract(epoch from interval)` yields: `percentile_cont` is declared over
    double precision, and the cast keeps the median a Python `float` instead
    of a `Decimal` that would need converting at every call site.
    """
    return select(
        StageRun.doc_id.label("doc_id"),
        StageRun.stage_name.label("stage_name"),
        StageRun.status.label("status"),
        sqlalchemy.cast(
            func.extract("epoch", StageRun.updated_at - StageRun.created_at), Float
        ).label("seconds"),
        func.row_number()
        .over(
            partition_by=(StageRun.doc_id, StageRun.stage_name),
            order_by=(StageRun.created_at.desc(), StageRun.id.desc()),
        )
        .label("rn"),
    ).subquery()


class RegistryRepository:
    """Document + stage_run persistence. The caller owns the transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def create_document(self, doc_id: str, checksum: str | None = None) -> None:
        self._s.add(Document(id=doc_id, checksum=checksum))
        await self._s.flush()

    async def get_document(self, doc_id: str) -> Document | None:
        return await self._s.get(Document, doc_id)

    async def start_run(
        self,
        run_id: str,
        doc_id: str,
        stage_name: str,
        stage_version: int,
        input_hash: str,
        correlation_id: str | None,
    ) -> None:
        self._s.add(
            StageRun(
                id=run_id,
                doc_id=doc_id,
                stage_name=stage_name,
                stage_version=stage_version,
                input_hash=input_hash,
                status="running",
                correlation_id=correlation_id,
            )
        )
        await self._s.flush()

    async def finish_run(self, run_id: str, output_hash: str, artifact_key: str) -> None:
        run = await self._s.get(StageRun, run_id)
        assert run is not None
        run.status, run.output_hash, run.artifact_key = "done", output_hash, artifact_key
        await self._s.flush()

    async def fail_run(self, run_id: str, error: str) -> None:
        run = await self._s.get(StageRun, run_id)
        assert run is not None
        run.status, run.error = "failed", error
        await self._s.flush()

    async def record_ocr_usage(
        self,
        id: str,
        *,
        doc_id: str,
        stage_run_id: str,
        correlation_id: str | None,
        page_count: int,
        azure_model: str,
        duration_ms: int,
    ) -> None:
        self._s.add(
            OcrUsageEvent(
                id=id, doc_id=doc_id, stage_run_id=stage_run_id,
                correlation_id=correlation_id, page_count=page_count,
                azure_model=azure_model, duration_ms=duration_ms,
            )
        )
        await self._s.flush()

    async def record_llm_usage(
        self,
        id: str,
        *,
        doc_id: str | None,
        stage_run_id: str | None,
        source: str,
        correlation_id: str | None,
        model: str,
        input_tokens: int,
        output_tokens: int,
        duration_ms: int,
    ) -> None:
        self._s.add(
            LlmUsageEvent(
                id=id, doc_id=doc_id, stage_run_id=stage_run_id, source=source,
                correlation_id=correlation_id, model=model,
                input_tokens=input_tokens, output_tokens=output_tokens,
                duration_ms=duration_ms,
            )
        )
        await self._s.flush()

    async def runs_for_document(self, doc_id: str) -> dict[str, RunFacts]:
        rows = (
            await self._s.execute(
                sqlalchemy.select(StageRun)
                .where(StageRun.doc_id == doc_id, StageRun.status == "done")
                .order_by(StageRun.created_at)
            )
        ).scalars().all()
        facts: dict[str, RunFacts] = {}
        for r in rows:  # later done-run for a stage overwrites the earlier
            facts[r.stage_name] = RunFacts(r.stage_version, r.input_hash, r.output_hash or "")
        return facts

    async def latest_run(self, doc_id: str, stage_name: str) -> StageRun | None:
        """The most recent run of `stage_name`, WHATEVER its status.

        Use this to answer "what is this stage doing?". To answer "give me
        this stage's output", use `latest_completed_run` -- see its docstring
        for what taking the newest row instead actually costs."""
        return (
            await self._s.execute(
                sqlalchemy.select(StageRun)
                .where(StageRun.doc_id == doc_id, StageRun.stage_name == stage_name)
                .order_by(StageRun.created_at.desc())
                .limit(1)
            )
        ).scalars().first()

    async def latest_completed_run(self, doc_id: str, stage_name: str) -> StageRun | None:
        """The most recent run of `stage_name` that PRODUCED AN ARTIFACT.

        A stage can hold several runs at once: an older `done` one with a real
        `artifact_key`, and a newer `running` or `failed` one with NULL. Asking
        for the newest row and then testing its `artifact_key` reports "no
        artifact" whenever a re-run is in flight -- even though a perfectly good
        artifact is sitting in the store, and even though the re-run may be
        about to fail and leave that artifact the only answer there is.

        MEASURED: reprocessing RIL from `basis` left `fog_mapping` with a `done`
        run and a newer `running` one. `run_stage`'s dependency read took the
        newer row, found a NULL key, and `fog_reason` died with a bare
        `AssertionError()`. The same lookup backs
        `GET /documents/{id}/artifacts/{stage}`, so the UI simultaneously showed
        "not built yet" for stages whose artifacts existed.

        Filtering on `artifact_key IS NOT NULL` as well as `status` is
        deliberate belt-and-braces: the key is what the caller actually needs,
        and a `done` row without one would be a broken invariant rather than a
        reason to hand back None from a different code path."""
        return (
            await self._s.execute(
                sqlalchemy.select(StageRun)
                .where(
                    StageRun.doc_id == doc_id,
                    StageRun.stage_name == stage_name,
                    StageRun.status == "done",
                    StageRun.artifact_key.is_not(None),
                )
                .order_by(StageRun.created_at.desc())
                .limit(1)
            )
        ).scalars().first()

    async def list_documents(self) -> list[Document]:
        """Every document in the corpus, ordered by id (deterministic).

        Corpus-scale note (SP-16): this materializes all documents; at 5000+ docs
        a streamed/paginated variant is the scale path, deferred with on-demand
        entity resolution (spec §6)."""
        rows = (
            await self._s.execute(sqlalchemy.select(Document).order_by(Document.id))
        ).scalars().all()
        return list(rows)

    async def latest_runs(self, doc_id: str) -> dict[str, StageRun]:
        """The latest `stage_run` per `stage_name` for this document, any status."""
        rows = (
            await self._s.execute(
                sqlalchemy.select(StageRun)
                .where(StageRun.doc_id == doc_id)
                .order_by(StageRun.created_at)
            )
        ).scalars().all()
        latest: dict[str, StageRun] = {}
        for r in rows:  # later row (by created_at) wins per stage_name
            latest[r.stage_name] = r
        return latest

    async def latest_run_status_counts(
        self, doc_ids: Sequence[str]
    ) -> dict[str, dict[str, int]]:
        """{doc_id: {status: count}} over the LATEST `stage_run` per
        (doc_id, stage_name), for every id in `doc_ids`.

        Latest-per-stage, not all-runs: a document whose `ocr` was retried
        three times must contribute one `ocr` to the summary, not three --
        otherwise a flaky stage looks like a bigger pipeline. Statuses with
        no rows are simply absent from the inner dict.

        ONE query for the whole page of documents (a `row_number()` window
        partitioned by (doc_id, stage_name)), not one per document: the
        caller is a paginated list route, and N+1 there is the difference
        between one round trip and fifty. An empty `doc_ids` short-circuits
        to `{}` without a query, same posture as `DocumentRepository.get_many`.
        """
        if not doc_ids:
            return {}
        ranked = (
            select(
                StageRun.doc_id.label("doc_id"),
                StageRun.status.label("status"),
                func.row_number()
                .over(
                    partition_by=(StageRun.doc_id, StageRun.stage_name),
                    order_by=(StageRun.created_at.desc(), StageRun.id.desc()),
                )
                .label("rn"),
            )
            .where(StageRun.doc_id.in_(list(doc_ids)))
            .subquery()
        )
        rows = (
            await self._s.execute(
                select(ranked.c.doc_id, ranked.c.status, func.count())
                .where(ranked.c.rn == 1)
                .group_by(ranked.c.doc_id, ranked.c.status)
            )
        ).all()
        counts: dict[str, dict[str, int]] = {}
        for doc_id, status, n in rows:
            counts.setdefault(str(doc_id), {})[str(status)] = int(n)
        return counts

    async def stage_throughput(self) -> list[StageThroughput]:
        """One row per distinct `stage_name`, over the LATEST run per
        (doc_id, stage_name), ordered by name.

        The corpus-wide counterpart of `latest_run_status_counts`, which is
        keyed by document and takes a page of ids; this one has no id filter
        because `GET /analytics` is not paginated. Same latest-per-stage rule
        (see `_latest_stage_runs`).

        `median_seconds` is `percentile_cont(0.5)` over the duration of the
        runs counted as `done` ONLY -- a failed run's duration is the time it
        took to give up, which is not throughput, and a `running` run's
        `updated_at` is just "now" and would drag the median toward wall-clock
        age. The `FILTER` is what keeps those out of the ordered set; with no
        `done` runs the aggregate returns NULL, which is the contract's
        `median_seconds: null` rather than a zero that would read as "instant".

        One query with a window function, not one per stage: the number of
        distinct stages is a property of the pipeline, but the number of
        round trips should not be.
        """
        ranked = _latest_stage_runs()

        def _count(status: str) -> sqlalchemy.sql.elements.ColumnElement[int]:
            return func.count().filter(ranked.c.status == status)

        rows = (
            await self._s.execute(
                select(
                    ranked.c.stage_name,
                    _count("done"),
                    _count("running"),
                    _count("queued"),
                    _count("failed"),
                    func.percentile_cont(0.5)
                    .within_group(ranked.c.seconds)
                    .filter(ranked.c.status == "done"),
                )
                .where(ranked.c.rn == 1)
                .group_by(ranked.c.stage_name)
                .order_by(ranked.c.stage_name)
            )
        ).all()
        return [
            StageThroughput(
                name=str(name),
                done=int(done),
                running=int(running),
                queued=int(queued),
                failed=int(failed),
                median_seconds=None if median is None else float(median),
            )
            for name, done, running, queued, failed, median in rows
        ]

    async def document_outcome_counts(self) -> dict[str, int]:
        """{"complete": n, "failed": n} over the whole corpus.

        `complete` is documents whose latest `index` run is `done`: `index`
        is the pipeline's terminal stage and nothing depends on it, so it is
        the only stage whose success means the document is finished. Counting
        "all stages done" instead would mark a document complete the moment
        its first stage succeeded, since absent stages have no rows.

        `failed` is documents with at least one latest-per-stage run in
        `failed` -- latest-per-stage so a stage that failed and was
        successfully retried does not keep the document marked failed
        forever.

        `count(distinct case(...))` rather than two queries: the CASE yields
        NULL for non-matching rows and `count` ignores NULLs, so both figures
        fall out of one scan of the same ranked set. A document can be in
        both counts (a failed `ocr` retry alongside a done `index`); they are
        two independent questions, not a partition.
        """
        ranked = _latest_stage_runs()
        complete = func.count(
            distinct(
                case(
                    (
                        (ranked.c.stage_name == "index") & (ranked.c.status == "done"),
                        ranked.c.doc_id,
                    ),
                    else_=None,
                )
            )
        )
        failed = func.count(
            distinct(case((ranked.c.status == "failed", ranked.c.doc_id), else_=None))
        )
        row = (
            await self._s.execute(select(complete, failed).where(ranked.c.rn == 1))
        ).one()
        return {"complete": int(row[0]), "failed": int(row[1])}

    async def document_cost_by_source(self, doc_id: str) -> list[CostBySource]:
        """Every source of billable usage for one document: "ocr" (if any
        ocr_usage_event rows exist) plus each distinct llm_usage_event
        `source` that has at least one row for this doc_id."""
        rows: list[CostBySource] = []
        ocr_row = (
            await self._s.execute(
                select(
                    func.count(),
                    func.coalesce(func.sum(OcrUsageEvent.page_count), 0),
                    func.coalesce(func.sum(OcrUsageEvent.duration_ms), 0),
                ).where(OcrUsageEvent.doc_id == doc_id)
            )
        ).one()
        if ocr_row[0]:
            rows.append(
                CostBySource(
                    source="ocr", call_count=int(ocr_row[0]),
                    page_count=int(ocr_row[1]), input_tokens=0, output_tokens=0,
                    duration_ms=int(ocr_row[2]),
                )
            )
        llm_rows = (
            await self._s.execute(
                select(
                    LlmUsageEvent.source,
                    func.count(),
                    func.coalesce(func.sum(LlmUsageEvent.input_tokens), 0),
                    func.coalesce(func.sum(LlmUsageEvent.output_tokens), 0),
                    func.coalesce(func.sum(LlmUsageEvent.duration_ms), 0),
                )
                .where(LlmUsageEvent.doc_id == doc_id)
                .group_by(LlmUsageEvent.source)
                .order_by(LlmUsageEvent.source)
            )
        ).all()
        rows.extend(
            CostBySource(
                source=str(source), call_count=int(count), page_count=0,
                input_tokens=int(in_tok), output_tokens=int(out_tok),
                duration_ms=int(dur),
            )
            for source, count, in_tok, out_tok, dur in llm_rows
        )
        return rows

    async def document_usage_events(self, doc_id: str) -> list[UsageEventRecord]:
        """Every individual OCR/LLM call for one document, newest first --
        the raw per-call detail document_cost_by_source aggregates over.
        Costing feature drill-down (spec sec 6's deferred "raw per-call
        event listing endpoint")."""
        ocr_rows = (
            await self._s.execute(
                select(OcrUsageEvent).where(OcrUsageEvent.doc_id == doc_id)
            )
        ).scalars().all()
        llm_rows = (
            await self._s.execute(
                select(LlmUsageEvent).where(LlmUsageEvent.doc_id == doc_id)
            )
        ).scalars().all()
        events: list[UsageEventRecord] = [
            UsageEventRecord(
                id=r.id, kind="ocr", source="ocr", model=r.azure_model,
                page_count=r.page_count, input_tokens=0, output_tokens=0,
                duration_ms=r.duration_ms, stage_run_id=r.stage_run_id,
                correlation_id=r.correlation_id, created_at=r.created_at,
            )
            for r in ocr_rows
        ] + [
            UsageEventRecord(
                id=r.id, kind="llm", source=r.source, model=r.model,
                page_count=0, input_tokens=r.input_tokens,
                output_tokens=r.output_tokens, duration_ms=r.duration_ms,
                stage_run_id=r.stage_run_id, correlation_id=r.correlation_id,
                created_at=r.created_at,
            )
            for r in llm_rows
        ]
        events.sort(key=lambda e: e["created_at"], reverse=True)
        return events

    async def corpus_cost_totals(self) -> CorpusCostTotals:
        """Every billable event corpus-wide, including /ask's doc_id=NULL
        rows -- the "grand total" figure for GET /analytics."""
        ocr_total = (
            await self._s.execute(
                select(func.coalesce(func.sum(OcrUsageEvent.page_count), 0))
            )
        ).scalar_one()
        llm_totals = (
            await self._s.execute(
                select(
                    func.coalesce(func.sum(LlmUsageEvent.input_tokens), 0),
                    func.coalesce(func.sum(LlmUsageEvent.output_tokens), 0),
                )
            )
        ).one()
        return CorpusCostTotals(
            ocr_page_count=int(ocr_total),
            llm_input_tokens=int(llm_totals[0]),
            llm_output_tokens=int(llm_totals[1]),
        )

    async def corpus_cost_by_source(self) -> list[CostBySource]:
        """Every source of billable usage corpus-wide (costing feature) --
        "ocr" plus each distinct llm_usage_event source, across every
        document AND /ask's doc_id=NULL rows. The corpus-wide sibling of
        document_cost_by_source (same shape, no doc_id filter)."""
        rows: list[CostBySource] = []
        ocr_row = (
            await self._s.execute(
                select(
                    func.count(),
                    func.coalesce(func.sum(OcrUsageEvent.page_count), 0),
                    func.coalesce(func.sum(OcrUsageEvent.duration_ms), 0),
                )
            )
        ).one()
        if ocr_row[0]:
            rows.append(
                CostBySource(
                    source="ocr", call_count=int(ocr_row[0]),
                    page_count=int(ocr_row[1]), input_tokens=0, output_tokens=0,
                    duration_ms=int(ocr_row[2]),
                )
            )
        llm_rows = (
            await self._s.execute(
                select(
                    LlmUsageEvent.source,
                    func.count(),
                    func.coalesce(func.sum(LlmUsageEvent.input_tokens), 0),
                    func.coalesce(func.sum(LlmUsageEvent.output_tokens), 0),
                    func.coalesce(func.sum(LlmUsageEvent.duration_ms), 0),
                )
                .group_by(LlmUsageEvent.source)
                .order_by(LlmUsageEvent.source)
            )
        ).all()
        rows.extend(
            CostBySource(
                source=str(source), call_count=int(count), page_count=0,
                input_tokens=int(in_tok), output_tokens=int(out_tok),
                duration_ms=int(dur),
            )
            for source, count, in_tok, out_tok, dur in llm_rows
        )
        return rows

    async def corpus_cost_by_document(self) -> list[DocumentCostSummary]:
        """Raw usage totals per document, corpus-wide (costing feature) --
        the "which filings cost the most" listing. /ask's doc_id=NULL rows
        belong to no document and are excluded (they still count in
        corpus_cost_totals/corpus_cost_by_source). Two grouped queries
        (ocr, llm) merged in Python rather than a SQL UNION, since the two
        tables measure different things (pages vs tokens) -- matches
        document_cost_by_source's own per-source merge shape, one level up."""
        ocr_totals: dict[str, int] = {
            str(doc_id): int(count)
            for doc_id, count in (
                await self._s.execute(
                    select(OcrUsageEvent.doc_id, func.sum(OcrUsageEvent.page_count))
                    .group_by(OcrUsageEvent.doc_id)
                )
            ).all()
        }
        llm_totals: dict[str, tuple[int, int]] = {
            str(doc_id): (int(inp), int(out))
            for doc_id, inp, out in (
                await self._s.execute(
                    select(
                        LlmUsageEvent.doc_id,
                        func.sum(LlmUsageEvent.input_tokens),
                        func.sum(LlmUsageEvent.output_tokens),
                    )
                    .where(LlmUsageEvent.doc_id.is_not(None))
                    .group_by(LlmUsageEvent.doc_id)
                )
            ).all()
        }
        doc_ids = set(ocr_totals) | set(llm_totals)
        if not doc_ids:
            return []
        docs = (
            await self._s.execute(select(Document).where(Document.id.in_(doc_ids)))
        ).scalars().all()
        doc_by_id = {d.id: d for d in docs}
        result: list[DocumentCostSummary] = []
        for doc_id in doc_ids:
            doc = doc_by_id.get(doc_id)
            in_tok, out_tok = llm_totals.get(doc_id, (0, 0))
            result.append(
                DocumentCostSummary(
                    doc_id=doc_id,
                    doc_key=doc.doc_key if doc else None,
                    filename=doc.filename if doc else None,
                    ocr_page_count=int(ocr_totals.get(doc_id) or 0),
                    llm_input_tokens=in_tok,
                    llm_output_tokens=out_tok,
                )
            )
        return result

    async def stage_cost_totals(self) -> dict[str, CorpusCostTotals]:
        """{stage_name: totals} for every stage that cost anything: "ocr"
        (page charges) plus each stage whose LLM calls carry a stage_run_id.
        /ask and extract_document run outside run_stage, so they have no
        stage_run_id and no stage entry here -- they only ever appear in
        corpus_cost_totals. A stage with zero billable events is simply
        absent from the returned dict.

        The stage name is JOINED from stage_run rather than inferred from
        `source`. The previous version filtered on source == 'semantic_
        classify' and hardcoded the key "semantic", which reported every
        other stage's spend as zero -- and `fog_mapping`, the only stage in
        the FOG pipeline that spends money at all, is exactly such a stage.
        Joining means a new billed stage appears here on its own."""
        result: dict[str, CorpusCostTotals] = {}
        ocr_total = (
            await self._s.execute(
                select(func.coalesce(func.sum(OcrUsageEvent.page_count), 0))
            )
        ).scalar_one()
        if ocr_total:
            result["ocr"] = CorpusCostTotals(
                ocr_page_count=int(ocr_total), llm_input_tokens=0, llm_output_tokens=0
            )
        by_stage = (
            await self._s.execute(
                select(
                    StageRun.stage_name,
                    func.coalesce(func.sum(LlmUsageEvent.input_tokens), 0),
                    func.coalesce(func.sum(LlmUsageEvent.output_tokens), 0),
                )
                .join(StageRun, LlmUsageEvent.stage_run_id == StageRun.id)
                .group_by(StageRun.stage_name)
            )
        ).all()
        for stage_name, in_tok, out_tok in by_stage:
            if not in_tok and not out_tok:
                continue
            prior = result.get(stage_name)
            result[stage_name] = CorpusCostTotals(
                ocr_page_count=prior["ocr_page_count"] if prior else 0,
                llm_input_tokens=int(in_tok),
                llm_output_tokens=int(out_tok),
            )
        return result


class EvalRepository:
    """Eval run + case_result persistence. The caller owns the transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def record_run(
        self,
        run_id: str,
        candidate_name: str,
        candidate_version: int,
        dataset_name: str,
        dataset_version: int,
        metric_name: str,
        outcome: EvalOutcome,
    ) -> None:
        self._s.add(
            EvalRun(
                id=run_id,
                candidate_name=candidate_name,
                candidate_version=candidate_version,
                dataset_name=dataset_name,
                dataset_version=dataset_version,
                metric_name=metric_name,
                aggregate=outcome.aggregate,
            )
        )
        # Flush the parent row before the children: EvalCaseResult.eval_run_id is
        # a real FK, and with no ORM relationship() linking the two mapped
        # classes, the unit of work has no dependency edge to order these two
        # inserts itself — a single flush() at the end risked emitting the
        # child INSERT first and tripping the FK constraint (seen locally).
        await self._s.flush()
        for result in outcome.results:
            self._s.add(
                EvalCaseResult(
                    id=str(uuid.uuid4()),
                    eval_run_id=run_id,
                    case_id=result.case_id,
                    score=result.score,
                )
            )
        await self._s.flush()

    async def leaderboard(
        self, dataset_name: str, metric_name: str
    ) -> list[tuple[str, int, float]]:
        # SP-3 scale: datasets/candidates are small, so fetch every matching run
        # and reduce to "latest per candidate" in Python rather than a
        # window-function/subquery on max(created_at). Simpler to read and
        # correct; revisit with a DB-side query if this table grows large.
        rows = (
            await self._s.execute(
                sqlalchemy.select(EvalRun).where(
                    EvalRun.dataset_name == dataset_name,
                    EvalRun.metric_name == metric_name,
                )
            )
        ).scalars().all()

        latest: dict[str, EvalRun] = {}
        for run in rows:
            current = latest.get(run.candidate_name)
            if current is None or run.created_at > current.created_at:
                latest[run.candidate_name] = run

        ranked = sorted(latest.values(), key=lambda r: r.aggregate, reverse=True)
        return [(r.candidate_name, r.candidate_version, r.aggregate) for r in ranked]


class DocumentRepository:
    """Document intake persistence. The caller owns the transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def get(self, doc_id: str) -> Document | None:
        return await self._s.get(Document, doc_id)

    async def get_by_checksum(self, checksum: str) -> Document | None:
        return (
            await self._s.execute(select(Document).where(Document.checksum == checksum))
        ).scalars().first()

    async def next_version(self, doc_key: str) -> int:
        max_version = (
            await self._s.execute(
                select(func.max(Document.version)).where(Document.doc_key == doc_key)
            )
        ).scalar_one()
        return (max_version or 0) + 1

    async def create(
        self,
        *,
        id: str,
        checksum: str,
        filename: str,
        byte_size: int,
        content_type: str,
        doc_key: str,
        version: int,
    ) -> Document:
        doc = Document(
            id=id,
            checksum=checksum,
            filename=filename,
            byte_size=byte_size,
            content_type=content_type,
            doc_key=doc_key,
            version=version,
        )
        self._s.add(doc)
        await self._s.flush()
        return doc

    async def list_by_doc_key(self, doc_key: str) -> list[Document]:
        rows = (
            await self._s.execute(
                select(Document).where(Document.doc_key == doc_key).order_by(Document.version)
            )
        ).scalars().all()
        return list(rows)

    async def list_latest_per_doc_key(
        self, *, limit: int, offset: int
    ) -> tuple[list[Document], int]:
        """(one page of the corpus, total group count).

        One row per doc_key -- its HIGHEST version -- so a UI's document
        list shows each filing once rather than once per re-upload. Grouped
        on `coalesce(doc_key, id)`, NOT on the bare column: `doc_key` is
        nullable (early intake rows predate it), and grouping on NULL would
        collapse every such legacy row into one phantom document. Ranking is
        `version DESC NULLS LAST, created_at DESC, id` so the winner is
        deterministic even when two rows share a version.

        `total` counts GROUPS, not rows -- it is the denominator for the
        page count the UI renders, and must not move when someone uploads a
        new version of a document already in the list.
        """
        group = func.coalesce(Document.doc_key, Document.id)
        ranked = (
            select(
                Document.id.label("id"),
                func.row_number()
                .over(
                    partition_by=group,
                    order_by=(
                        Document.version.desc().nullslast(),
                        Document.created_at.desc(),
                        Document.id,
                    ),
                )
                .label("rn"),
            )
            .subquery()
        )
        latest_ids = select(ranked.c.id).where(ranked.c.rn == 1).subquery()
        total = (
            await self._s.execute(select(func.count()).select_from(latest_ids))
        ).scalar_one()
        rows = (
            await self._s.execute(
                select(Document)
                .where(Document.id.in_(select(latest_ids.c.id)))
                .order_by(Document.created_at.desc(), Document.id)
                .limit(limit)
                .offset(offset)
            )
        ).scalars().all()
        return list(rows), int(total)

    async def corpus_totals(self) -> CorpusTotals:
        """Whole-corpus counters for `GET /analytics`, in one round trip.

        `doc_key_count` counts distinct `coalesce(doc_key, id)` groups, NOT
        distinct `doc_key` -- exactly as `list_latest_per_doc_key` groups,
        and for the same reason: `doc_key` is nullable (early intake rows
        predate it), so `count(distinct doc_key)` would drop every legacy row
        from the total instead of counting each as its own filing, and the
        number on the analytics screen would disagree with the number of rows
        the document list renders.

        `total_bytes` coalesces `byte_size` to 0 per row, then coalesces the
        SUM itself: the inner one stops a single NULL-sized row from being
        silently skipped, the outer one turns the empty-corpus `sum() -> NULL`
        into the 0 the response contract requires. `count()` and `max()` need
        no such help -- they already return 0 and NULL.

        No `chunk_count` here (RT-9): that figure now comes from the
        injected `VectorStore`, not this pure-Postgres repository.
        """
        totals = (
            await self._s.execute(
                select(
                    func.count(),
                    func.count(distinct(func.coalesce(Document.doc_key, Document.id))),
                    func.coalesce(func.sum(func.coalesce(Document.byte_size, 0)), 0),
                    func.max(Document.created_at),
                ).select_from(Document)
            )
        ).one()
        return CorpusTotals(
            document_count=int(totals[0]),
            doc_key_count=int(totals[1]),
            total_bytes=int(totals[2]),
            latest_ingested_at=totals[3],
        )

    async def get_many(self, ids: list[str]) -> dict[str, Document]:
        """Every `Document` row whose id is in `ids`, keyed by id. Empty input ->
        no query (RT-2's vector_search calls this once per request for its
        doc_key/version filter check AND its doc_key/version citation join --
        an empty hit list must not become a wasted round trip). Unknown ids are
        simply absent from the result, not an error -- same "omit, don't raise"
        posture as resolve_chunks (fdi/pipeline/retrieval.py)."""
        if not ids:
            return {}
        rows = (
            await self._s.execute(select(Document).where(Document.id.in_(ids)))
        ).scalars().all()
        return {d.id: d for d in rows}

    async def delete(self, doc_id: str) -> None:
        """Remove a document, its stage_run history, and its usage-event
        history. None of `stage_run.doc_id`, `ocr_usage_event.doc_id`/
        `.stage_run_id`, or `llm_usage_event.doc_id`/`.stage_run_id` have
        ON DELETE CASCADE, so children are deleted explicitly before their
        parents -- usage events (costing feature) before stage_run (they FK
        to it), stage_run before document (deleting document first would
        violate every one of these FKs)."""
        await self._s.execute(
            sqlalchemy.delete(OcrUsageEvent).where(OcrUsageEvent.doc_id == doc_id)
        )
        await self._s.execute(
            sqlalchemy.delete(LlmUsageEvent).where(LlmUsageEvent.doc_id == doc_id)
        )
        await self._s.execute(
            sqlalchemy.delete(StageRun).where(StageRun.doc_id == doc_id)
        )
        await self._s.execute(sqlalchemy.delete(Document).where(Document.id == doc_id))
        await self._s.flush()

    async def ids_for(self, *, doc_key: str | None, version: int | None) -> list[str]:
        """Document ids matching a doc_key and/or version. Used to scope a
        Milvus filter, which keys on doc_id (a Stage has no doc_key to write).

        Both None -> every document id. Callers that mean "unscoped" must not
        call this at all; `bm25_search`/`vector_search` guard on that before
        reaching here."""
        stmt = select(Document.id)
        if doc_key is not None:
            stmt = stmt.where(Document.doc_key == doc_key)
        if version is not None:
            stmt = stmt.where(Document.version == version)
        return list((await self._s.execute(stmt)).scalars().all())


class UserRepository:
    """app_user + refresh_token persistence (ADR-0002). The caller owns the
    transaction -- commit is theirs, not ours.

    Emails are normalised (strip + lowercase) HERE, at the only write and read
    edges, so an Invitee created as ``Mentor@Example.ORG`` logs in as
    ``mentor@example.org`` and the unique constraint is on the canonical form.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    @staticmethod
    def _canonical(email: str) -> str:
        return email.strip().lower()

    async def create(self, *, email: str, password_hash: str, active: bool = True) -> AppUser:
        user = AppUser(
            id=str(uuid.uuid4()),
            email=self._canonical(email),
            password_hash=password_hash,
            active=active,
        )
        self._s.add(user)
        await self._s.flush()
        return user

    async def get_by_email(self, email: str) -> AppUser | None:
        stmt = select(AppUser).where(AppUser.email == self._canonical(email))
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def get(self, user_id: str) -> AppUser | None:
        return await self._s.get(AppUser, user_id)

    async def set_active(self, user_id: str, *, active: bool) -> AppUser | None:
        user = await self._s.get(AppUser, user_id)
        if user is None:
            return None
        user.active = active
        await self._s.flush()
        return user

    async def list(self) -> list[AppUser]:
        stmt = select(AppUser).order_by(AppUser.created_at)
        return list((await self._s.execute(stmt)).scalars().all())

    async def store_refresh_token(
        self, *, user_id: str, token_hash: str, expires_at: datetime
    ) -> None:
        self._s.add(
            RefreshToken(
                id=str(uuid.uuid4()),
                user_id=user_id,
                token_hash=token_hash,
                expires_at=expires_at,
            )
        )
        await self._s.flush()

    async def purge_expired_refresh_tokens(self, *, now: datetime) -> int:
        """Delete rows whose TTL has passed, returning the count.

        Refresh rows are never touched again after revocation, so nothing else
        ever removes them -- without this, every login leaks one row forever.
        Called opportunistically by each session issue (login + refresh),
        which bounds the table at ~one TTL's worth of live sessions with no
        cron to forget. Revoked-but-unexpired rows are deliberately kept: the
        burn-on-refuse path in the refresh route still needs to distinguish
        "revoked" from "unknown" until the TTL would have ended them anyway.
        """
        result = await self._s.execute(
            sqlalchemy.delete(RefreshToken).where(RefreshToken.expires_at < now)
        )
        # CursorResult.rowcount is typed only on the sync flavor; asyncpg's
        # driver-level result reaches us as Result. The count is advisory (it
        # feeds nothing but logs/debug), so the runtime attribute is fine and
        # getattr keeps mypy --strict honest about it.
        return int(getattr(result, "rowcount", 0))

    async def find_refresh_token(self, token_hash: str) -> RefreshToken | None:
        return (
            await self._s.execute(select(RefreshToken).where(RefreshToken.token_hash == token_hash))
        ).scalar_one_or_none()

    async def revoke_refresh_token(self, token_id: str) -> None:
        token = await self._s.get(RefreshToken, token_id)
        assert token is not None
        token.revoked_at = func.now()
        await self._s.flush()


class JobRepository:
    """api_job persistence (T5). The caller owns the transaction -- commit is
    theirs, not ours.

    Every read a ROUTE makes goes through an `_for_user` method: ownership is
    part of the lookup, not a check after it, so a route cannot fetch a job
    and forget to compare owners. `get` (unscoped) exists for the worker,
    which has no user and runs whatever it was handed.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def create(
        self,
        *,
        job_id: str,
        user_id: str,
        kind: str,
        params: dict[str, object],
        correlation_id: str,
    ) -> ApiJob:
        job = ApiJob(
            id=job_id,
            user_id=user_id,
            kind=kind,
            status="queued",
            params=params,
            correlation_id=correlation_id,
        )
        self._s.add(job)
        await self._s.flush()
        return job

    async def get(self, job_id: str) -> ApiJob | None:
        return await self._s.get(ApiJob, job_id)

    async def get_for_user(self, job_id: str, user_id: str) -> ApiJob | None:
        stmt = select(ApiJob).where(ApiJob.id == job_id, ApiJob.user_id == user_id)
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def list_for_user(
        self, user_id: str, *, kind: str | None = None, limit: int = 50
    ) -> list[ApiJob]:
        stmt = select(ApiJob).where(ApiJob.user_id == user_id)
        if kind is not None:
            stmt = stmt.where(ApiJob.kind == kind)
        stmt = stmt.order_by(ApiJob.created_at.desc(), ApiJob.id).limit(limit)
        return list((await self._s.execute(stmt)).scalars().all())

    async def lock_submissions(self, user_id: str) -> None:
        """Serialise this user's job submissions until the transaction ends
        (T7). Without it, two parallel tabs both count zero jobs in flight
        and both pass a cap of one. A transaction-scoped advisory lock: it is
        released by the commit or rollback, so it cannot leak."""
        await self._s.execute(
            select(func.pg_advisory_xact_lock(func.hashtextextended(f"api_job:{user_id}", 0)))
        )

    async def in_flight_ids(self, user_id: str, *, stale_after_seconds: int) -> list[str]:
        """Ids of this user's jobs still queued or running, oldest first --
        ignoring any whose status has not moved for `stale_after_seconds`,
        which a lost worker would otherwise hold forever."""
        cutoff = func.now() - sqlalchemy.literal(
            timedelta(seconds=stale_after_seconds), sqlalchemy.Interval
        )
        stmt = (
            select(ApiJob.id)
            .where(
                ApiJob.user_id == user_id,
                ApiJob.status.in_(("queued", "running")),
                ApiJob.updated_at > cutoff,
            )
            .order_by(ApiJob.created_at, ApiJob.id)
        )
        return list((await self._s.execute(stmt)).scalars().all())

    async def mark_running(self, job_id: str) -> None:
        await self._set(job_id, status="running")

    async def mark_succeeded(self, job_id: str, result: object) -> None:
        await self._set(job_id, status="succeeded", result=result, error=None)

    async def mark_failed(self, job_id: str, error: str) -> None:
        await self._set(job_id, status="failed", error=error)

    async def _set(self, job_id: str, **values: object) -> None:
        job = await self._s.get(ApiJob, job_id)
        if job is None:
            raise LookupError(f"job {job_id} not found")
        for name, value in values.items():
            setattr(job, name, value)
        await self._s.flush()
