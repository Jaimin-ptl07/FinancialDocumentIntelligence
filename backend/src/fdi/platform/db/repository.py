"""Registry + eval persistence. The caller owns the transaction — commit is
theirs, not ours."""

import uuid

import sqlalchemy
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from fdi.core.eval_types import EvalOutcome
from fdi.core.facts import RunFacts
from fdi.platform.db.models import Document, EvalCaseResult, EvalRun, StageRun


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
        return (
            await self._s.execute(
                sqlalchemy.select(StageRun)
                .where(StageRun.doc_id == doc_id, StageRun.stage_name == stage_name)
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
