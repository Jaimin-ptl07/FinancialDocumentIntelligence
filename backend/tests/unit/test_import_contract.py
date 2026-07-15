"""The layered contract is the project's most important structural rule.

This test runs the contract as part of the pytest suite, so a violation fails
the tests a developer runs locally rather than only a separate CI step.
"""

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
        timeout=30,
    )
    assert result.returncode == 0, (
        f"Layered architecture violated.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
