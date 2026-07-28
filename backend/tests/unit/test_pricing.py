from fdi.core.pricing import LlmCallUsage, llm_cost_inr, ocr_cost_inr


def test_ocr_cost_inr_zero_pages() -> None:
    assert ocr_cost_inr(0, 1.0) == 0.0


def test_ocr_cost_inr_known_pages() -> None:
    assert ocr_cost_inr(42, 1.0) == 42.0
    assert ocr_cost_inr(10, 2.5) == 25.0


def test_llm_cost_inr_zero_tokens() -> None:
    assert llm_cost_inr(
        0, 0, input_usd_per_mtok=2.5, output_usd_per_mtok=10.0, usd_to_inr=95.0
    ) == 0.0


def test_llm_cost_inr_known_tokens() -> None:
    # 1_000_000 input tokens @ $2.5/M + 1_000_000 output tokens @ $10/M
    # = $12.5 USD * 95 INR/USD = 1187.5 INR
    cost = llm_cost_inr(
        1_000_000, 1_000_000,
        input_usd_per_mtok=2.5, output_usd_per_mtok=10.0, usd_to_inr=95.0,
    )
    assert cost == 1187.5


def test_llm_cost_inr_fractional_tokens() -> None:
    # 500 input tokens @ $2.5/M = $0.00125 -> * 95 = 0.11875 INR
    cost = llm_cost_inr(
        500, 0, input_usd_per_mtok=2.5, output_usd_per_mtok=10.0, usd_to_inr=95.0
    )
    assert cost == 0.11875


def test_llm_call_usage_is_frozen() -> None:
    usage = LlmCallUsage(model="gpt-4o", input_tokens=10, output_tokens=5, duration_ms=120)
    assert usage.model == "gpt-4o"
    assert usage.input_tokens == 10
