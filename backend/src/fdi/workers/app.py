"""The Celery application.

Prefork only. gevent/eventlet cannot parallelize CPU-bound stages (monkey-
patching makes only I/O yield), starve the broker heartbeat into redelivery,
and per Celery's docs cannot enforce time limits on blocking tasks. The pool is
set on the compose command line (`--pool=prefork`), not here -- to read it back
you need `docker compose ps --no-trunc`, since plain `docker compose ps`
truncates COMMAND to about 20 characters ("/bin/sh -c 'celery ...").

Note the asymmetry with worker_concurrency below, which is config-only and
deliberately absent from that command line. It is not an inconsistency: the pool
is FIXED (Global Constraint 5), so a CLI flag is not a competing source of truth
for it -- nothing suggests it is tunable. Concurrency IS tunable via
FDI_WORKER_CONCURRENCY, so a CLI flag there would silently win over the env var
and make it a lie.
"""

from celery import Celery

from fdi.platform.config import get_settings


def create_celery_app() -> Celery:
    settings = get_settings()
    app = Celery("fdi")
    app.conf.update(
        broker_url=settings.broker_url,
        result_backend=settings.result_backend,
        # Single source of truth for concurrency. The compose worker command
        # deliberately carries NO --concurrency flag: a CLI flag silently wins over
        # config, which would make FDI_WORKER_CONCURRENCY look configurable while
        # doing nothing.
        worker_concurrency=settings.worker_concurrency,
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1,
        worker_max_tasks_per_child=50,
        broker_connection_retry_on_startup=True,
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        timezone="UTC",
        enable_utc=True,
        task_track_started=True,
    )
    # Lazy, NOT force=True. force=True discovers immediately, which imports
    # fdi.workers.tasks from inside this module's own body -- while `app` below is
    # still an unbound name. tasks.py does `from fdi.workers.app import app` to
    # reach the decorator, so that is a circular import, and it is fatal:
    #
    #   ImportError: cannot import name 'app' from partially initialized module
    #   'fdi.workers.app' (most likely due to a circular import)
    #
    # Nothing catches it -- Celery's find_related_module (loaders/base.py:271)
    # excepts ModuleNotFoundError, and a circular import raises its parent class,
    # plain ImportError. `import fdi.workers.app` then fails outright and
    # `celery -A fdi.workers.app` cannot boot. Deferring is the whole reason
    # `force` defaults to False: discovery runs on the `import_modules` signal at
    # worker boot, when `app` exists. Pinned by test_task_discovery_is_deferred.
    app.autodiscover_tasks(["fdi.workers"])
    return app


app = create_celery_app()
