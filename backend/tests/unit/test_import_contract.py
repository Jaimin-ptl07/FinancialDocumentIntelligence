"""The layered contract is the project's most important structural rule.

This test runs the contract as part of the pytest suite, so a violation fails
the tests a developer runs locally rather than only a separate CI step.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[2]


def test_import_contract_holds() -> None:
    if shutil.which("lint-imports") is None:
        pytest.fail(
            "lint-imports not on PATH -- run tests via "
            "`uv run --directory backend pytest`, not a bare `pytest`."
        )
    # MUST be the `lint-imports` console script. Do NOT use
    # `python -m importlinter.cli` -- importlinter/cli.py has no
    # `if __name__ == "__main__"` guard, so `-m` merely imports the module and
    # exits 0 without running anything. Verified: pointing that form at a config
    # file that DOES NOT EXIST still returns exit 0 with no output, so this test
    # would pass forever while enforcing nothing.
    result = subprocess.run(
        ["lint-imports", "--config", str(BACKEND / ".importlinter")],
        cwd=BACKEND,
        capture_output=True,
        text=True,
        # Both of these are load-bearing on Windows, for the same root cause:
        # import-linter renders a `:brick:` emoji through `rich`, which on a
        # non-VT console takes its legacy path and encodes to the ANSI code page
        # (cp1252 here). PYTHONUTF8=1 puts the child in UTF-8 mode so the emoji
        # survives; encoding="utf-8" stops OUR reader thread dying on the way
        # back, which silently blanks result.stdout and makes the assertions
        # below judge an empty string.
        encoding="utf-8",
        env={**os.environ, "PYTHONUTF8": "1"},
        timeout=30,
    )
    detail = f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    # A crashed linter and a real violation BOTH exit 1. Check the linter
    # actually reached a verdict before blaming the architecture for it --
    # otherwise a tooling crash reports itself as "architecture violated",
    # which sends you hunting a violation that does not exist.
    assert "Contracts:" in result.stdout, (
        f"lint-imports did not run to completion, so the contract was never "
        f"checked. This is a TOOLING failure, not an architecture violation.\n"
        f"{detail}"
    )
    assert result.returncode == 0, f"Layered architecture violated.\n{detail}"
