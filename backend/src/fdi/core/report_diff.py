"""Compare two answer-quality reports item by item.

One entry point, `diff_reports(old, new, force=False) -> ReportDiff`, over
two `report.json` bodies as `scripts/eval_answers.py run` writes them. It
says what a change did beyond one aggregate number: which items it fixed,
which it regressed, which it left alone, per kind.

WHAT FIXED MEANS
----------------
An item's outcome is its `completed` flag -- the task outcome (§16.2):
correct AND backed by a valid citation. FIXED is not completed before and
completed now; REGRESSED the reverse; UNCHANGED the same either way, even
when the status under it moved (wrong -> failed is still a failed task). So
a correct answer that gains its missing citation is fixed, and one that
keeps its number but loses its citation has regressed. An item only one
report ran is ONLY_OLD or ONLY_NEW -- a limited run or a grown benchmark,
not a change in quality.

WHAT IS COMPARABLE
------------------
Two reports compare only when they ran the same benchmark version in the
same mode: an item id in v0 and v1 may not be the same question, and a
retrieval-only run's items are not answers. The diff is still computed, and
each mismatch recorded naming both values; REFUSING it is the caller's
decision (`eval_answers.py diff` refuses unless forced). The mode is read
from the report's top level, else its stamp; two reports that carry no mode
compare.

Never raises (`fdi.core` is total): what cannot be read -- a report that is
not an object, an item with no id or no `completed` flag -- is left out
and named in `problems`, so a caller can refuse rather than trust a diff
that silently lost items.

Pure (`fdi.core` law): no I/O, deterministic.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

Change = Literal["fixed", "regressed", "unchanged", "only_old", "only_new"]


@dataclass(frozen=True)
class ItemChange:
    """One item's change. A status is None in the report that did not run
    the item."""

    item_id: str
    kind: str
    change: Change
    old_status: str | None
    new_status: str | None


@dataclass(frozen=True)
class KindDiff:
    """Change counts over one kind's items, or the whole diff's."""

    fixed: int
    regressed: int
    unchanged: int
    only_old: int
    only_new: int

    @property
    def net(self) -> int:
        """Tasks completed gained, over items both reports ran."""
        return self.fixed - self.regressed


@dataclass(frozen=True)
class ReportDiff:
    """Items in OLD's order, then the items only NEW ran in NEW's order.
    `mismatches` names a benchmark-version or mode difference; `problems`
    what could not be read. Both are empty for a clean comparison."""

    old_version: str | None
    new_version: str | None
    old_mode: str | None
    new_mode: str | None
    items: tuple[ItemChange, ...]
    by_kind: dict[str, KindDiff]
    overall: KindDiff
    mismatches: tuple[str, ...] = ()
    problems: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Outcome:
    kind: str
    status: str | None
    completed: bool


def diff_reports(old: object, new: object) -> ReportDiff:
    """The item-by-item diff of two `report.json` bodies, whatever they hold."""
    problems: list[str] = []
    old_items, new_items = _outcomes("OLD", old, problems), _outcomes("NEW", new, problems)
    old_version, new_version = _version(old), _version(new)
    old_mode, new_mode = _mode(old), _mode(new)

    mismatches: list[str] = []
    if old_version != new_version:
        mismatches.append(f"reports are from different benchmark versions: "
                          f"OLD {old_version!r}, NEW {new_version!r}")
    if old_mode != new_mode:
        mismatches.append(f"reports are of different modes: OLD {old_mode!r}, NEW {new_mode!r}")

    changes: list[ItemChange] = []
    for item_id, was in old_items.items():
        now = new_items.get(item_id)
        if now is None:
            changes.append(ItemChange(item_id, was.kind, "only_old", was.status, None))
            continue
        change: Change = (
            "unchanged" if was.completed == now.completed
            else "fixed" if now.completed else "regressed"
        )
        changes.append(ItemChange(item_id, now.kind, change, was.status, now.status))
    changes += [
        ItemChange(item_id, now.kind, "only_new", None, now.status)
        for item_id, now in new_items.items() if item_id not in old_items
    ]
    kinds = sorted({c.kind for c in changes})
    return ReportDiff(
        old_version=old_version, new_version=new_version,
        old_mode=old_mode, new_mode=new_mode,
        items=tuple(changes),
        by_kind={k: _count([c for c in changes if c.kind == k]) for k in kinds},
        overall=_count(changes),
        mismatches=tuple(mismatches),
        problems=tuple(problems),
    )


def _count(changes: Sequence[ItemChange]) -> KindDiff:
    kinds = [c.change for c in changes]
    return KindDiff(
        fixed=kinds.count("fixed"), regressed=kinds.count("regressed"),
        unchanged=kinds.count("unchanged"), only_old=kinds.count("only_old"),
        only_new=kinds.count("only_new"),
    )


def _stamp(report: object) -> Mapping[str, object]:
    stamp = report.get("stamp") if isinstance(report, Mapping) else None
    return stamp if isinstance(stamp, Mapping) else {}


def _version(report: object) -> str | None:
    version = _stamp(report).get("benchmark_version")
    return version if isinstance(version, str) else None


def _mode(report: object) -> str | None:
    top = report.get("mode") if isinstance(report, Mapping) else None
    mode = top if top is not None else _stamp(report).get("mode")
    return mode if isinstance(mode, str) else None


def _outcomes(which: str, report: object, problems: list[str]) -> dict[str, _Outcome]:
    """Item id -> outcome, in report order. What cannot be read is left out
    and appended to `problems`."""
    where = f"{which} report"
    if not isinstance(report, Mapping):
        problems.append(f"{where} must be a JSON object")
        return {}
    raw_items = report.get("items")
    if not isinstance(raw_items, list):
        problems.append(f"{where} 'items' must be a list")
        return {}
    outcomes: dict[str, _Outcome] = {}
    for index, raw in enumerate(raw_items):
        if not isinstance(raw, Mapping):
            problems.append(f"{where} item #{index}: must be a JSON object")
            continue
        item_id = raw.get("item_id")
        if not isinstance(item_id, str) or not item_id.strip():
            problems.append(f"{where} item #{index}: 'item_id' must be a non-empty string")
            continue
        if item_id in outcomes:
            problems.append(f"{where} item {item_id!r}: duplicate item_id")
            continue
        completed = raw.get("completed")
        if not isinstance(completed, bool):
            problems.append(f"{where} item {item_id!r}: 'completed' must be true or false")
            continue
        kind, status = raw.get("kind"), raw.get("status")
        outcomes[item_id] = _Outcome(
            kind=kind if isinstance(kind, str) else "unknown",
            status=status if isinstance(status, str) else None,
            completed=completed,
        )
    return outcomes
