"""SemanticStage: a DERIVED stage that classifies each AST section into a
SectionType via an injected SemanticClient. Auto-chains off AstStage via
`deps=("ast",)`.

Content-addressed on `derived_input_hash([ast.output_hash])` +
`SemanticStage.version` -- deliberate, since the LLM is non-deterministic: a
second run against the same `ast` artifact is a store cache hit that does NOT
call the client again (proven in tests/integration/test_semantic_stage.py with
a call-counting fake). Each real classify() call reports its LlmCallUsage to
ctx.services["usage"] (costing feature, docs/superpowers/specs/2026-07-28-
costing-feature-design.md) -- run_stage persists these as llm_usage_event
rows after this stage returns, so a cache hit (this method never runs) does
not bill again either."""

from collections.abc import Mapping
from typing import cast

from fdi.core.semantic import SectionType, build_snippet
from fdi.pipeline.registry import REGISTRY
from fdi.pipeline.semantic import SemanticClient
from fdi.pipeline.stage import StageContext
from fdi.pipeline.usage import UsageAccumulator


class SemanticStage:
    name = "semantic"
    version = 1
    deps: tuple[str, ...] = ("ast",)

    async def run(self, ctx: StageContext) -> object:
        ast = cast(Mapping[str, object], await ctx.read("ast"))
        client = cast(SemanticClient, ctx.services["semantic"])
        usage = cast(UsageAccumulator, ctx.services["usage"])

        sections_out: list[dict[str, object]] = []
        sections_raw = ast.get("sections")
        if isinstance(sections_raw, list):
            for section in sections_raw:
                if not isinstance(section, dict):
                    continue
                heading_raw = section.get("heading")
                heading = heading_raw if isinstance(heading_raw, str) else ""
                paragraphs_raw = section.get("paragraphs")
                paragraphs = (
                    [p for p in paragraphs_raw if isinstance(p, str)]
                    if isinstance(paragraphs_raw, list)
                    else []
                )
                snippet = build_snippet(paragraphs)
                result = client.classify(heading, snippet)
                if result.usage is not None:
                    usage.record_llm("semantic_classify", result.usage)
                section_type = SectionType(result.label)
                sections_out.append(
                    {
                        "heading": heading_raw,
                        "type": section_type.value,
                        "paragraph_count": len(paragraphs),
                    }
                )

        return {"schema_version": 1, "sections": sections_out}


REGISTRY.register(SemanticStage())
