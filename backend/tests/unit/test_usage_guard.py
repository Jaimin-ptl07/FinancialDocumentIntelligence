"""A failed usage write must never void a successful extraction."""

import pytest

from fdi.core.extractor import ExtractResult
from fdi.core.pricing import LlmCallUsage
from fdi.pipeline import extractor as ex


class _Client:
    def extract(self, *, system: str, user: str) -> ExtractResult:
        return ExtractResult(
            data={"assets.CB": {"value": 1833, "confidence": 1.0,
                                "source": {"page": 252}}},
            usage=LlmCallUsage(model="gpt-4o", input_tokens=10,
                               output_tokens=5, duration_ms=12),
        )


def _unit() -> ex.UnitSpec:
    field = ex.FieldSpec(
        field_name="Cash balance", data_type="decimal", retrieval_query="cash",
        keywords=(), extraction_prompt="extract cash", enum_values=None,
        module_id=1, module_name="Assets", unit_id="U1", field_path="assets.CB",
    )
    return ex.UnitSpec(unit_id="U1", unit_title="Assets", retrieval_query="cash",
                       locator_keywords=(), fields=(field,))


@pytest.mark.asyncio
async def test_extraction_survives_a_broken_usage_write(monkeypatch):
    """Regression: a failed usage write does not lose `raw` -- it poisons
    ctx.session, and hybrid_search shares that session, so retrieval died for
    every LATER unit. Measured with llm_usage_event absent: TCS 125 -> 71
    filled, 19 -> 9 correct, early units fine and the rest empty. The session
    must be rolled back so the next unit can still retrieve."""
    attempts: list[str] = []
    rolled_back: list[str] = []

    class _Session:
        async def rollback(self) -> None:
            rolled_back.append("rolled back")

    class _Ctx:
        session = _Session()
        doc_id = "d1"

    async def boom(**kwargs: object) -> None:
        attempts.append("attempted")
        raise RuntimeError('relation "llm_usage_event" does not exist')

    async def pool(**kwargs: object) -> list[ex.Candidate]:
        return [ex.Candidate(evidence_id="d:1", text="Cash | 1,833", page=252,
                             score=1.0, source_tool="hybrid")]

    monkeypatch.setattr(ex, "_record_llm_usage", boom)
    monkeypatch.setattr(ex, "_pool_for", pool)

    records = await ex.extract_unit(
        ctx=_Ctx(), unit=_unit(), client=_Client(),  # type: ignore[arg-type]
    )

    assert attempts == ["attempted"]        # the write was tried
    assert records[0]["value"] == 1833      # the extraction still stands
    assert rolled_back == ["rolled back"]   # and the session is usable again


@pytest.mark.asyncio
async def test_a_working_usage_write_is_still_recorded(monkeypatch):
    seen: list[object] = []

    async def ok(**kwargs: object) -> None:
        seen.append(kwargs.get("usage"))

    async def pool(**kwargs: object) -> list[ex.Candidate]:
        return [ex.Candidate(evidence_id="d:1", text="Cash | 1,833", page=252,
                             score=1.0, source_tool="hybrid")]

    monkeypatch.setattr(ex, "_record_llm_usage", ok)
    monkeypatch.setattr(ex, "_pool_for", pool)

    records = await ex.extract_unit(
        ctx=object(), unit=_unit(), client=_Client(),  # type: ignore[arg-type]
    )
    assert len(seen) == 1 and records[0]["value"] == 1833
