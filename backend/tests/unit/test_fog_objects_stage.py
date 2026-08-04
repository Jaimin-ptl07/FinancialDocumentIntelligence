"""The whole deterministic FOG, assembled per basis, with the diagnostics that
make `absent` distinguishable from `missed`."""

from fdi.core.fog_build import build_fog, build_note_index, build_statement_index


def _cell(r: int, c: int, text: str) -> dict[str, object]:
    return {"row_index": r, "column_index": c, "content": text}


_FACE = {
    "bounding_regions": [{"page_number": 150}],
    "spans": [{"offset": 500, "length": 100}],
    "cells": [
        _cell(0, 0, "Particulars"), _cell(0, 1, "Note"), _cell(0, 2, "2025-26"),
        _cell(1, 0, "Revenue from operations"), _cell(1, 1, "10"), _cell(1, 2, "90"),
        _cell(2, 0, "Total income"), _cell(2, 2, "100"),
        _cell(3, 0, "Total expenses"), _cell(3, 2, "70"),
        _cell(4, 0, "Profit before tax"), _cell(4, 2, "30"),
    ],
}
_NOTE_TABLE = {
    "bounding_regions": [{"page_number": 170}],
    "spans": [{"offset": 9100, "length": 60}],
    "cells": [
        _cell(0, 0, "Particulars"), _cell(0, 1, "2025-26"),
        _cell(1, 0, "Sale of services"), _cell(1, 1, "90"),
    ],
}
_HEAD = {
    "content": "10. Revenue from operations",
    "role": "sectionHeading",
    "bounding_regions": [{"page_number": 170}],
    "spans": [{"offset": 9000, "length": 27}],
}

_BASIS = {
    "default_basis": "unknown",
    "regions": [{"basis": "consolidated", "start_page": 100, "end_page": 200}],
}
_OCR = {"paragraphs": [_HEAD], "tables": [_FACE, _NOTE_TABLE]}


def _fog():
    si = build_statement_index(_OCR, _BASIS)
    ni = build_note_index(_OCR, _BASIS)
    return build_fog(_OCR, si, ni, doc_id="doc-1")


def test_face_and_note_items_both_land_in_one_basis_region():
    region = _fog()["bases"]["consolidated"]
    origins = {i["origin"] for i in region["line_items"]}
    assert origins == {"face", "note"}


def test_the_references_edge_connects_the_face_to_its_note():
    region = _fog()["bases"]["consolidated"]
    refs = [e for e in region["edges"] if e["type"] == "REFERENCES"]
    assert len(refs) == 1
    assert refs[0]["target"] == "nt:consolidated:10"


def test_diagnostics_report_note_resolution_per_basis():
    """A ref-grammar failure like RITES's 0/30 must be visible in one file."""
    diag = _fog()["diagnostics"]["consolidated"]
    assert diag["face_refs"] == 1
    assert diag["refs_resolved"] == 1
    assert diag["notes_indexed"] == 1
    assert diag["line_items"] == 5


def test_an_issuer_with_no_note_column_records_absent_not_zero_items():
    """RIL prints no note column on its P&L face. That is ABSENT, and the FOG
    must say so rather than look like a failed extraction."""
    face = {
        "bounding_regions": [{"page_number": 150}],
        "spans": [{"offset": 500, "length": 100}],
        "cells": [
            _cell(0, 0, "Particulars"), _cell(0, 1, "2025-26"),
            _cell(1, 0, "Total income"), _cell(1, 1, "100"),
            _cell(2, 0, "Total expenses"), _cell(2, 1, "70"),
            _cell(3, 0, "Profit before tax"), _cell(3, 1, "30"),
        ],
    }
    ocr = {"paragraphs": [], "tables": [face]}
    si = build_statement_index(ocr, _BASIS)
    ni = build_note_index(ocr, _BASIS)
    fog = build_fog(ocr, si, ni, doc_id="ril")
    assert fog["diagnostics"]["consolidated"]["face_refs"] == 0
    assert len(fog["bases"]["consolidated"]["line_items"]) == 3


def test_the_artifact_is_deterministic_for_content_addressing():
    """`f(x) == f(x)` in one process cannot fail for the reason it names: set
    iteration order for strings is stable WITHIN an interpreter and dict order
    is invisible to `==`. The property that matters is the canonical BYTES --
    the exact bytes stored and hashed -- plus an externally-fixed node order."""
    from fdi.core.serialization import canonical_json_bytes

    first = _fog()
    assert canonical_json_bytes(first) == canonical_json_bytes(_fog())
    region = first["bases"]["consolidated"]
    assert [i["id"] for i in region["line_items"]] == [
        "li:st:consolidated:profit_and_loss:r0",
        "li:st:consolidated:profit_and_loss:r1",
        "li:st:consolidated:profit_and_loss:r2",
        "li:st:consolidated:profit_and_loss:r3",
        "li:nt:consolidated:10:r0",
    ]
    assert [(e["type"], e["source"], e["target"]) for e in region["edges"]] == sorted(
        (e["type"], e["source"], e["target"]) for e in region["edges"]
    )


def test_the_body_hashes_the_same_under_a_different_hash_seed():
    """PYTHONHASHSEED is fixed per interpreter, so an implementation that dumped
    a `set` straight into `edges` would pass every in-process check and only
    break across worker processes -- which is exactly where the cache lives."""
    import json
    import os
    import subprocess
    import sys

    from fdi.core.serialization import canonical_json_bytes

    script = (
        "import json,sys;"
        "from fdi.core.fog_build import build_fog,build_note_index,build_statement_index;"
        "ocr,basis=json.loads(sys.stdin.read());"
        "si=build_statement_index(ocr,basis);ni=build_note_index(ocr,basis);"
        "sys.stdout.write(json.dumps(build_fog(ocr,si,ni,doc_id='doc-1'),"
        "sort_keys=True,separators=(',',':'),ensure_ascii=False))"
    )
    env = {**os.environ, "PYTHONHASHSEED": "1"}
    out = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script], input=json.dumps([_OCR, _BASIS]),
        capture_output=True, text=True, check=True, env=env,
    )
    assert out.stdout.encode("utf-8") == canonical_json_bytes(_fog())


def test_no_maps_to_edges_are_emitted_mapping_is_phase_3():
    region = _fog()["bases"]["consolidated"]
    assert not [e for e in region["edges"] if e["type"] == "MAPS_TO"]


def test_no_governed_by_edges_are_emitted_policy_is_domain_2():
    region = _fog()["bases"]["consolidated"]
    assert not [e for e in region["edges"] if e["type"] == "GOVERNED_BY"]


def test_malformed_input_yields_a_well_formed_empty_fog():
    fog = build_fog({}, {}, {}, doc_id="d")
    assert fog["doc_id"] == "d"
    assert fog["schema_version"] == 4


def test_the_stage_is_registered_with_the_right_deps():
    import fdi.pipeline.stages  # noqa: F401 - triggers registration
    from fdi.pipeline.registry import REGISTRY

    stage = REGISTRY.get("fog_objects")
    # `ocr` is DECLARED because the stage reads it: `run.py` builds input_hash
    # from declared deps only, so an undeclared read sits outside the cache key.
    assert stage.deps == ("ocr", "statement_index", "note_index", "structure")
    assert stage.version == 4


def test_the_dag_resolves_and_orders_fog_objects_after_its_indices():
    import fdi.pipeline.stages  # noqa: F401
    from fdi.pipeline.registry import REGISTRY
    from fdi.pipeline.resolver import DagResolver

    order = DagResolver(REGISTRY.all()).topological_order()
    assert order.index("statement_index") < order.index("fog_objects")
    assert order.index("note_index") < order.index("fog_objects")
    assert order.index("ocr") < order.index("fog_objects")
