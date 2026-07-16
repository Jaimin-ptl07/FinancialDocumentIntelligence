"""Structured logging with a correlation ID that survives process boundaries.

contextvars is copy-IN / no-copy-OUT. An ID bound before asyncio.run() is
visible inside the loop; one bound inside is lost. So Celery tasks MUST bind in
the sync wrapper, from the message header, before calling asyncio.run().
"""

import logging
import sys
import uuid

import structlog

_CORRELATION_ID = "correlation_id"


def new_correlation_id() -> str:
    return str(uuid.uuid4())


def bind_correlation_id(value: str) -> None:
    structlog.contextvars.bind_contextvars(**{_CORRELATION_ID: value})


def get_correlation_id() -> str:
    value = structlog.contextvars.get_contextvars().get(_CORRELATION_ID, "")
    return str(value)


def clear_correlation_id() -> None:
    structlog.contextvars.unbind_contextvars(_CORRELATION_ID)


def configure_logging(level: str = "INFO") -> None:
    """Configure structlog + stdlib to emit one JSON line per event.

    ProcessorFormatter (not LoggerFactory+JSONRenderer) because the latter
    double-prints tracebacks: BoundLogger.exception() sets exc_info on the
    LogRecord and stdlib re-appends the traceback structlog already rendered.
    It also gives uvicorn's and Celery's own loggers the same JSON shape and
    correlation ID, via foreign_pre_chain.
    """
    shared_processors: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,   # MUST be first
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
    ]

    structlog.configure(
        processors=[*shared_processors,
                    structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
