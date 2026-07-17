"""Document intake: checksum, dedup, version, store, register. Spec §3.3.

Flow: checksum the bytes -> dedup on an existing row with the same checksum
(idempotent, no re-store) -> content-address the blob at
`documents/{checksum}.pdf` -> allocate the next version for `doc_key` -> create
the row. Two concurrent identical uploads can both miss the dedup read and
race on the DB's `UNIQUE(checksum)` insert (§3.4); the loser's `create` raises
`IntegrityError`, and it re-reads and returns the winner's row instead of
failing the request.
"""

import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fdi.core.hashing import sha256_hex
from fdi.platform.db.models import Document
from fdi.platform.db.repository import DocumentRepository
from fdi.platform.storage.artifact_store import ArtifactStore


def document_key(checksum: str) -> str:
    return f"documents/{checksum}.pdf"


async def intake_document(
    *,
    pdf: bytes,
    filename: str,
    doc_key: str,
    content_type: str,
    session_factory: async_sessionmaker[AsyncSession],
    store: ArtifactStore,
) -> Document:
    checksum = sha256_hex(pdf)

    async with session_factory() as s:
        existing = await DocumentRepository(s).get_by_checksum(checksum)
        if existing is not None:
            return existing

    await store.put_bytes(document_key(checksum), pdf, content_type)

    async with session_factory() as s:
        repo = DocumentRepository(s)
        version = await repo.next_version(doc_key)
        try:
            doc = await repo.create(
                id=str(uuid.uuid4()),
                checksum=checksum,
                filename=filename,
                byte_size=len(pdf),
                content_type=content_type,
                doc_key=doc_key,
                version=version,
            )
            await s.commit()
        except IntegrityError:
            # Lost the UNIQUE(checksum) race to a concurrent identical upload.
            await s.rollback()
            async with session_factory() as s2:
                winner = await DocumentRepository(s2).get_by_checksum(checksum)
                if winner is None:
                    raise RuntimeError(
                        f"IntegrityError on checksum {checksum!r} but no row exists "
                        "for it after rollback -- the race was not a checksum collision"
                    ) from None
                return winner
        return doc
