"""The stale-Field planner (S3): which Field x Report pairs a run reads.

Driven through its one entry point, `plan_extraction`, asserting only what
it decides (action + reason per pair) -- never how."""

from fdi.core.extraction_plan import (
    EngineVersions,
    PlannedField,
    StoredRead,
    cache_key,
    plan_extraction,
    summarize,
)

ENGINE = EngineVersions(prompt_version="p1", retrieval_version="r1", model="m1")


def _read(field_id: str, *, doc: str = "d1", hash_: str = "h", status: str = "extracted",
          prompt: str = "p1", retrieval: str = "r1", model: str = "m1",
          read_id: str | None = None) -> StoredRead:
    return StoredRead(read_id=read_id or f"read-{field_id}-{doc}-{hash_}", doc_id=doc,
                      field_id=field_id, definition_hash=hash_, prompt_version=prompt,
                      retrieval_version=retrieval, model=model, status=status)


def _decisions(plan) -> dict[tuple[str, str], tuple[str, str]]:
    return {(i.doc_id, i.field_id): (i.action, i.reason) for i in plan.items}


def test_a_new_field_is_the_only_one_planned() -> None:
    fields = [PlannedField("a", "h"), PlannedField("b", "h"), PlannedField("new", "h")]
    plan = plan_extraction(fields=fields, doc_ids=["d1"], engine=ENGINE,
                           reads=[_read("a"), _read("b")])
    assert _decisions(plan) == {
        ("d1", "a"): ("reuse", "fresh"),
        ("d1", "b"): ("reuse", "fresh"),
        ("d1", "new"): ("extract", "missing"),
    }
    assert [i.field_id for i in plan.to_extract()] == ["new"]


def test_a_fresh_read_is_reused_by_its_id() -> None:
    plan = plan_extraction(fields=[PlannedField("a", "h")], doc_ids=["d1"], engine=ENGINE,
                           reads=[_read("a", read_id="R1")])
    assert plan.reused()[0].read_id == "R1"


def test_a_reworded_field_is_the_only_one_reread() -> None:
    fields = [PlannedField("a", "h"), PlannedField("b", "h2")]
    plan = plan_extraction(fields=fields, doc_ids=["d1"], engine=ENGINE,
                           reads=[_read("a"), _read("b", hash_="h1")])
    assert _decisions(plan) == {
        ("d1", "a"): ("reuse", "fresh"),
        ("d1", "b"): ("extract", "definition_changed"),
    }


def test_reverting_a_field_reuses_the_read_of_that_definition() -> None:
    """Both definitions' reads stay live; going back costs nothing."""
    reads = [_read("a", hash_="h2"), _read("a", hash_="h1", read_id="OLD")]
    plan = plan_extraction(fields=[PlannedField("a", "h1")], doc_ids=["d1"], engine=ENGINE,
                           reads=reads)
    assert plan.items[0].action == "reuse"
    assert plan.items[0].read_id == "OLD"


def test_an_engine_change_makes_every_field_stale_with_its_reason() -> None:
    fields = [PlannedField("a", "h")]
    for engine, reason in (
        (EngineVersions("p2", "r1", "m1"), "prompt_changed"),
        (EngineVersions("p1", "r2", "m1"), "retrieval_changed"),
        (EngineVersions("p1", "r1", "m2"), "model_changed"),
    ):
        plan = plan_extraction(fields=fields, doc_ids=["d1"], engine=engine,
                               reads=[_read("a")])
        assert _decisions(plan) == {("d1", "a"): ("extract", reason)}


def test_forced_fields_are_read_again_even_when_fresh() -> None:
    fields = [PlannedField("a", "h"), PlannedField("b", "h")]
    plan = plan_extraction(fields=fields, doc_ids=["d1"], engine=ENGINE,
                           reads=[_read("a"), _read("b")], forced=["b"])
    assert _decisions(plan) == {("d1", "a"): ("reuse", "fresh"),
                                ("d1", "b"): ("extract", "forced")}


def test_a_previously_failed_read_is_retried() -> None:
    plan = plan_extraction(fields=[PlannedField("a", "h")], doc_ids=["d1"], engine=ENGINE,
                           reads=[_read("a", status="failed")])
    assert _decisions(plan) == {("d1", "a"): ("extract", "previously_failed")}


def test_a_not_found_read_is_fresh() -> None:
    """"Not in this Report" is an answer, and it is not re-paid for."""
    plan = plan_extraction(fields=[PlannedField("a", "h")], doc_ids=["d1"], engine=ENGINE,
                           reads=[_read("a", status="not_found")])
    assert plan.items[0].action == "reuse"


def test_derived_fields_are_computed_never_read() -> None:
    plan = plan_extraction(fields=[PlannedField("t", "h", origin="derived")],
                           doc_ids=["d1"], engine=ENGINE, reads=[])
    assert _decisions(plan) == {("d1", "t"): ("derive", "derived")}


def test_a_removed_field_is_not_planned_and_its_reads_are_ignored() -> None:
    plan = plan_extraction(fields=[PlannedField("a", "h")], doc_ids=["d1"], engine=ENGINE,
                           reads=[_read("a"), _read("removed")])
    assert [i.field_id for i in plan.items] == ["a"]


def test_reads_are_per_report() -> None:
    fields = [PlannedField("a", "h")]
    plan = plan_extraction(fields=fields, doc_ids=["d1", "d2", "d3"], engine=ENGINE,
                           reads=[_read("a", doc="d1"), _read("a", doc="d3")])
    assert _decisions(plan) == {
        ("d1", "a"): ("reuse", "fresh"),
        ("d2", "a"): ("extract", "missing"),
        ("d3", "a"): ("reuse", "fresh"),
    }
    assert summarize(plan) == {"fresh": 2, "missing": 1}


def test_repeated_fields_and_reports_are_planned_once() -> None:
    plan = plan_extraction(fields=[PlannedField("a", "h"), PlannedField("a", "h")],
                           doc_ids=["d1", "d1"], engine=ENGINE, reads=[])
    assert len(plan.items) == 1


def test_the_cache_key_covers_every_part_of_the_read() -> None:
    base = cache_key("f", "h", ENGINE)
    assert base == cache_key("f", "h", ENGINE)
    assert len({
        base,
        cache_key("g", "h", ENGINE),
        cache_key("f", "h2", ENGINE),
        cache_key("f", "h", EngineVersions("p2", "r1", "m1")),
        cache_key("f", "h", EngineVersions("p1", "r2", "m1")),
        cache_key("f", "h", EngineVersions("p1", "r1", "m2")),
    }) == 6
