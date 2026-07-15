"""The layered contract is the project's most important structural rule.
This test runs it in-process so a violation fails pytest, not just CI."""
import subprocess
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]


def test_import_contract_holds() -> None:
    result = subprocess.run(
        ["lint-imports", "--config", str(BACKEND / ".importlinter")],
        cwd=BACKEND, capture_output=True, text=True,
    )
    assert result.returncode == 0, (
        f"Layered architecture violated.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
