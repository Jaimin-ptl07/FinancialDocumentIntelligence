"""Both new stages read ctx.services. If nothing supplies those keys they
degrade to mapping NOTHING -- silently, which is the failure mode this whole
architecture exists to eliminate."""

import pytest

from fdi.pipeline.stages.fog_mapping import build_mapping
from fdi.platform.config import get_settings

_FOG = {
    "doc_id": "d",
    "bases": {"consolidated": {
        "statements": [], "notes": [
            {"id": "nt:consolidated:1", "ref": "1", "title": "Other expenses"}],
        "line_items": [{"id": "li:a", "parent": "nt:consolidated:1",
                        "origin": "note", "label": "Travel expenses",
                        "current": 1.0}],
        "edges": [],
    }},
}


def test_an_absent_schema_maps_nothing_and_says_so():
    """Degrading is correct; degrading INVISIBLY is not."""
    body = build_mapping(_FOG, {}, client=None, store=None)
    d = body["diagnostics"]["consolidated"]
    assert d["by_lexical"] == 0
    assert d["by_llm"] == 0
    assert d["unmapped"] == 1


def test_an_absent_client_still_runs_the_free_tiers():
    schema = {"modules": [{
        "module_id": "profit_and_loss", "statement": "profit_and_loss",
        "retrieval_units": [{"unit_id": "RU", "fields": [
            {"tag": "TCEXP", "field_name": "Travel and conveyance",
             "origin": "extracted", "aliases": ["travel expenses"]},
        ]}],
    }]}
    body = build_mapping(_FOG, schema, client=None, store=None)
    assert body["diagnostics"]["consolidated"]["by_lexical"] == 1
    assert body["diagnostics"]["consolidated"]["llm_calls"] == 0


class _FakeEngine:
    """Stands in for the real AsyncEngine `_run_stage_async` builds. Never
    connects -- `dispose()` is the only thing called on it in this test, since
    `run_stage` itself is monkeypatched away below."""

    async def dispose(self) -> None:
        return None


class _FakeMinio:
    """Duck-types just the two Minio calls `ArtifactStore.ensure_bucket` makes,
    so the test never opens a real socket."""

    def bucket_exists(self, bucket: str) -> bool:
        return True

    def make_bucket(self, bucket: str) -> None:
        return None


async def test_services_carry_schema_and_extractor_when_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Proves the WIRING, not the degradation contract above: when
    `load_schema` succeeds and a configured GPT endpoint/key is available,
    `services` (the dict `_run_stage_async` hands to `run_stage`) carries
    both "schema" and "extractor" keys.

    `load_schema` itself is mocked -- Task 4/schema_store's own I/O
    (store-vs-seed fallback) is not this test's concern, only that
    `_run_stage_async` calls it and threads its result through under the
    "schema" key `fog_mapping`/`fog_reason` read."""
    for key, value in {
        "FDI_DATABASE_URL": "postgresql+asyncpg://stub:stub@127.0.0.1:1/stub",
        "FDI_BROKER_URL": "amqp://guest:guest@localhost:5672/%2F",
        "FDI_RESULT_BACKEND": "redis://localhost:6379/0",
        "FDI_S3_ENDPOINT": "localhost:9000",
        "FDI_S3_ACCESS_KEY": "minioadmin",
        "FDI_S3_SECRET_KEY": "minioadmin",
        "FDI_AUTH_SECRET": "test-signing-secret-0123456789abcdef0123456789abcdef",
        "FDI_GPT_ENDPOINT": "https://example.invalid",
        "FDI_GPT_KEY": "key",
    }.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()

    # Imported HERE, after the env above exists, not at module level. The
    # module-level import this replaces only ever worked by accident: it
    # builds Settings inside fdi.workers.app's module body, and the one thing
    # that satisfied Settings at COLLECTION time was pymilvus's import-time
    # load_dotenv() picking up the developer's repo-root .env -- absent on CI,
    # and insufficient the moment auth_secret became required. Same pattern
    # tests/integration/conftest.py's stub_fdi_env documents.
    import fdi.workers.tasks as tasks_mod

    monkeypatch.setattr(tasks_mod, "make_engine", lambda url: _FakeEngine())
    monkeypatch.setattr(tasks_mod, "make_session_factory", lambda engine: object())

    schema_doc = {"modules": []}

    async def fake_load_schema(*, store: object, seed_path: str) -> tuple[dict, str]:
        return schema_doc, "seed"

    monkeypatch.setattr(tasks_mod, "load_schema", fake_load_schema)

    captured: dict[str, object] = {}

    async def fake_run_stage(**kwargs: object) -> list[str]:
        captured.update(kwargs)
        return []

    monkeypatch.setattr(tasks_mod, "run_stage", fake_run_stage)

    try:
        await tasks_mod._run_stage_async(
            "doc1", "fog_mapping", "run1", "corr1", client=_FakeMinio()
        )
    finally:
        get_settings.cache_clear()

    services = captured["services"]
    assert isinstance(services, dict)
    assert services["schema"] == schema_doc
    assert "extractor" in services
