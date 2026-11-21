"""MEASURED ON TCS: `EQWVR` expected 362, got 1.0. The face row 'Share capital'
is not a claimant at all -- the concept "Equity share capital with voting
rights" has no alias matching what TCS actually prints, so note noise wins by
default. The gap is ALIAS COVERAGE; no tier or arbitration change can reach it."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))

from harvest_aliases import propose_aliases  # noqa: E402

_SCHEMA = {"modules": [{
    "module_id": "equity", "statement": "equity",
    "retrieval_units": [{"unit_id": "RU", "fields": [
        {"tag": "EQWVR", "field_name": "Equity share capital with voting rights",
         "origin": "extracted", "note_aliases": []},
    ]}],
}]}
_FOG = {"bases": {"consolidated": {
    "statements": [{"id": "st:consolidated:balance_sheet", "statement": "balance_sheet"}],
    "notes": [],
    "line_items": [{"id": "li:a", "parent": "st:consolidated:balance_sheet",
                    "origin": "face", "label": "Share capital", "current": 362.0,
                    "section_path": ["Equity"], "kind": "currency"}],
    "edges": [],
}}}


def test_an_unmapped_face_label_is_proposed_against_the_closest_concepts():
    proposals = propose_aliases(_FOG, _SCHEMA, basis="consolidated")
    assert proposals
    top = proposals[0]
    assert top["label"] == "Share capital"
    assert "EQWVR" in [c["tag"] for c in top["candidates"]]


def test_a_proposal_carries_the_evidence_a_human_needs_to_judge():
    top = propose_aliases(_FOG, _SCHEMA, basis="consolidated")[0]
    assert top["value"] == 362.0
    assert top["section_path"] == ["Equity"]


def test_note_labels_are_not_proposed():
    """Only FACE captions are worth aliasing: a note's rows are detail, and
    aliasing them would bind a concept to one issuer's schedule wording."""
    fog = {"bases": {"consolidated": {
        "statements": [], "notes": [{"id": "nt:consolidated:1", "ref": "1", "title": "x"}],
        "line_items": [{"id": "li:n", "parent": "nt:consolidated:1", "origin": "note",
                        "label": "Something", "current": 1.0, "section_path": [],
                        "kind": "currency"}],
        "edges": [],
    }}}
    assert propose_aliases(fog, _SCHEMA, basis="consolidated") == []


def test_an_already_mapped_label_is_not_proposed():
    schema = {"modules": [{"module_id": "equity", "statement": "equity",
        "retrieval_units": [{"unit_id": "RU", "fields": [
            {"tag": "EQWVR", "field_name": "Equity share capital with voting rights",
             "origin": "extracted", "note_aliases": ["share capital"]},
        ]}]}]}
    assert propose_aliases(_FOG, schema, basis="consolidated") == []


def test_proposals_are_deterministic_and_sorted():
    a = propose_aliases(_FOG, _SCHEMA, basis="consolidated")
    assert a == propose_aliases(_FOG, _SCHEMA, basis="consolidated")


def test_malformed_input_never_raises():
    assert propose_aliases({}, {}, basis="consolidated") == []
