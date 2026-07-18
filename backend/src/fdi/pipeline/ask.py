"""RT-8's agent loop: answer_question(question, ctx, agent_client,
max_rounds=5) -> {answer, citations, tool_trace}. Plans (agent_client.
complete), executes (execute_tool against ctx's real injected services),
feeds results back, repeats up to max_rounds, then either returns the
LLM's final content or a locally-built best-effort answer on exhaustion
(design spec decision 12 -- no extra "force a final answer" LLM call)."""

import json
from collections.abc import Mapping

import structlog

from fdi.core.agent import dedup_citations, extract_citations
from fdi.pipeline.agent import AgentClient
from fdi.pipeline.agent_tools import TOOL_SCHEMAS, AgentContext, execute_tool

log = structlog.get_logger(__name__)

_SYSTEM_PROMPT = (
    "You are a financial-document analyst. Answer the user's question using "
    "ONLY facts returned by your tools -- never rely on prior knowledge of a "
    "specific company or filing. Call tools as needed; when you have enough "
    "information, give a final answer that (1) directly answers the "
    "question, (2) cites the specific document/section/table/period each "
    "fact came from, and (3) says plainly if the tools could not find an "
    "answer rather than guessing."
)


async def answer_question(
    *, question: str, ctx: AgentContext, agent_client: AgentClient, max_rounds: int = 5
) -> dict[str, object]:
    messages: list[dict[str, object]] = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]
    tool_trace: list[dict[str, object]] = []
    raw_citations: list[dict[str, object]] = []

    for round_num in range(max_rounds):
        resp = agent_client.complete(messages, TOOL_SCHEMAS)
        if resp.tool_calls is None:
            return {
                "answer": resp.content or "",
                "citations": dedup_citations(raw_citations),
                "tool_trace": tool_trace,
            }

        messages.append({
            "role": "assistant",
            "tool_calls": [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)}}
                for tc in resp.tool_calls
            ],
        })
        for tc in resp.tool_calls:
            result = await execute_tool(tc.name, tc.arguments, ctx=ctx)
            error = result.get("error") if isinstance(result, Mapping) else None
            tool_trace.append({
                "round": round_num, "tool": tc.name, "arguments": tc.arguments,
                "result": result, "error": error,
            })
            raw_citations.extend(extract_citations(tc.name, tc.arguments, result))
            messages.append({
                "role": "tool", "tool_call_id": tc.id,
                "content": json.dumps(result, default=str),
            })

    log.warning("ask.max_rounds_exhausted", max_rounds=max_rounds)
    return {
        "answer": f"Could not fully answer within {max_rounds} tool-call round(s).",
        "citations": dedup_citations(raw_citations),
        "tool_trace": tool_trace,
    }
