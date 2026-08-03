"""The three FOG stages run in order against fixture artifacts, through the
real StageContext -- proving the deps chain and the artifact shapes line up."""

from collections.abc import Mapping

import pytest

from fdi.pipeline.stage import StageContext
from fdi.pipeline.stages.fog_objects import FogObjectsStage
from fdi.pipeline.stages.note_index import NoteIndexStage
from fdi.pipeline.stages.statement_index import StatementIndexStage

pytestmark = pytest.mark.integration


def _cell(r: int, c: int, text: str) -> dict[str, object]:
    return {"row_index": r, "column_index": c, "content": text}


_OCR: dict[str, object] = {
    "paragraphs": [{
        "content": "10. Revenue from operations",
        "role": "sectionHeading",
        "bounding_regions": [{"page_number": 170}],
        "spans": [{"offset": 9000, "length": 27}],
    }],
    "tables": [
        {
            "bounding_regions": [{"page_number": 150}],
            "spans": [{"offset": 500, "length": 100}],
            "cells": [
                _cell(0, 0, "Particulars"), _cell(0, 1, "Note"), _cell(0, 2, "2025-26"),
                _cell(1, 0, "Revenue from operations"), _cell(1, 1, "10"),
                _cell(1, 2, "2,55,324"),
                _cell(2, 0, "Total income"), _cell(2, 2, "100"),
                _cell(3, 0, "Total expenses"), _cell(3, 2, "70"),
                _cell(4, 0, "Profit before tax"), _cell(4, 2, "30"),
            ],
        },
        {
            "bounding_regions": [{"page_number": 170}],
            "spans": [{"offset": 9100, "length": 60}],
            "cells": [
                _cell(0, 0, "Particulars"), _cell(0, 1, "2025-26"),
                _cell(1, 0, "Sale of services"), _cell(1, 1, "2,55,324"),
            ],
        },
    ],
}
_BASIS: dict[str, object] = {
    "default_basis": "unknown",
    "regions": [{"basis": "consolidated", "start_page": 100, "end_page": 200}],
}


async def test_the_three_stages_chain_into_a_populated_fog():
    artifacts: dict[str, object] = {"ocr": _OCR, "structure": {}, "basis": _BASIS}

    async def read(_doc_id: str, stage: str) -> object:
        return artifacts[stage]

    ctx = StageContext(doc_id="doc-1", read_artifact=read)

    artifacts["statement_index"] = await StatementIndexStage().run(ctx)
    artifacts["note_index"] = await NoteIndexStage().run(ctx)
    fog = await FogObjectsStage().run(ctx)

    assert isinstance(fog, Mapping)
    region = fog["bases"]["consolidated"]
    labels = {i["label"] for i in region["line_items"]}
    assert "Revenue from operations" in labels
    assert "Sale of services" in labels
    assert [e for e in region["edges"] if e["type"] == "REFERENCES"]
    assert fog["diagnostics"]["consolidated"]["refs_resolved"] == 1


async def test_a_second_run_produces_a_byte_identical_body():
    """Content-addressed caching depends on this. Any set iteration or dict
    ordering leaking into the body breaks it silently."""
    artifacts: dict[str, object] = {"ocr": _OCR, "structure": {}, "basis": _BASIS}

    async def read(_doc_id: str, stage: str) -> object:
        return artifacts[stage]

    ctx = StageContext(doc_id="doc-1", read_artifact=read)
    artifacts["statement_index"] = await StatementIndexStage().run(ctx)
    artifacts["note_index"] = await NoteIndexStage().run(ctx)

    assert await FogObjectsStage().run(ctx) == await FogObjectsStage().run(ctx)
