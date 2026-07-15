"""Celery 5.6.3 has no native async task support (Celery 6 has not shipped).
Decorating `async def` with @app.task returns an un-awaited coroutine and
reports SUCCESS while doing nothing. This walks the AST so it cannot happen."""
import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "fdi"


def _is_task_decorator(dec: ast.expr) -> bool:
    node = dec.func if isinstance(dec, ast.Call) else dec
    if isinstance(node, ast.Attribute):
        return node.attr in {"task", "shared_task"}
    if isinstance(node, ast.Name):
        return node.id in {"task", "shared_task"}
    return False


def test_no_async_def_under_celery_task() -> None:
    files = sorted(SRC.rglob("*.py"))
    # A wrong SRC (bad parents[N] count, typo'd segment, ...) makes rglob()
    # return an empty iterator rather than raise -- the loop below would never
    # run and `assert not offenders` would pass having checked nothing. Fail
    # loudly instead of silently guarding zero files forever.
    assert files, (
        f"scanned zero .py files under {SRC} -- SRC is wrong and this guard "
        "is not actually running against fdi's source tree."
    )

    offenders: list[str] = []
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and any(
                _is_task_decorator(d) for d in node.decorator_list
            ):
                offenders.append(f"{path}:{node.lineno} async def {node.name}")
    assert not offenders, (
        "async def under a Celery task decorator. Celery will report SUCCESS "
        "and do nothing.\nUse:\n"
        "    @app.task\n"
        "    def my_task(x): return asyncio.run(_my_task(x))\n\n"
        + "\n".join(offenders)
    )
