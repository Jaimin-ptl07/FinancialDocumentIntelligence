"""`diff_reports` is the only seam: every test states two `report.json`
bodies, as `eval_answers.py run` writes them, and what the diff must say."""

import pytest

from fdi.core.report_diff import ItemChange, diff_reports


def _item(item_id: str, completed: bool, kind: str = "fact_lookup",
          status: str | None = None) -> dict[str, object]:
    return {"item_id": item_id, "kind": kind,
            "status": status or ("correct" if completed else "wrong"), "completed": completed}


def _report(*items: dict[str, object], version: str = "v0", mode: str | None = "answers",
            **extra: object) -> dict[str, object]:
    stamp: dict[str, object] = {"benchmark_version": version, "git_commit": "abc"}
    if mode is not None:
        stamp["mode"] = mode
    return {"stamp": stamp, "items": list(items), **extra}


def test_items_are_fixed_regressed_or_unchanged_by_task_completion() -> None:
    old = _report(_item("a", False), _item("b", True), _item("c", True), _item("d", False),
                  _item("gone", True))
    new = _report(_item("a", True), _item("b", False, status="failed"), _item("c", True),
                  _item("d", False, status="failed"), _item("added", False))
    diff = diff_reports(old, new)
    assert diff.items == (
        ItemChange("a", "fact_lookup", "fixed", "wrong", "correct"),
        ItemChange("b", "fact_lookup", "regressed", "correct", "failed"),
        ItemChange("c", "fact_lookup", "unchanged", "correct", "correct"),
        # Still not completed: unchanged, though its status moved.
        ItemChange("d", "fact_lookup", "unchanged", "wrong", "failed"),
        ItemChange("gone", "fact_lookup", "only_old", "correct", None),
        ItemChange("added", "fact_lookup", "only_new", None, "wrong"),
    )


def test_a_correct_answer_that_gains_a_valid_citation_is_fixed() -> None:
    """Completion, not correctness, is the outcome: correct-but-unsupported
    becoming correct-and-cited is a fixed task."""
    old = _report(_item("a", False, status="correct"))
    new = _report(_item("a", True, status="correct"))
    assert diff_reports(old, new).items[0].change == "fixed"


def test_the_summary_counts_changes_per_kind_and_overall() -> None:
    old = _report(_item("f0", False), _item("f1", True), _item("f2", False),
                  _item("r0", True, kind="research"), _item("r1", True, kind="research"))
    new = _report(_item("f0", True), _item("f1", True), _item("f2", True),
                  _item("r0", False, kind="research"), _item("c0", True, kind="cross_company"))
    diff = diff_reports(old, new)

    fact = diff.by_kind["fact_lookup"]
    assert (fact.fixed, fact.regressed, fact.unchanged, fact.only_old, fact.only_new) == (
        2, 0, 1, 0, 0)
    assert fact.net == 2
    research = diff.by_kind["research"]
    assert (research.fixed, research.regressed, research.only_old) == (0, 1, 1)
    assert research.net == -1
    assert diff.by_kind["cross_company"].only_new == 1
    assert list(diff.by_kind) == ["cross_company", "fact_lookup", "research"]
    o = diff.overall
    assert (o.fixed, o.regressed, o.unchanged, o.only_old, o.only_new, o.net) == (
        2, 1, 1, 1, 1, 1)


def test_reports_from_different_benchmark_versions_are_a_mismatch_naming_both() -> None:
    """The diff is still computed; refusing it is the caller's decision."""
    diff = diff_reports(_report(_item("a", False), version="v0"),
                        _report(_item("a", True), version="v1"))
    assert diff.mismatches == (
        "reports are from different benchmark versions: OLD 'v0', NEW 'v1'",)
    assert diff.items[0].change == "fixed"
    assert (diff.old_version, diff.new_version) == ("v0", "v1")


def test_reports_of_different_modes_are_a_mismatch() -> None:
    diff = diff_reports(_report(mode="answers"), _report(mode="retrieval"))
    assert diff.mismatches == (
        "reports are of different modes: OLD 'answers', NEW 'retrieval'",)


def test_a_top_level_mode_is_read_before_the_stamps() -> None:
    assert diff_reports({**_report(mode=None), "mode": "answers"},
                        _report(mode="answers")).mismatches == ()
    assert diff_reports({**_report(mode="answers"), "mode": "retrieval"},
                        _report(mode="answers")).mismatches


def test_reports_that_both_carry_no_mode_compare() -> None:
    assert diff_reports(_report(mode=None), _report(mode=None)).mismatches == ()


@pytest.mark.parametrize(
    ("old", "new", "reason"),
    [
        ([], _report(), "OLD report must be a JSON object"),
        (_report(), {"stamp": {"benchmark_version": "v0", "mode": "answers"}},
         "NEW report 'items' must be a list"),
        (_report({"kind": "x", "completed": True}), _report(),
         "OLD report item #0: 'item_id' must be a non-empty string"),
        (_report(), _report({"item_id": "a", "kind": "x", "status": "correct"}),
         "NEW report item 'a': 'completed' must be true or false"),
        (_report(_item("a", True), _item("a", False)), _report(),
         "OLD report item 'a': duplicate item_id"),
    ],
)
def test_what_the_diff_cannot_read_is_a_problem_with_its_reason_never_an_exception(
    old: object, new: object, reason: str,
) -> None:
    diff = diff_reports(old, new)  # type: ignore[arg-type]
    assert diff.problems == (reason,)


def test_an_unreadable_item_is_left_out_and_the_rest_still_diff() -> None:
    old = _report(_item("a", False), {"item_id": "b", "kind": "x"})
    diff = diff_reports(old, _report(_item("a", True), _item("b", True)))
    assert [(c.item_id, c.change) for c in diff.items] == [("a", "fixed"), ("b", "only_new")]
    assert len(diff.problems) == 1
