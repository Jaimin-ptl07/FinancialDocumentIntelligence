"""Global Constraint 3: no engine or connection pool may exist at module import.

Celery's prefork worker fork()s. The child inherits a COPY of the parent's engine
and of its already-open sockets, but not the threads or the event loop driving
them. Two interpreters then hold one socket and interleave protocol frames on it.
Postgres does not raise -- it reads a corrupted stream. The failure is silent,
which is why this is a guard and not a note in code review.

Engines are therefore built inside the function that owns them (a Celery task's
asyncio.run() coroutine, a FastAPI lifespan) and disposed in `finally`. So the
rule is not "no engines" -- it is "no engine construction on any path Python
executes at import time".
"""

import ast
from collections.abc import Iterator
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine

from fdi.platform.db.engine import make_engine, make_session_factory

SRC = Path(__file__).resolve().parents[2] / "src" / "fdi"

# Matched on the callee's FINAL name, so `create_async_engine(...)` and
# `sa.create_async_engine(...)` both hit -- the same shape as _is_task_decorator
# in test_no_async_celery_tasks.py. The sessionmaker names are here because the
# canonical SQLAlchemy/FastAPI tutorial snippet is two module-level lines --
# `engine = create_engine(URL)` then `SessionLocal = sessionmaker(bind=engine)` --
# and that snippet, pasted into a Celery worker, is the exact accident this file
# exists to prevent. Flagging both lines makes the error message match the paste.
ENGINE_FACTORIES = frozenset(
    {
        "create_async_engine",
        "create_engine",
        "async_sessionmaker",
        "sessionmaker",
        "make_engine",
        "make_session_factory",
    }
)


def _callee_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _import_time_nodes(node: ast.AST) -> Iterator[ast.AST]:
    """Yield every node Python evaluates while IMPORTING the module.

    Descends into `if` / `try` / `with` / `for` / `while` and class bodies: all of
    those run at import, and a guard that only reads `tree.body` -- the obvious
    way to write this -- never looks inside any of them.

    Stops at function and lambda bodies, which do NOT run at import. That is not
    an oversight; it is the entire point. `create_async_engine` inside make_engine
    is the correct code, and a guard that flagged it would be deleted within a day.

    A function's decorators and default arguments are the exception: they are
    evaluated when the `def` statement itself executes, i.e. at import. So
    `def f(e=create_async_engine(url))` builds a pool at import while looking like
    it defers -- those are yielded even though the body is not.
    """
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            eager: list[ast.expr] = [
                *child.args.defaults,
                *(d for d in child.args.kw_defaults if d is not None),
            ]
            if not isinstance(child, ast.Lambda):
                eager += child.decorator_list
            for sub in eager:
                yield sub
                yield from _import_time_nodes(sub)
            continue
        yield child
        yield from _import_time_nodes(child)


def test_make_engine_returns_async_engine() -> None:
    engine = make_engine("postgresql+asyncpg://u:p@h:5432/db")
    assert isinstance(engine, AsyncEngine)


def test_make_session_factory_is_callable() -> None:
    engine = make_engine("postgresql+asyncpg://u:p@h:5432/db")
    factory = make_session_factory(engine)
    assert callable(factory)


def test_no_module_level_engine() -> None:
    """Global Constraint 3, enforced across the whole package.

    Scanning only engine.py -- which is what the obvious version of this guard
    does -- watches the one file least likely to offend. engine.py exports
    factories by construction; the accident happens in a CONSUMER, where someone
    reaches for a module-level `_engine` to avoid threading one through. That
    module is the one that gets fork()ed.
    """
    files = sorted(SRC.rglob("*.py"))
    # A wrong SRC makes rglob() return an empty iterator rather than raise, and
    # `assert not offenders` would then pass having read nothing, forever. Same
    # self-check as test_no_async_celery_tasks.py, for the same reason.
    assert files, (
        f"scanned zero .py files under {SRC} -- SRC is wrong and this guard is "
        "not actually running against fdi's source tree."
    )

    offenders: list[str] = []
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in _import_time_nodes(tree):
            if isinstance(node, ast.Call):
                name = _callee_name(node)
                if name in ENGINE_FACTORIES:
                    offenders.append(f"{path.relative_to(SRC)}:{node.lineno} {name}(...)")

    assert not offenders, (
        "Engine/pool constructed at module import. Celery forks this process; the "
        "child inherits the pool's sockets but not the threads driving them, and "
        "two interpreters writing one socket corrupt it SILENTLY.\n"
        "Build it inside the function that owns it and dispose it in `finally`:\n"
        "    async def _work() -> None:\n"
        "        engine = make_engine(url)\n"
        "        try:\n"
        "            ...\n"
        "        finally:\n"
        "            await engine.dispose()\n\n" + "\n".join(offenders)
    )
