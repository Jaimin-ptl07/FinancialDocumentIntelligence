"""Engine/session factories.

NEVER build an engine at module import. fork() copies the object graph but not
the threads driving it, so an inherited pool hands two interpreters the same
socket and corrupts silently. Celery tasks build an engine inside their
asyncio.run() coroutine and dispose it in `finally`; FastAPI builds one in
lifespan. Both own it for exactly as long as their event loop lives.
"""

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def make_engine(url: str, *, echo: bool = False) -> AsyncEngine:
    return create_async_engine(url, echo=echo, pool_pre_ping=True)


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
