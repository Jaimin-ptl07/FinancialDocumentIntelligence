"""The committed benchmark is generated, never typed, and the runner never
calls a billed endpoint without an explicit item limit."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))

from eval_answers import (  # noqa: E402
    BENCHMARK_DIR,
    dump,
    generate_fact_lookup,
    load_benchmark,
    main,
)


def test_the_committed_fact_lookup_items_are_exactly_what_ground_truth_generates() -> None:
    """A hand edit to the benchmark, or ground truth that moved without a
    regenerate, shows up here. Fix: `eval_answers.py generate`."""
    committed = (BENCHMARK_DIR / "fact_lookup.json").read_text(encoding="utf-8")
    assert committed == dump(generate_fact_lookup())


def test_the_committed_benchmark_loads_with_one_item_per_ground_truth_value() -> None:
    gt_values = sum(
        len(json.loads(p.read_text(encoding="utf-8"))["values"])
        for p in (BENCHMARK_DIR.parents[1]).glob("*.json")
    )
    bench = load_benchmark(BENCHMARK_DIR)
    assert bench.version == "v0"
    assert len(bench.items) == gt_values
    assert all(item.kind == "fact_lookup" for item in bench.items)
    assert not any("RITES" in item.question for item in bench.items)


def test_run_refuses_to_call_ask_without_a_limit_or_confirmation(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # An unroutable endpoint: any HTTP attempt would raise, not print.
    code = main(["run", "--endpoint", "http://127.0.0.1:9", "--out-dir", "unused"])
    assert code == 0
    assert "Pass --limit N" in capsys.readouterr().out
