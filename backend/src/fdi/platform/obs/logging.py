"""Structured logging with a correlation ID that survives process boundaries.

contextvars is copy-IN / no-copy-OUT. An ID bound before asyncio.run() is
visible inside the loop; one bound inside is lost. So Celery tasks MUST bind in
the sync wrapper, from the message header, before calling asyncio.run().
"""

import logging
import re
import sys
import uuid

import structlog

_CORRELATION_ID = "correlation_id"

# 36 is not a style choice: `jobs.correlation_id` is String(36), and PostgreSQL
# RAISES StringDataRightTruncation past a varchar's width rather than truncating.
# Since the API accepts this value from a client header, an unbounded id turns a
# request header into a 500 -- measured: `X-Request-ID` of 37 chars -> 500.
CORRELATION_ID_MAX_LENGTH = 36

# RFC 3986 s2.3 "unreserved" characters only. Deliberately narrow, for three
# reasons that are each independently sufficient:
#   1. The value is echoed into a response header, where a CR or LF is response
#      splitting.
#   2. It is rendered into the RFC 9457 `instance` member as a URI; unreserved
#      chars need no escaping, so the id and the URI stay byte-identical.
#   3. It reaches the log as a JSON string value. structlog's JSONRenderer DOES
#      escape quotes and newlines correctly (measured -- forging a log line via
#      `x","level":"critical"` fails), so this rule is defence in depth there
#      rather than the thing standing between us and log injection.
# Wide enough for every request-id format a fronting proxy actually stamps:
# uuid4 (36), nginx $request_id (32 hex), X-Ray (35, dashes), cf-ray.
_CORRELATION_ID_RE = re.compile(rf"^[A-Za-z0-9._~-]{{1,{CORRELATION_ID_MAX_LENGTH}}}$")


def new_correlation_id() -> str:
    return str(uuid.uuid4())


def is_valid_correlation_id(value: str) -> bool:
    """Is `value` usable as a correlation id everywhere this app puts one?

    Everywhere means: a varchar(36) column, a response header, a URI, and a JSON
    log value. The narrowest of those wins, so the rule is one regex rather than
    a per-sink check that would inevitably disagree with itself.
    """
    return _CORRELATION_ID_RE.match(value) is not None


def bind_correlation_id(value: str) -> None:
    structlog.contextvars.bind_contextvars(**{_CORRELATION_ID: value})


def get_correlation_id() -> str:
    value = structlog.contextvars.get_contextvars().get(_CORRELATION_ID, "")
    return str(value)


def clear_correlation_id() -> None:
    structlog.contextvars.unbind_contextvars(_CORRELATION_ID)


# Loggers that install their OWN handlers and then refuse to propagate, which
# routes their records around this module's formatter entirely.
#
# uvicorn runs `logging.config.dictConfig(LOGGING_CONFIG)` from `Config.load()`
# at server startup -- BEFORE lifespan calls configure_logging() -- and that
# config gives the `uvicorn` logger a plain StreamHandler(stderr) with
# `propagate: False`. `uvicorn.error` has no handler of its own and propagates
# to that parent, where it stops. Measured against a real uvicorn 0.51: an
# unhandled 500 printed our single JSON record on stdout AND a bare
# multi-line `Traceback (most recent call last):` on stderr -- i.e. exactly the
# double-printed traceback ProcessorFormatter exists to prevent, reintroduced
# downstream of it. Clearing the handlers and restoring propagation is what
# makes the foreign_pre_chain claim below true rather than aspirational.
#
# Celery is deliberately absent: nothing in the worker calls configure_logging()
# today, so listing its loggers here would assert a fix for a path this function
# never runs on.
_HIJACKED_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


def configure_logging(level: str = "INFO") -> None:
    """Configure structlog + stdlib to emit one JSON line per event.

    ProcessorFormatter (not LoggerFactory+JSONRenderer) because the latter
    double-prints tracebacks: BoundLogger.exception() sets exc_info on the
    LogRecord and stdlib re-appends the traceback structlog already rendered.
    It also gives uvicorn's own loggers the same JSON shape and correlation ID,
    via foreign_pre_chain -- but only because _HIJACKED_LOGGERS undoes uvicorn's
    dictConfig first. See tests/unit/test_uvicorn_logging.py.

    Call it as early as possible in a process: until it runs, structlog's default
    ConsoleRenderer draws through rich, whose box glyphs are unencodable on a
    cp1252 stdout, and the resulting UnicodeEncodeError escapes whatever was
    logging. That is how Task 15's 500 handler returned a bodyless response.
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

    # Must come AFTER the root handler is in place: these loggers have nowhere
    # to send records in between.
    for name in _HIJACKED_LOGGERS:
        hijacked = logging.getLogger(name)
        hijacked.handlers = []
        hijacked.propagate = True
