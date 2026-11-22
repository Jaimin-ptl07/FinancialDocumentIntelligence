"""The optional LLM judge's pure half: whether a research answer's
`required_claims` are supported, judged from the cited evidence alone.

Three entry points, none of which calls a model:

- `judge_messages(item, answer, evidence)` -- the chat messages a judge
  model is sent: the question, the answer, every required claim numbered
  from 0, and the evidence the answer cited -- nothing else, so the judge
  cannot lean on what it already knows about a Company.
- `parse_judgement(item, reply)` -- the model's reply as one verdict per
  claim. `not_judged(item, reason)` is the verdict for an answer that never
  came: every claim unsupported, and no judge call spent on it.
  `judge_error(item, reason)` is the verdict when the judge itself failed.
- `summarise_judgements(benchmark, judgements) -> JudgeReport` -- the JUDGE
  COLUMNS, per kind and for the run.

JUDGE COLUMNS ARE NOT SCORES
----------------------------
A judge's opinion drifts with the judge model and its prompt; the
deterministic scores (`fdi.core.answer_scoring`) do not. The two are
reported side by side and never blended: nothing here reads an `ItemScore`,
and nothing there reads a judgement.

A claim is SUPPORTED when the answer makes it AND the evidence bears it
out. An item is FULLY SUPPORTED when all of its claims are.

A JUDGE ERROR (the call failed, or the reply does not judge every claim
exactly once) is the judge's failure, not the answer's. It is counted in
`judge_errors` and left out of every support rate -- scoring it as
unsupported would charge the endpoint for the judge's mistake. A rate is
None, never 0.0, when nothing was judged.

Pure (`fdi.core` law): no I/O, no LLM, deterministic.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass

from fdi.core.benchmark import Benchmark, BenchmarkItem

# Enough for several notes' worth of tool results, well inside the judge
# model's context; beyond it the evidence is cut, and the judge is told so.
MAX_EVIDENCE_CHARS = 40_000

_SYSTEM_PROMPT = (
    "You are grading an answer to a question about a company's annual report. "
    "You are given the answer, a numbered list of claims a complete answer must make, "
    "and the EVIDENCE the answer cited. Judge ONLY from that evidence: never use "
    "anything you know about the company. A claim is supported when the answer makes "
    "it (in any wording) AND the evidence bears it out. Reply with JSON only: "
    '{"claims": [{"index": <claim number>, "supported": true|false, '
    '"reason": "<one sentence>"}]}, one entry per claim.'
)

_FENCE = re.compile(r"^\s*```(?:json)?\s*(?P<body>.*?)\s*```\s*$", re.DOTALL)


@dataclass(frozen=True)
class ClaimVerdict:
    claim: str
    supported: bool
    reason: str


@dataclass(frozen=True)
class ItemJudgement:
    """The judge's verdicts on one item's required claims, in claim order;
    () with `error` set when the judge itself failed."""

    item_id: str
    kind: str
    verdicts: tuple[ClaimVerdict, ...]
    error: str | None = None

    @property
    def supported_claims(self) -> int:
        return sum(v.supported for v in self.verdicts)


@dataclass(frozen=True)
class JudgeSummary:
    """Judge columns over a set of judged items. `items` counts every item
    with required claims that was run, judge errors included; `claims` and
    the rates count only the items the judge actually judged."""

    items: int
    judge_errors: int
    claims: int
    supported_claims: int
    fully_supported: int

    @property
    def claim_support_rate(self) -> float | None:
        """Supported claims over claims judged."""
        return self.supported_claims / self.claims if self.claims else None

    @property
    def full_support_rate(self) -> float | None:
        """Items with every claim supported, over items judged."""
        judged = self.items - self.judge_errors
        return self.fully_supported / judged if judged else None


@dataclass(frozen=True)
class JudgeReport:
    items: tuple[ItemJudgement, ...]
    by_kind: dict[str, JudgeSummary]
    overall: JudgeSummary


def judge_messages(
    item: BenchmarkItem,
    answer: str,
    evidence: Sequence[str],
    *,
    max_evidence_chars: int = MAX_EVIDENCE_CHARS,
) -> list[dict[str, object]]:
    text = "\n\n".join(evidence)
    if len(text) > max_evidence_chars:
        text = text[:max_evidence_chars] + "\n[evidence truncated]"
    claims = "\n".join(f"{i}. {claim}" for i, claim in enumerate(item.gold.required_claims))
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": (
            f"QUESTION:\n{item.question}\n\nANSWER:\n{answer}\n\nCLAIMS:\n{claims}"
            f"\n\nEVIDENCE:\n{text or '(the answer cited no evidence)'}"
        )},
    ]


def not_judged(item: BenchmarkItem, reason: str) -> ItemJudgement:
    """No answer to judge: every claim unsupported, at no judge cost."""
    return ItemJudgement(
        item_id=item.id, kind=item.kind,
        verdicts=tuple(ClaimVerdict(c, False, reason) for c in item.gold.required_claims),
    )


def judge_error(item: BenchmarkItem, reason: str) -> ItemJudgement:
    """The judge failed on `item`: no verdicts, out of every support rate."""
    return ItemJudgement(item_id=item.id, kind=item.kind, verdicts=(), error=reason)


def parse_judgement(item: BenchmarkItem, reply: str) -> ItemJudgement:
    def error(reason: str) -> ItemJudgement:
        return judge_error(item, reason)

    fenced = _FENCE.match(reply)
    try:
        body = json.loads(fenced["body"] if fenced else reply)
    except ValueError:
        return error("judge reply is not JSON")
    raw = body.get("claims") if isinstance(body, dict) else None
    if not isinstance(raw, list):
        return error("judge reply has no 'claims' list")

    claims = item.gold.required_claims
    found: dict[int, ClaimVerdict] = {}
    for n, entry in enumerate(raw):
        index = entry.get("index") if isinstance(entry, dict) else None
        supported = entry.get("supported") if isinstance(entry, dict) else None
        if (not isinstance(index, int) or isinstance(index, bool)
                or not isinstance(supported, bool)):
            return error(f"judge verdict #{n} is malformed")
        if not 0 <= index < len(claims):
            return error(f"judge judged claim {index}, which does not exist")
        if index in found:
            return error(f"judge judged claim {index} twice")
        reason = entry.get("reason")
        found[index] = ClaimVerdict(claims[index], supported,
                                    reason if isinstance(reason, str) else "")
    missing = [i for i in range(len(claims)) if i not in found]
    if missing:
        return error(f"judge gave no verdict for claim {missing[0]}")
    return ItemJudgement(item_id=item.id, kind=item.kind,
                         verdicts=tuple(found[i] for i in range(len(claims))))


def summarise_judgements(
    benchmark: Benchmark, judgements: Sequence[ItemJudgement]
) -> JudgeReport:
    """Judgements in benchmark order; ids the benchmark does not hold are
    ignored."""
    by_id = {j.item_id: j for j in judgements}
    items = tuple(by_id[i.id] for i in benchmark.items if i.id in by_id)
    kinds = sorted({j.kind for j in items})
    return JudgeReport(
        items=items,
        by_kind={k: _summarise([j for j in items if j.kind == k]) for k in kinds},
        overall=_summarise(items),
    )


def _summarise(items: Sequence[ItemJudgement]) -> JudgeSummary:
    judged = [j for j in items if j.error is None]
    return JudgeSummary(
        items=len(items),
        judge_errors=len(items) - len(judged),
        claims=sum(len(j.verdicts) for j in judged),
        supported_claims=sum(j.supported_claims for j in judged),
        fully_supported=sum(all(v.supported for v in j.verdicts) for j in judged),
    )
