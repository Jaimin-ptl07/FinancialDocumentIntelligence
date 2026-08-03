"""Every ground-truth file on disk must parse. A malformed file silently skips
during scoring, which would quietly shrink the denominator and inflate recall.

This guard is deliberately landed BEFORE the corpus. `parse_groundtruth` returns
None rather than raising, so a typo in one file does not take a scoring run
down -- it just removes that file's tags from the denominator, and the run still
prints a number. A number computed over a corpus that quietly lost a file is
worse than no number, which is exactly what this test exists to prevent.

The corpus itself (plan Task 3) is hand data entry against the real PDFs and is
a pending HUMAN deliverable. Until it lands, the parametrised tests collect zero
cases and `test_the_corpus_is_not_empty` skips with the reason -- it must never
report green as if the corpus had been checked, and it must never fail the suite
for work no automation can do.
"""

import json
from pathlib import Path

import pytest

from fdi.core.scoring import parse_groundtruth

_GT_DIR = Path(__file__).resolve().parents[3] / "docs" / "groundtruth"


def _files() -> list[Path]:
    """Collection-time safe: an absent directory yields no cases, not an error."""
    return sorted(_GT_DIR.glob("*.json")) if _GT_DIR.is_dir() else []


@pytest.mark.parametrize("path", _files(), ids=lambda p: p.name)
def test_ground_truth_file_parses(path: Path) -> None:
    body = json.loads(path.read_text(encoding="utf-8"))
    gt = parse_groundtruth(body)
    assert gt is not None, (
        f"{path.name} is malformed (check values/not_reported disjointness)"
    )


@pytest.mark.parametrize("path", _files(), ids=lambda p: p.name)
def test_filename_matches_its_contents(path: Path) -> None:
    """A file keyed for the wrong basis scores the wrong statement set and the
    error is invisible in the numbers."""
    body = json.loads(path.read_text(encoding="utf-8"))
    gt = parse_groundtruth(body)
    assert gt is not None
    assert path.stem == f"{gt.issuer}-{gt.fiscal_year}-{gt.basis}-{gt.statement}"


def test_the_corpus_is_not_empty() -> None:
    if not _files():
        pytest.skip(
            "no ground-truth files under docs/groundtruth/ -- plan Task 3 is a "
            "human deliverable and is NOT done. Every later gate is measured "
            "against this corpus; until it lands there is no baseline."
        )
    assert _files()
