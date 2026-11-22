"""`diff_reports` is the only seam: every test states two `report.json`
bodies, as `eval_answers.py run` writes them, and what the diff must say."""

import pytest

from fdi.core.report_diff import ItemChange, ReportDiffError, diff_reports


def _item(item_id: str, completed: bool, kind: str = "fact_lookup",
          status: str | None = None) -> dict[str, object]:
    return {"item_id": item_id, "kind": kind,
            "status": status or ("correct" if completed else "wrong"), "completed": completed}


def _report(*items: dict[str, object], version: str = "v0", mode: str | None = "ask",
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


def test_reports_from_different_benchmark_versions_are_refused_naming_both() -> None:
    with pytest.raises(ReportDiffError) as exc:
        diff_reports(_report(version="v0"), _report(version="v1"))
    message = str(exc.value)
    assert "different benchmark versions" in message
    assert "OLD 'v0'" in message and "NEW 'v1'" in message
    assert "force" in message


def test_forcing_compares_different_versions_and_records_the_mismatch() -> None:
    diff = diff_reports(_report(_item("a", False), version="v0"),
                        _report(_item("a", True), version="v1"), force=True)
    assert diff.items[0].change == "fixed"
    assert (diff.old_version, diff.new_version) == ("v0", "v1")
    assert len(diff.mismatches) == 1
    assert "OLD 'v0'" in diff.mismatches[0]


def test_reports_of_different_modes_are_refused_unless_forced() -> None:
    with pytest.raises(ReportDiffError) as exc:
        diff_reports(_report(mode="ask"), _report(mode="retrieval"))
    assert "different modes: OLD 'ask', NEW 'retrieval'" in str(exc.value)
    assert diff_reports(_report(mode="ask"), _report(mode="retrieval"), force=True).mismatches


def test_a_top_level_mode_is_read_before_the_stamps() -> None:
    assert diff_reports({**_report(mode=None), "mode": "ask"}, _report(mode="ask")).mismatches == ()
    with pytest.raises(ReportDiffError):
        diff_reports({**_report(mode="ask"), "mode": "retrieval"}, _report(mode="ask"))


def test_reports_that_both_carry_no_mode_compare() -> None:
    assert diff_reports(_report(mode=None), _report(mode=None)).mismatches == ()


@pytest.mark.parametrize(
    ("old", "new", "reason"),
    [
        ([], _report(), "OLD report must be a JSON object"),
        (_report(), {"stamp": {"benchmark_version": "v0", "mode": "ask"}},
         "NEW report 'items' must be a list"),
        (_report({"kind": "x", "completed": True}), _report(),
         "OLD report item #0: 'item_id' must be a non-empty string"),
        (_report(), _report({"item_id": "a", "kind": "x", "status": "correct"}),
         "NEW report item 'a': 'completed' must be true or false"),
        (_report(_item("a", True), _item("a", False)), _report(),
         "OLD report item 'a': duplicate item_id"),
    ],
)
def test_a_report_the_diff_cannot_read_is_rejected_with_its_reason(
    old: object, new: object, reason: str,
) -> None:
    with pytest.raises(ReportDiffError) as exc:
        diff_reports(old, new)  # type: ignore[arg-type]
    assert str(exc.value) == reason
