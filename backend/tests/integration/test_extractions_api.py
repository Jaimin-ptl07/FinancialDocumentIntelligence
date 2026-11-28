"""`/api/v1/extractions` and Extracted values (S3), end to end.

The worker is played eagerly (`execute_job`, as test_api.py's `run_job`)
against the same fake model the routes were served, so every assertion about
"which Fields reached the model" is about the real queued path: route ->
api_job -> execute_job -> execute_run -> engine."""

from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from fdi.pipeline.embeddings import FakeEmbeddingClient
from fdi.pipeline.vectorstore import FakeVectorStore
from fdi.platform.config import get_settings
from tests.conftest import TEST_SIGNING_SECRET
from tests.integration.conftest import authenticate, job_services
from tests.integration.extraction_support import AnsweringClient, indexed_report

pytestmark = pytest.mark.integration

SCORE_PIPELINE = Path(__file__).resolve().parents[3] / "scripts" / "score_pipeline.py"
SCHEMAS = "/api/v1/extraction-schemas"
RUNS = "/api/v1/extractions"


@pytest.fixture
def fakes() -> tuple[FakeEmbeddingClient, FakeVectorStore]:
    return FakeEmbeddingClient(dim=8), FakeVectorStore()


@pytest.fixture
async def api(postgres_url, minio_endpoint, amqp_url, redis_url, monkeypatch, fakes):
    """`api(creds, extractor)` -> a logged-in client whose app is served
    `extractor` (None: unconfigured) and the shared fake retrieval."""
    embedder, vector_store = fakes

    @asynccontextmanager
    async def _make(creds, extractor):
        for key, value in {
            "FDI_DATABASE_URL": postgres_url, "FDI_BROKER_URL": amqp_url,
            "FDI_RESULT_BACKEND": redis_url, "FDI_S3_ENDPOINT": minio_endpoint,
            "FDI_S3_ACCESS_KEY": "minioadmin", "FDI_S3_SECRET_KEY": "minioadmin",
            "FDI_S3_BUCKET": "test-artifacts", "FDI_AUTH_SECRET": TEST_SIGNING_SECRET,
        }.items():
            monkeypatch.setenv(key, value)
        get_settings.cache_clear()
        from fdi.api.app import create_app
        from fdi.api.deps import get_embedder, get_extractor_client, get_vector_store

        app = create_app()
        app.dependency_overrides[get_extractor_client] = lambda: extractor
        app.dependency_overrides[get_embedder] = lambda: embedder
        app.dependency_overrides[get_vector_store] = lambda: vector_store
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        ):
            await authenticate(client, creds)
            yield client

    return _make


@pytest.fixture
async def reports(session_factory, store, fakes) -> list[str]:
    embedder, vector_store = fakes
    return [await indexed_report(session_factory, store, embedder=embedder,
                                 vector_store=vector_store) for _ in range(3)]


async def _work(session_factory, store, fakes, accepted, extractor) -> None:
    """Play the worker for one queued run."""
    from fdi.pipeline.jobs import execute_job

    embedder, vector_store = fakes
    await execute_job(accepted["job_id"], job_services(
        session_factory, store, extractor_client=extractor, embedder=embedder,
        vector_store=vector_store,
    ))


async def _publish(client, name: str, fields: list[dict], groups: list[dict] | None = None,
                   copy_from: str | None = None) -> dict:
    body: dict = {"name": name}
    if copy_from:
        body["copy_from"] = {"schema_id": copy_from}
    schema = (await client.post(SCHEMAS, json=body)).json()
    draft = (await client.get(f"{SCHEMAS}/{schema['id']}/draft")).json()
    if fields or groups:
        put = await client.put(f"{SCHEMAS}/{schema['id']}/draft", json={
            "revision": draft["revision"], "fields": draft["fields"] + fields,
            "groups": draft["groups"] + (groups or []),
        })
        assert put.status_code == 200, put.text
        draft = put.json()
    v = await client.post(f"{SCHEMAS}/{schema['id']}/draft/publish",
                          json={"revision": draft["revision"]})
    assert v.status_code == 201, v.text
    return v.json()


TOTALS = [
    {"name": "Revenue", "tag": "REV", "group": "pl"},
    {"name": "Other income", "tag": "OI", "group": "pl"},
    {"name": "Total income", "tag": "TI", "origin": "derived",
     "derivation": {"inputs": ["REV", "OI"]}},
    {"name": "Auditor", "tag": "AUD", "data_type": "string", "group": "front"},
]
GROUPS = [
    {"key": "pl", "title": "Profit and Loss", "retrieval_query": "Revenue from operations"},
    {"key": "front", "title": "Front matter", "retrieval_query": "statutory auditors"},
]


async def _run(client, body: dict) -> dict:
    r = await client.post(RUNS, json=body)
    assert r.status_code == 202, r.text
    return r.json()


async def test_acceptance_adding_one_field_to_a_standard_copy_reads_only_it(
    api, invitee, reports, session_factory, store, fakes
) -> None:
    """Design §17.2: add one Field to a copy of the standard schema, extract
    it from three Reports of one Company -- the model is called only for it."""
    model = AnsweringClient()
    async with api(invitee, model) as client:
        standard = next(s for s in (await client.get(SCHEMAS)).json()["schemas"]
                        if s["is_standard"])
        first = await _run(client, {"schema_id": standard["id"], "report_ids": reports})
        await _work(session_factory, store, fakes, first, model)
        done = (await client.get(f"{RUNS}/{first['run_id']}")).json()
        standard_calls = len(model.calls)

        copy = await _publish(client, "Standard + capex", [{
            "name": "Capital expenditure", "tag": "CAPEXP", "group": "CF",
            "description": "Purchase of property, plant and equipment",
        }], copy_from=standard["id"])
        plan = (await client.post(f"{RUNS}/plan", json={
            "schema_id": copy["schema_id"], "report_ids": reports,
        })).json()
        model.calls.clear()
        second = await _run(client, {"schema_id": copy["schema_id"], "report_ids": reports})
        await _work(session_factory, store, fakes, second, model)
        run = (await client.get(f"{RUNS}/{second['run_id']}")).json()
        results = (await client.get(f"{RUNS}/{second['run_id']}/results")).json()

    assert done["status"] == "succeeded"
    assert standard_calls == 5 * 3, "one call per statement group per Report"
    assert plan["to_extract"] == 3
    assert {c["reason"] for c in plan["cells"] if c["action"] == "extract"} == {"missing"}
    assert model.calls == [["CAPEXP"]] * 3, "only the new Field reached the model"
    assert run["status"] == "succeeded"
    extracted = copy["field_count"] - sum(1 for f in copy["fields"]
                                          if f["origin"] == "derived")
    for report in run["reports"]:
        assert report["counts"]["reused"] == extracted - 1
        assert report["counts"]["extracted"] == 1
    capex = next(c for c in results["columns"] if c["tag"] == "CAPEXP")
    for row in results["rows"]:
        cell = next(v for v in row["values"] if v["field_id"] == capex["field_id"])
        assert cell["value"] == 1.0 and cell["read_id"]


async def test_results_review_and_derived_totals(
    api, invitee, reports, session_factory, store, fakes
) -> None:
    model = AnsweringClient({"REV": 100.0, "OI": 5.0, "AUD": "Deloitte"})
    async with api(invitee, model) as client:
        version = await _publish(client, "Totals", TOTALS, GROUPS)
        accepted = await _run(client, {"schema_id": version["schema_id"],
                                       "report_ids": reports[:1]})
        await _work(session_factory, store, fakes, accepted, model)
        table = (await client.get(f"{RUNS}/{accepted['run_id']}/results")).json()
        csv = await client.get(f"{RUNS}/{accepted['run_id']}/results?format=csv")

        by_tag = {c["tag"]: c["field_id"] for c in table["columns"]}
        cells = {v["field_id"]: v for v in table["rows"][0]["values"]}
        oi = cells[by_tag["OI"]]
        corrected = await client.post(f"/api/v1/extracted-values/{oi['read_id']}/review",
                                      json={"action": "corrected", "value": 7.0,
                                            "note": "note 21 restates it"})
        bad = await client.post(f"/api/v1/extracted-values/{oi['read_id']}/review",
                                json={"action": "corrected", "value": "seven"})
        accepted_aud = await client.post(
            f"/api/v1/extracted-values/{cells[by_tag['AUD']]['read_id']}/review",
            json={"action": "accepted"},
        )
        report_view = (await client.get(
            f"/api/v1/reports/{reports[0]}/extracted-values",
            params={"schema_id": version["schema_id"]},
        )).json()
        history = (await client.get(
            f"/api/v1/extracted-values/{oi['read_id']}/reviews"
        )).json()

    assert len(model.calls) == 2, "one call per retrieval group; the total is computed"
    assert cells[by_tag["TI"]]["status"] == "derived"
    assert cells[by_tag["TI"]]["value"] == 105.0
    assert cells[by_tag["TI"]]["derived_from"] == ["REV", "OI"]
    assert cells[by_tag["REV"]]["status"] == "extracted"
    assert cells[by_tag["REV"]]["basis"] in ("standalone", "consolidated")
    assert cells[by_tag["REV"]]["provenance"]["page"] == 1
    assert csv.status_code == 200 and csv.headers["content-type"].startswith("text/csv")
    header, row = csv.text.strip().splitlines()
    assert "Total income [TI]" in header and "105.0" in row

    assert corrected.status_code == 200
    assert corrected.json()["status"] == "corrected"
    assert corrected.json()["value"] == 7.0
    assert corrected.json()["value_as_read"] == 5.0, "the value as read is kept"
    assert bad.status_code == 422
    assert accepted_aud.json()["status"] == "accepted"
    after = {v["field_id"]: v for v in report_view["rows"][0]["values"]}
    assert after[by_tag["TI"]]["value"] == 107.0, "a correction carries into the total"
    assert after[by_tag["OI"]]["review"]["note"] == "note 21 restates it"
    assert [h["action"] for h in history["reviews"]] == ["corrected"]


async def test_a_partly_failed_run_is_retried_for_its_failures_only(
    api, invitee, reports, session_factory, store, fakes
) -> None:
    flaky = AnsweringClient(fail=lambda paths: "AUD" in paths)
    async with api(invitee, flaky) as client:
        version = await _publish(client, "Flaky", TOTALS, GROUPS)
        first = await _run(client, {"schema_id": version["schema_id"],
                                    "report_ids": reports[:2]})
        await _work(session_factory, store, fakes, first, flaky)
        failed_run = (await client.get(f"{RUNS}/{first['run_id']}")).json()
        retry = await client.post(f"{RUNS}/{first['run_id']}/retry")
        healthy = AnsweringClient()
        await _work(session_factory, store, fakes, retry.json(), healthy)
        retried = (await client.get(f"{RUNS}/{retry.json()['run_id']}")).json()
        nothing = await client.post(f"{RUNS}/{retry.json()['run_id']}/retry")

    assert failed_run["status"] == "partial"
    assert failed_run["counts"]["failed"] == 2
    assert all(r["status"] == "partial" for r in failed_run["reports"])
    assert retry.status_code == 202
    assert healthy.calls == [["AUD"], ["AUD"]], "only the failed Field, per Report"
    assert retried["status"] == "succeeded" and retried["retry_of"] == first["run_id"]
    assert nothing.status_code == 409


async def test_a_redelivered_run_pays_nothing_twice(
    api, invitee, reports, session_factory, store, fakes
) -> None:
    from sqlalchemy import update

    from fdi.pipeline.extraction_runs import EngineDeps, execute_run
    from fdi.platform.db.models import ExtractionRun, ExtractionRunReport

    model = AnsweringClient()
    async with api(invitee, model) as client:
        version = await _publish(client, "Redelivery", TOTALS, GROUPS)
        accepted = await _run(client, {"schema_id": version["schema_id"],
                                       "report_ids": reports[:2],
                                       "force_field_ids": []})
        await _work(session_factory, store, fakes, accepted, model)
        calls = len(model.calls)
        # The worker "died" after reading but before recording the end: the
        # message is redelivered with the run still running.
        async with session_factory() as s:
            await s.execute(update(ExtractionRun).where(
                ExtractionRun.id == accepted["run_id"]).values(status="running"))
            await s.execute(update(ExtractionRunReport).where(
                ExtractionRunReport.run_id == accepted["run_id"]).values(status="running"))
            await s.commit()
        embedder, vector_store = fakes
        summary = await execute_run(accepted["run_id"], session_factory=session_factory,
                                    deps=EngineDeps(store=store, embedder=embedder,
                                                    vector_store=vector_store, client=model))
        run = (await client.get(f"{RUNS}/{accepted['run_id']}")).json()

    assert len(model.calls) == calls, "the redelivery reused every read"
    assert summary["status"] == "succeeded"
    for report in run["reports"]:
        assert report["counts"].get("extracted") == 3, "still this run's own reads"
        assert "reused" not in report["counts"]


async def test_forcing_a_field_rereads_it(
    api, invitee, reports, session_factory, store, fakes
) -> None:
    model = AnsweringClient()
    async with api(invitee, model) as client:
        version = await _publish(client, "Force", TOTALS, GROUPS)
        rev = next(f["field_id"] for f in version["fields"] if f["tag"] == "REV")
        body = {"schema_id": version["schema_id"], "report_ids": reports[:1]}
        await _work(session_factory, store, fakes, await _run(client, body), model)
        model.calls.clear()
        await _work(session_factory, store, fakes,
                    await _run(client, {**body, "force_field_ids": [rev]}), model)
    assert model.calls == [["REV"]]


async def test_runs_are_private_and_requests_are_checked(
    api, invitee, another_invitee, reports, session_factory, store, fakes
) -> None:
    from fdi.pipeline.intake import intake_document

    unindexed = await intake_document(
        pdf=b"%PDF-1.7\n%unindexed\n" + reports[0].encode(), filename="u.pdf",
        doc_key="unindexed", content_type="application/pdf",
        session_factory=session_factory, store=store,
    )
    model = AnsweringClient()
    async with api(invitee, model) as client:
        version = await _publish(client, "Mine", TOTALS, GROUPS)
        sid = version["schema_id"]
        accepted = await _run(client, {"schema_id": sid, "report_ids": reports[:1]})
        unknown_field = await client.post(RUNS, json={
            "schema_id": sid, "report_ids": reports[:1], "field_ids": ["nope"]})
        unknown_report = await client.post(RUNS, json={
            "schema_id": sid, "report_ids": ["00000000-0000-0000-0000-000000000000"]})
        not_indexed = await client.post(RUNS, json={
            "schema_id": sid, "report_ids": [unindexed.id]})
        listed = (await client.get(RUNS)).json()["runs"]
    async with api(another_invitee, model) as other:
        theirs = await other.get(f"{RUNS}/{accepted['run_id']}")
        # ...but a schema is anyone's to run.
        can_run = await other.post(RUNS, json={"schema_id": sid, "report_ids": reports[:1]})
    async with api(invitee, None) as unconfigured:
        no_model = await unconfigured.post(RUNS, json={"schema_id": sid,
                                                       "report_ids": reports[:1]})

    assert unknown_field.status_code == 422
    assert unknown_report.status_code == 404
    assert not_indexed.status_code == 422
    assert accepted["run_id"] in [r["id"] for r in listed]
    assert theirs.status_code == 404
    assert can_run.status_code == 202
    assert no_model.status_code == 503


async def test_a_run_report_scores_like_an_extraction_record(
    api, invitee, reports, session_factory, store, fakes
) -> None:
    model = AnsweringClient({"REV": 100.0, "OI": 5.0})
    async with api(invitee, model) as client:
        version = await _publish(client, "Scored", TOTALS, GROUPS)
        accepted = await _run(client, {"schema_id": version["schema_id"],
                                       "report_ids": reports[:1]})
        await _work(session_factory, store, fakes, accepted, model)
        record = (await client.get(
            f"{RUNS}/{accepted['run_id']}/reports/{reports[0]}/record"
        )).json()

    import importlib.util

    script = SCORE_PIPELINE
    spec = importlib.util.spec_from_file_location("score_pipeline", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    facts = module.facts_from_extraction(record)
    assert facts == {"REV": 100.0, "OI": 5.0, "TI": 105.0, "AUD": 1.0}
    assert record["doc_id"] == reports[0]
    assert record["basis"] in ("standalone", "consolidated")


async def test_a_crashed_run_is_failed_and_can_be_retried(
    api, invitee, reports, session_factory, store, fakes, monkeypatch
) -> None:
    """An unexpected crash marks the job failed (terminal, never redelivered
    to completion), so the run must be failed too -- else it reads `running`
    forever and refuses a retry."""
    import fdi.pipeline.jobs as jobs

    model = AnsweringClient()
    async with api(invitee, model) as client:
        version = await _publish(client, "Crash", TOTALS, GROUPS)
        accepted = await _run(client, {"schema_id": version["schema_id"],
                                       "report_ids": reports[:1]})

        async def crash(*_args, **_kwargs):
            raise RuntimeError("worker lost its database")

        monkeypatch.setattr(jobs, "execute_run", crash)
        with pytest.raises(RuntimeError):
            await _work(session_factory, store, fakes, accepted, model)
        monkeypatch.undo()
        crashed = (await client.get(f"{RUNS}/{accepted['run_id']}")).json()
        retry = await client.post(f"{RUNS}/{accepted['run_id']}/retry")
        await _work(session_factory, store, fakes, retry.json(), model)
        retried = (await client.get(f"{RUNS}/{retry.json()['run_id']}")).json()

    assert crashed["status"] == "failed"
    assert "worker lost its database" in crashed["error"]
    assert retry.status_code == 202
    assert retried["status"] == "succeeded"
    assert sorted(p for call in model.calls for p in call) == ["AUD", "OI", "REV"]
