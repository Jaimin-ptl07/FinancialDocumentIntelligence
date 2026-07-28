"""Pure OCR/LLM cost calculation, and the `LlmCallUsage` data shape every
platform LLM client (agent/extractor/semantic) returns alongside its
payload. No I/O, no config object, no clock -- costs are computed from
already-known counts and a caller-supplied price, per
docs/superpowers/specs/2026-07-28-costing-feature-design.md sec 2 decision
2: INR is always computed at READ time from the currently configured
price, never persisted per event."""

from dataclasses import dataclass


@dataclass(frozen=True)
class LlmCallUsage:
    """One real chat.completions.create() call's cost-relevant facts."""

    model: str
    input_tokens: int
    output_tokens: int
    duration_ms: int


def ocr_cost_inr(page_count: int, price_per_page_inr: float) -> float:
    return page_count * price_per_page_inr


def llm_cost_inr(
    input_tokens: int,
    output_tokens: int,
    *,
    input_usd_per_mtok: float,
    output_usd_per_mtok: float,
    usd_to_inr: float,
) -> float:
    input_inr_per_mtok = input_usd_per_mtok * usd_to_inr
    output_inr_per_mtok = output_usd_per_mtok * usd_to_inr
    return (
        input_tokens * input_inr_per_mtok + output_tokens * output_inr_per_mtok
    ) / 1_000_000
