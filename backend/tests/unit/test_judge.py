"""The LLM judge's pure half: what it is shown, how its reply becomes a
verdict per required claim, and the judge columns a run reports -- kept
apart from the deterministic scores, never blended into them."""

import json

import pytest

from fdi.core.benchmark import Benchmark, parse_benchmark
from fdi.core.judge import (
    ItemJudgement,
    judge_error,
    judge_messages,
    not_judged,
    parse_judgement,
    summarise_judgements,
)


def _bench() -> Benchmark:
    def research(item_id: str, claims: list[str]) -> dict[str, object]:
        return {
            "id": item_id, "kind": "research", "question": f"Question {item_id}?",
            "gold": {"answerable": True, "evidence": [{"doc_key": "tcs-2025-26", "page": 210}],
                     "required_claims": claims},
        }

    return parse_benchmark({"version": "v-test", "items": [
        research("r1", ["Interest income is the largest component.",
                        "Other income includes net gain on investments."]),
        research("r2", ["The Board recommended a final dividend."]),
        {"id": "f1", "kind": "fact_lookup", "question": "Revenue?",
         "gold": {"answerable": True, "values": [{"value": 1.0, "unit": "INR crore"}]}},
    ]})


_R1 = _bench().items[0]


def _reply(*verdicts: tuple[int, bool]) -> str:
    return json.dumps({"claims": [
        {"index": i, "supported": s, "reason": f"reason {i}"} for i, s in verdicts
    ]})


def test_the_judge_sees_the_question_the_answer_every_claim_and_only_the_evidence() -> None:
    messages = judge_messages(_R1, "Interest income leads.", ["Note 21: Interest 2,875"])
    text = "\n".join(str(m["content"]) for m in messages)
    assert messages[0]["role"] == "system"
    assert "ONLY" in str(messages[0]["content"])
    for expected in ("Question r1?", "Interest income leads.", "Note 21: Interest 2,875",
                     "0. Interest income is the largest component.",
                     "1. Other income includes net gain on investments."):
        assert expected in text


def test_evidence_beyond_the_cap_is_cut_and_says_so() -> None:
    messages = judge_messages(_R1, "answer", ["x" * 100], max_evidence_chars=40)
    user = str(messages[-1]["content"])
    assert "x" * 40 in user and "x" * 41 not in user
    assert "[evidence truncated]" in user


def test_a_reply_becomes_one_verdict_per_claim_in_claim_order() -> None:
    judgement = parse_judgement(_R1, _reply((1, False), (0, True)))
    assert judgement.error is None
    assert [(v.claim, v.supported, v.reason) for v in judgement.verdicts] == [
        ("Interest income is the largest component.", True, "reason 0"),
        ("Other income includes net gain on investments.", False, "reason 1"),
    ]
    assert judgement.supported_claims == 1


def test_a_reply_in_a_code_fence_still_parses() -> None:
    fenced = "```json\n" + _reply((0, True), (1, True)) + "\n```"
    assert parse_judgement(_R1, fenced).supported_claims == 2


@pytest.mark.parametrize(
    ("reply", "error"),
    [
        ("the answer looks fine", "judge reply is not JSON"),
        (json.dumps({"verdicts": []}), "judge reply has no 'claims' list"),
        (_reply((0, True)), "judge gave no verdict for claim 1"),
        (_reply((0, True), (0, False), (1, True)), "judge judged claim 0 twice"),
        (_reply((0, True), (1, True), (2, True)), "judge judged claim 2, which does not exist"),
        (json.dumps({"claims": [{"index": 0, "supported": "yes"},
                                {"index": 1, "supported": True}]}),
         "judge verdict #0 is malformed"),
    ],
)
def test_a_reply_that_does_not_judge_every_claim_exactly_once_is_a_judge_error(
    reply: str, error: str
) -> None:
    """A judge error is the judge's failure, not the answer's: no verdicts."""
    judgement = parse_judgement(_R1, reply)
    assert (judgement.error, judgement.verdicts) == (error, ())


def test_an_answer_that_never_came_supports_no_claim_without_asking_the_judge() -> None:
    judgement = not_judged(_R1, "no answer returned")
    assert judgement.error is None
    assert [v.supported for v in judgement.verdicts] == [False, False]
    assert {v.reason for v in judgement.verdicts} == {"no answer returned"}


def test_the_judge_columns_count_claims_items_and_judge_errors_apart() -> None:
    """r1: 1 of 2 claims supported. r2: the judge failed -- counted as a
    judge error and left out of every support rate, since the answer was
    never judged. f1 has no claims and is not in the judge columns at all."""
    report = summarise_judgements(_bench(), [
        parse_judgement(_R1, _reply((0, True), (1, False))),
        ItemJudgement(item_id="r2", kind="research", verdicts=(), error="timeout"),
    ])
    research = report.by_kind["research"]
    assert (research.items, research.judge_errors, research.claims,
            research.supported_claims, research.fully_supported) == (2, 1, 2, 1, 0)
    assert research.claim_support_rate == 0.5
    assert research.full_support_rate == 0.0
    assert set(report.by_kind) == {"research"}
    assert report.overall == research


def test_rates_are_none_when_nothing_was_judged() -> None:
    report = summarise_judgements(_bench(), [
        ItemJudgement(item_id="r2", kind="research", verdicts=(), error="timeout"),
    ])
    assert report.overall.claim_support_rate is None
    assert report.overall.full_support_rate is None


def test_a_failed_judge_call_is_a_judge_error_with_no_verdicts() -> None:
    judgement = judge_error(_R1, "judge call failed: timeout")
    assert (judgement.item_id, judgement.kind) == ("r1", "research")
    assert (judgement.error, judgement.verdicts) == ("judge call failed: timeout", ())
