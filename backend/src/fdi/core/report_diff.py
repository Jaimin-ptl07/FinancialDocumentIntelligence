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
retrieval-only run's items are not answers. A mismatch is refused, naming
both values, unless forced; a forced diff records each mismatch so its
reader sees it. The mode is read from the report's top level, else its
stamp; two reports that carry no mode compare.

Pure (`fdi.core` law): no I/O, deterministic.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

Change = Literal["fixed", "regressed", "unchanged", "only_old", "only_new"]


class ReportDiffError(ValueError):
    """Two reports that cannot be compared, and exactly why."""


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
    `mismatches` is empty unless the diff was forced past one."""

    old_version: str | None
    new_version: str | None
    old_mode: str | None
    new_mode: str | None
    items: tuple[ItemChange, ...]
    by_kind: dict[str, KindDiff]
    overall: KindDiff
    mismatches: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Outcome:
    kind: str
    status: str | None
    completed: bool


def diff_reports(
    old: Mapping[str, object], new: Mapping[str, object], *, force: bool = False
) -> ReportDiff:
    """Raises `ReportDiffError` for a report it cannot read, or for reports
    of different benchmark versions or modes unless `force`."""
    old_items, new_items = _outcomes("OLD", old), _outcomes("NEW", new)
    old_version, new_version = _version(old), _version(new)
    old_mode, new_mode = _mode(old), _mode(new)

    mismatches: list[str] = []
    if old_version != new_version:
        mismatches.append(f"reports are from different benchmark versions: "
                          f"OLD {old_version!r}, NEW {new_version!r}")
    if old_mode != new_mode:
        mismatches.append(f"reports are of different modes: OLD {old_mode!r}, NEW {new_mode!r}")
    if mismatches and not force:
        raise ReportDiffError("; ".join(mismatches) + " (pass force to compare anyway)")

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
    )


def _count(changes: Sequence[ItemChange]) -> KindDiff:
    kinds = [c.change for c in changes]
    return KindDiff(
        fixed=kinds.count("fixed"), regressed=kinds.count("regressed"),
        unchanged=kinds.count("unchanged"), only_old=kinds.count("only_old"),
        only_new=kinds.count("only_new"),
    )


def _stamp(report: Mapping[str, object]) -> Mapping[str, object]:
    stamp = report.get("stamp")
    return stamp if isinstance(stamp, Mapping) else {}


def _version(report: Mapping[str, object]) -> str | None:
    version = _stamp(report).get("benchmark_version")
    return version if isinstance(version, str) else None


def _mode(report: Mapping[str, object]) -> str | None:
    mode = report.get("mode", _stamp(report).get("mode"))
    return mode if isinstance(mode, str) else None


def _outcomes(which: str, report: object) -> dict[str, _Outcome]:
    """Item id -> outcome, in report order."""
    where = f"{which} report"
    if not isinstance(report, Mapping):
        raise ReportDiffError(f"{where} must be a JSON object")
    raw_items = report.get("items")
    if not isinstance(raw_items, list):
        raise ReportDiffError(f"{where} 'items' must be a list")
    outcomes: dict[str, _Outcome] = {}
    for index, raw in enumerate(raw_items):
        if not isinstance(raw, Mapping):
            raise ReportDiffError(f"{where} item #{index}: must be a JSON object")
        item_id = raw.get("item_id")
        if not isinstance(item_id, str) or not item_id.strip():
            raise ReportDiffError(f"{where} item #{index}: 'item_id' must be a non-empty string")
        if item_id in outcomes:
            raise ReportDiffError(f"{where} item {item_id!r}: duplicate item_id")
        completed = raw.get("completed")
        if not isinstance(completed, bool):
            raise ReportDiffError(f"{where} item {item_id!r}: 'completed' must be true or false")
        kind, status = raw.get("kind"), raw.get("status")
        outcomes[item_id] = _Outcome(
            kind=kind if isinstance(kind, str) else "unknown",
            status=status if isinstance(status, str) else None,
            completed=completed,
        )
    return outcomes
