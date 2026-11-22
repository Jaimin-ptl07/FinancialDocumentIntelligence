"""Whether a Report is ready to research, from its latest run per stage."""

from fdi.pipeline.companies import processing_status


def test_a_searchable_report_is_ready_even_if_a_later_derivation_failed() -> None:
    assert processing_status({"ocr": "done", "index": "done", "fog_reason": "failed"}) == "ready"


def test_a_failed_stage_before_it_is_searchable_is_failed() -> None:
    assert processing_status({"ocr": "done", "chunks": "failed"}) == "failed"


def test_otherwise_it_is_still_processing() -> None:
    assert processing_status({}) == "processing"
    assert processing_status({"ocr": "done", "index": "running"}) == "processing"
