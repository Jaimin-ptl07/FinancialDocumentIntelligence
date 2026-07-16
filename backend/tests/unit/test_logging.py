import asyncio
import json

from fdi.platform.obs.logging import (
    bind_correlation_id,
    configure_logging,
    get_correlation_id,
    new_correlation_id,
)


def test_new_correlation_id_is_unique() -> None:
    assert new_correlation_id() != new_correlation_id()


def test_bind_then_get_roundtrips() -> None:
    bind_correlation_id("abc-123")
    assert get_correlation_id() == "abc-123"


def test_correlation_id_survives_asyncio_run() -> None:
    """Global Constraint 4. contextvars copies IN across asyncio.run(), so an ID
    bound BEFORE the call is visible inside it."""
    bind_correlation_id("before-loop")

    async def _inner() -> str:
        return get_correlation_id()

    assert asyncio.run(_inner()) == "before-loop"


def test_binding_inside_asyncio_run_does_not_escape() -> None:
    """The other half of Constraint 4, pinned so nobody 'fixes' it later.
    Mutations inside the loop die with the copied Context."""
    bind_correlation_id("outer")

    async def _inner() -> None:
        bind_correlation_id("inner-only")

    asyncio.run(_inner())
    assert get_correlation_id() == "outer"


def test_log_output_is_json_with_correlation_id(capsys) -> None:
    configure_logging("INFO")
    bind_correlation_id("cid-42")
    import structlog

    structlog.get_logger("test").info("hello", extra_field=7)
    line = capsys.readouterr().out.strip().splitlines()[-1]
    payload = json.loads(line)
    assert payload["event"] == "hello"
    assert payload["correlation_id"] == "cid-42"
    assert payload["extra_field"] == 7
    assert payload["level"] == "info"


def test_exception_is_not_double_printed(capsys) -> None:
    """The LoggerFactory+JSONRenderer chain emits the JSON line AND a raw
    traceback. ProcessorFormatter must not."""
    configure_logging("INFO")
    import structlog

    try:
        raise ValueError("boom")
    except ValueError:
        structlog.get_logger("test").exception("failed")

    out = capsys.readouterr().out.strip()
    assert len(out.splitlines()) == 1, f"expected one JSON line, got:\n{out}"
    payload = json.loads(out)
    assert "ValueError: boom" in payload["exception"]
