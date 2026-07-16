"""The Celery app's configuration.

Every assertion here pins a setting whose Celery DEFAULT is wrong for
multi-minute document stages. That is the point of the file: the defaults are
reasonable for sub-second web tasks and quietly destructive for ours, so what
needs guarding is not the code, it is the drift back to the default.
"""

import os
from collections.abc import Iterator

import pytest

REQUIRED_ENV = {
    "FDI_DATABASE_URL": "postgresql+asyncpg://u:p@h:5432/db",
    "FDI_BROKER_URL": "amqp://guest:guest@h:5672/%2F",
    "FDI_RESULT_BACKEND": "redis://h:6379/0",
    "FDI_S3_ENDPOINT": "h:9000",
    "FDI_S3_ACCESS_KEY": "ak",
    "FDI_S3_SECRET_KEY": "sk",
}


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the entire FDI_ namespace, not merely the vars this file needs.

    Setting only REQUIRED_ENV leaves the developer's own FDI_ vars live, and two
    of them break these tests for reasons having nothing to do with the code:

    * `FDI_WORKER_CONCURRENCY=9` exported -> `worker_concurrency == 2` fails
      with `assert 9 == 2`. It is a *legitimate* var to have exported, which is
      what makes this nasty: the developer is not doing anything wrong.
    * ANY unknown FDI_ var exported -> Task 6's validator scans os.environ and
      rejects it, so `Settings()` raises ValidationError before a single
      assertion runs. `delenv` over a known list cannot fix this one -- the
      whole premise is that the name is not known in advance. Only clearing the
      prefix does.

    Both measured against tests/unit/test_config.py, which has this same hole.
    """
    for key in [k for k in os.environ if k.upper().startswith("FDI_")]:
        monkeypatch.delenv(key)
    for k, v in REQUIRED_ENV.items():
        monkeypatch.setenv(k, v)
    from fdi.platform.config import get_settings

    get_settings.cache_clear()
    yield
    # get_settings is an lru_cache, and monkeypatch restores the ENVIRONMENT --
    # not the object already built from it. Without this teardown the Settings
    # made by test_worker_concurrency_honours_the_env_var (concurrency=7)
    # outlives the var that produced it and is served to the next caller of
    # get_settings() in any later test FILE.
    get_settings.cache_clear()


def test_celery_app_config() -> None:
    from fdi.workers.app import create_celery_app

    app = create_celery_app()

    # Task loss on worker death is unacceptable for multi-minute stages.
    assert app.conf.task_acks_late is True
    # Long tasks + prefetch>1 = head-of-line blocking.
    assert app.conf.worker_prefetch_multiplier == 1
    # Bounds memory drift; prefork COW is defeated by CPython refcounting.
    assert app.conf.worker_max_tasks_per_child == 50
    assert app.conf.task_reject_on_worker_lost is True
    # Explicit: silences a CPendingDeprecationWarning in Celery 6.
    assert app.conf.broker_connection_retry_on_startup is True
    assert app.conf.broker_url == REQUIRED_ENV["FDI_BROKER_URL"]
    assert app.conf.result_backend == REQUIRED_ENV["FDI_RESULT_BACKEND"]
    # Concurrency must come from config, not a CLI flag -- otherwise
    # FDI_WORKER_CONCURRENCY looks configurable while being silently overridden.
    assert app.conf.worker_concurrency == 2


def test_task_discovery_is_deferred_not_forced() -> None:
    """`force=True` is not a stronger version of this call. It is a circular import.

    `app.py` ends with a module-level `app = create_celery_app()`, and `tasks.py`
    must do `from fdi.workers.app import app` to reach the decorator. `force=True`
    discovers IMMEDIATELY, so it imports `fdi.workers.tasks` while `fdi.workers.app`
    is still executing its own module body -- before the name `app` is ever bound.
    Measured, with a tasks.py present:

        ImportError: cannot import name 'app' from partially initialized module
        'fdi.workers.app' (most likely due to a circular import)

    and nothing swallows it. Celery's `find_related_module` (loaders/base.py:271)
    catches only `ModuleNotFoundError`; a circular import raises a plain
    `ImportError`, which is its PARENT class, so the `except` does not match. So
    `import fdi.workers.app` fails outright -- meaning `celery -A fdi.workers.app`
    cannot boot at all. It is a loud failure, not a silent one, but it is total.

    Lazy -- the default, and the reason `force` defaults to False -- is what makes
    tasks.py importable: discovery is deferred onto the `import_modules` signal and
    runs at `loader.import_default_modules()`, i.e. worker boot, by which time `app`
    is bound. Verified that the task genuinely registers that way; deferring must
    not quietly become never, or we trade an ImportError for `Received unregistered
    task of type 'fdi.ping_job'`.

    This asserts the deferral on the REAL app: `force=True` returns before ever
    connecting the signal, so the listener is absent.
    """
    from celery import signals

    from fdi.workers.app import create_celery_app

    app = create_celery_app()

    assert signals.import_modules.has_listeners(app), (
        "autodiscover_tasks() did not defer -- it discovered eagerly. That "
        "imports fdi.workers.tasks during fdi.workers.app's own module body, "
        "which is a circular import: tasks.py does `from fdi.workers.app import "
        "app` and `app` is not bound yet."
    )


def test_worker_concurrency_honours_the_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FDI_WORKER_CONCURRENCY", "7")
    from fdi.platform.config import get_settings

    get_settings.cache_clear()
    from fdi.workers.app import create_celery_app

    assert create_celery_app().conf.worker_concurrency == 7
