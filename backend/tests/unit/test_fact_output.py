"""M1 Fact shape: extraction records carry tag + origin, indexable by tag."""

from fdi.core.extractor import FieldSpec, assemble_result, index_by_tag


def _spec(tag: str, origin: str = "extracted") -> FieldSpec:
    return FieldSpec(
        field_name="Revenue from Sale of Products",
        data_type="decimal",
        retrieval_query="revenue",
        keywords=("revenue",),
        extraction_prompt="Extract revenue.",
        enum_values=None,
        module_id=1,
        module_name="Profit and Loss",
        tag=tag,
        origin=origin,
    )


def test_record_carries_tag_and_origin():
    rec = assemble_result(_spec("RFSP"), {"value": 1234.5, "confidence": 0.9}, [])
    assert rec["tag"] == "RFSP"
    assert rec["origin"] == "extracted"
    assert rec["value"] == 1234.5
    # existing contract keys still present
    assert set(rec) >= {"field_name", "value", "source", "confidence"}


def test_index_by_tag_maps_and_skips_untagged():
    recs = [
        assemble_result(_spec("RFSP"), {"value": 1.0}, []),
        assemble_result(_spec(""), {"value": 2.0}, []),  # blank tag -> skipped
        assemble_result(_spec("GST"), {"value": 3.0}, []),
    ]
    idx = index_by_tag(recs)
    assert set(idx) == {"RFSP", "GST"}
    assert idx["RFSP"]["value"] == 1.0


def test_index_by_tag_first_wins_on_repeat():
    recs = [
        assemble_result(_spec("TL"), {"value": 10.0}, []),
        assemble_result(_spec("TL"), {"value": 20.0}, []),
    ]
    assert index_by_tag(recs)["TL"]["value"] == 10.0
