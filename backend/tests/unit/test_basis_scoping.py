"""Basis must be decided per DOCUMENT: a per-unit fallback mixes statement sets."""

from fdi.core.basis import basis_is_ambiguous, basis_regions, resolve_effective_basis

_BOTH = {
    "default_basis": "unknown",
    "regions": [
        {"basis": "consolidated", "start_page": 163, "end_page": 243},
        {"basis": "standalone", "start_page": 244, "end_page": 360},
    ],
}
_CONSOLIDATED_ONLY = {
    "default_basis": "unknown",
    "regions": [{"basis": "consolidated", "start_page": 40, "end_page": 300}],
}


def test_preference_is_honoured_when_the_document_has_it():
    assert resolve_effective_basis(_BOTH, "standalone") == "standalone"
    assert resolve_effective_basis(_BOTH, "consolidated") == "consolidated"


def test_document_without_the_preferred_basis_adopts_the_one_it_has():
    """RIL is a consolidated-only integrated report; asking for standalone must
    not leave each unit to decide for itself."""
    assert resolve_effective_basis(_CONSOLIDATED_ONLY, "standalone") == "consolidated"


def test_widest_region_wins_when_several_compete():
    artifact = {
        "regions": [
            {"basis": "segment", "start_page": 10, "end_page": 12},
            {"basis": "consolidated", "start_page": 20, "end_page": 200},
        ]
    }
    assert resolve_effective_basis(artifact, "standalone") == "consolidated"


def test_no_artifact_leaves_behaviour_exactly_as_before():
    assert resolve_effective_basis({}, "standalone") == "standalone"
    assert resolve_effective_basis({"regions": "junk"}, "standalone") == "standalone"
    assert resolve_effective_basis(_BOTH, "both") == "both"


def test_ambiguity_is_what_makes_an_unfiltered_fallback_unsafe():
    assert basis_is_ambiguous(_BOTH) is True          # fallback could mix
    assert basis_is_ambiguous(_CONSOLIDATED_ONLY) is False  # cannot mix
    assert basis_is_ambiguous({}) is False


def test_regions_are_parsed_defensively():
    junk = {"regions": [None, 7, {"basis": "x"}, {"basis": "y", "start_page": 5,
                                                  "end_page": 1}]}
    assert basis_regions(junk) == []
    assert basis_regions({"regions": [{"basis": "c", "start_page": 1,
                                       "end_page": 9}]}) == [("c", 1, 9)]
