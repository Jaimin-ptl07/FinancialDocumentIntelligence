"""How a Company is found by the names people actually use (S2.4).

Pure functions in `fdi.core.company_names`: the registry searches and
resolves through these, so "Reliance Industries Ltd." and "RELIANCE
INDUSTRIES LIMITED" are the same name, and "RIL" is a whole-word match, never
a fragment of "April"."""

import pytest

from fdi.core.company_names import (
    identifier_key,
    is_same_name,
    matches_query,
    normalize_cin,
    normalize_identifier,
)

# --- one name, many spellings -----------------------------------------------


@pytest.mark.parametrize(
    "spelling",
    [
        "Reliance Industries Limited",
        "Reliance Industries Ltd.",
        "Reliance Industries Ltd",
        "RELIANCE INDUSTRIES LTD",
        "  reliance   industries, limited ",
    ],
)
def test_legal_suffix_punctuation_case_and_spacing_do_not_change_a_name(spelling: str) -> None:
    assert is_same_name(spelling, "Reliance Industries Limited")


def test_ampersand_and_and_are_the_same_name() -> None:
    assert is_same_name("Larsen & Toubro Ltd.", "Larsen and Toubro Limited")


def test_private_limited_abbreviation_is_the_same_name() -> None:
    assert is_same_name(
        "Silver Touch Technologies Pvt. Ltd.", "Silver Touch Technologies Private Limited"
    )


def test_different_companies_are_different_names() -> None:
    assert not is_same_name("Reliance Industries Limited", "Reliance Power Limited")


def test_nothing_is_never_a_name() -> None:
    assert not is_same_name("", "")
    assert not is_same_name("...", "Ltd.")


# --- search: whole words, in order ------------------------------------------


def test_a_query_matches_a_run_of_whole_words_in_a_name() -> None:
    assert matches_query("Reliance", "Reliance Industries Limited")
    assert matches_query("reliance industries", "Reliance Industries Limited")
    assert matches_query("Reliance Industries Ltd.", "Reliance Industries Limited")


def test_a_query_is_never_a_fragment_of_a_word() -> None:
    assert not matches_query("RIL", "April Industries Limited")
    assert not matches_query("Relian", "Reliance Industries Limited")


def test_an_exact_alias_or_ticker_matches() -> None:
    assert matches_query("RIL", "RIL")
    assert matches_query("ril", "RIL")
    assert matches_query("RELIANCE", "RELIANCE")


def test_an_empty_query_matches_nothing() -> None:
    assert not matches_query("", "Reliance Industries Limited")
    assert not matches_query("  .  ", "Reliance Industries Limited")


# --- typed identifiers: one canonical form, or rejected ---------------------


@pytest.mark.parametrize(
    ("kind", "raw", "canonical"),
    [
        ("nse_symbol", " reliance ", "RELIANCE"),
        ("nse_symbol", "m&m", "M&M"),
        ("nse_symbol", "bajaj-auto", "BAJAJ-AUTO"),
        ("bse_code", " 500325 ", "500325"),
        ("isin", "ine002a01018", "INE002A01018"),
        ("alias", "  RIL ", "RIL"),
    ],
)
def test_identifiers_are_stored_in_one_canonical_form(kind: str, raw: str, canonical: str) -> None:
    assert normalize_identifier(kind, raw) == canonical


@pytest.mark.parametrize(
    ("kind", "raw"),
    [
        ("nse_symbol", ""),
        ("nse_symbol", "RELIANCE INDUSTRIES"),
        ("bse_code", "50032"),
        ("bse_code", "RELI"),
        ("isin", "INE002A0101"),
        ("isin", "12E002A01018"),
        ("alias", "   "),
        ("alias", "..."),
        ("ticker", "RIL"),
    ],
)
def test_malformed_identifiers_are_rejected(kind: str, raw: str) -> None:
    assert normalize_identifier(kind, raw) is None


def test_aliases_differing_only_in_spelling_are_the_same_alias() -> None:
    assert identifier_key("alias", "RIL") == identifier_key("alias", "r.i.l") is not None
    assert identifier_key("alias", "Reliance Ltd.") == identifier_key("alias", "reliance limited")


def test_a_cin_is_upper_cased_and_checked() -> None:
    assert normalize_cin(" l17110mh1973plc019786 ") == "L17110MH1973PLC019786"
    assert normalize_cin("L17110MH1973PLC01978") is None
    assert normalize_cin("not a cin") is None
