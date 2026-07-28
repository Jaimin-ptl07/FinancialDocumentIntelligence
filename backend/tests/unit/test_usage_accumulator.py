from fdi.core.pricing import LlmCallUsage
from fdi.pipeline.usage import UsageAccumulator


def test_accumulator_starts_empty() -> None:
    acc = UsageAccumulator()
    assert acc.llm_calls == []


def test_accumulator_records_calls_in_order() -> None:
    acc = UsageAccumulator()
    u1 = LlmCallUsage(model="gpt-4o", input_tokens=10, output_tokens=5, duration_ms=100)
    u2 = LlmCallUsage(model="gpt-4o", input_tokens=20, output_tokens=8, duration_ms=150)
    acc.record_llm("semantic_classify", u1)
    acc.record_llm("semantic_classify", u2)
    assert [c.usage for c in acc.llm_calls] == [u1, u2]
    assert all(c.source == "semantic_classify" for c in acc.llm_calls)
