"""The per-stage-execution LLM usage collector (costing feature, docs/
superpowers/specs/2026-07-28-costing-feature-design.md sec 3.3).

A stage's `run()` only ever returns its artifact body -- widening that
return contract to also carry cost data would break the "framework owns
all I/O and persistence" principle every Stage already follows (see
fdi.pipeline.stage's own docstring). Instead, `run_stage` constructs ONE
UsageAccumulator per invocation, injects it into StageContext.services
under the key "usage", and a stage implementation that makes real LLM
calls (currently only SemanticStage) reports each one to it as it happens
-- purely in-memory, no I/O, safe to call from inside a stage's near-pure
`run()`. `run_stage` reads the accumulator back AFTER `stage.run()`
returns (on success OR failure) and persists one llm_usage_event row per
entry, attributing them to that execution's stage_run_id."""

from dataclasses import dataclass, field

from fdi.core.pricing import LlmCallUsage


@dataclass(frozen=True)
class RecordedLlmCall:
    source: str
    usage: LlmCallUsage


@dataclass
class UsageAccumulator:
    llm_calls: list[RecordedLlmCall] = field(default_factory=list)

    def record_llm(self, source: str, usage: LlmCallUsage) -> None:
        self.llm_calls.append(RecordedLlmCall(source=source, usage=usage))
