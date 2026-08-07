"""A stage's artifact key must move when the code that BUILDS the artifact
moves.

`run.py` composes input_hash from declared deps, `Stage.version` and
`key_material`. None of those sees `fdi.core` -- yet `fdi.core` is where every
artifact body is actually computed. So editing a pure function to fix a wrong
number leaves the key identical, and every document already processed keeps
serving the wrong number forever. The fix is invisible in exactly the corpus
that motivated it.

This is not hypothetical: two commits on this branch changed `fdi.core`
artifact bodies with no version bump.

The guard is a recorded digest over the TRANSITIVE `fdi.core` closure of each
stage. Touch that closure and this test fails, telling you to bump the stage's
version and record the new digest. It cannot know whether your edit changed the
output, so it asks rather than guesses -- a cosmetic docstring edit is a
one-line digest update, and a real change is a version bump. Both are
deliberate, which is the entire point.
"""

import ast
import hashlib
import importlib
import inspect
import pathlib

import pytest

import fdi.pipeline.stages  # noqa: F401  -- registers every stage
from fdi.pipeline.registry import REGISTRY

# stage name -> (version, digest of its fdi.core closure)
#
# To update: run this test, read the reported digest, decide whether the edit
# changes any artifact BODY. If it does, bump the stage's `version` too.
# Baselined against the code that produced the verified end-to-end TCS run
# (consolidated: assets 4/7, equity 3/3, liabilities 6/6, P&L 9/11, precision
# 100% throughout), so these digests pin a MEASURED state, not merely a
# current one.
#
# statement_index, note_index and fog_objects share a digest because they share
# one closure -- all three are built from fdi.core.fog_build. That is correct
# and load-bearing: a fog_build edit must be considered against all three.
EXPECTED: dict[str, tuple[int, str]] = {
    "fog_mapping": (7, "8d46fa8b294bc400"),
    "fog_objects": (6, "3d124423a52f524c"),
    "fog_reason": (4, "1741923af98df827"),
    "note_index": (3, "3d124423a52f524c"),
    "statement_index": (4, "3d124423a52f524c"),
}


def _core_closure(module_name: str) -> set[str]:
    """Every `fdi.core` module reachable from `module_name` by import.

    Transitive, because the boundary that matters is the code that runs, not
    the code that is named: `fog_build` imports `statements`, and a wrong row
    index in `statements` reaches the artifact just as directly."""
    seen: set[str] = set()
    queue = [module_name]
    while queue:
        current = queue.pop()
        try:
            source = inspect.getsource(importlib.import_module(current))
        except (OSError, TypeError, ImportError):  # pragma: no cover
            continue
        for node in ast.walk(ast.parse(source)):
            names: list[str] = []
            if isinstance(node, ast.ImportFrom) and node.module:
                names.append(node.module)
            elif isinstance(node, ast.Import):
                names.extend(a.name for a in node.names)
            for name in names:
                if name.startswith("fdi.core.") and name not in seen:
                    seen.add(name)
                    queue.append(name)
    return seen


def _digest(modules: set[str]) -> str:
    """Stable over module ORDER, sensitive to module CONTENT."""
    h = hashlib.sha256()
    for name in sorted(modules):
        path = pathlib.Path(importlib.import_module(name).__file__ or "")
        # Read bytes and normalise line endings: this repo checks out CRLF on
        # Windows and LF in CI, and a digest that depends on the checkout would
        # fail everywhere for no reason.
        h.update(name.encode())
        h.update(path.read_bytes().replace(b"\r\n", b"\n"))
    return h.hexdigest()[:16]


@pytest.mark.parametrize("stage_name", sorted(EXPECTED))
def test_stage_version_is_pinned_to_its_core_digest(stage_name: str) -> None:
    stage = REGISTRY.all()[stage_name]
    expected_version, expected_digest = EXPECTED[stage_name]
    actual = _digest(_core_closure(type(stage).__module__))

    assert stage.version == expected_version, (
        f"{stage_name}.version changed to {stage.version}. Update EXPECTED "
        f"in this file to ({stage.version}, {actual!r})."
    )
    assert actual == expected_digest, (
        f"{stage_name}'s fdi.core closure changed (digest {actual!r}).\n"
        f"If any ARTIFACT BODY changed, bump {type(stage).__name__}.version -- "
        f"otherwise every already-processed document keeps serving the old "
        f"body from cache. Then record ({stage.version}, {actual!r}) here."
    )


def test_every_fog_stage_is_covered() -> None:
    """A new FOG stage must be added to EXPECTED, not silently unguarded."""
    fog_stages = {n for n in REGISTRY.all() if n.startswith(("fog_", "statement_", "note_"))}
    assert fog_stages <= set(EXPECTED), f"unguarded: {sorted(fog_stages - set(EXPECTED))}"
