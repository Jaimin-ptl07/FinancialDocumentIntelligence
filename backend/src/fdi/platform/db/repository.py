"""Registry persistence. The caller owns the transaction — commit is theirs,
not ours."""

import sqlalchemy
from sqlalchemy.ext.asyncio import AsyncSession

from fdi.core.facts import RunFacts
from fdi.platform.db.models import Document, StageRun


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
