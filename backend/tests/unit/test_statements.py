"""Statement discovery and face parsing -- the deterministic bootstrap."""

from fdi.core.statements import (
    MIN_ANCHORS,
    find_statement_tables,
    note_windows,
    parse_face_table,
    parse_note_ref,
    score_statement,
    tables_in_window,
    verify_footing,
)


def _heading(text: str, page: int, offset: int) -> dict:
    return {"role": "sectionHeading", "content": text,
            "bounding_regions": [{"page_number": page}],
            "spans": [{"offset": offset, "length": len(text)}]}


def _table(rows: list[list[str]], page: int = 253, offset: int = 1000) -> dict:
    cells = [
        {"row_index": r, "column_index": c, "content": v}
        for r, row in enumerate(rows) for c, v in enumerate(row)
    ]
    return {"cells": cells, "bounding_regions": [{"page_number": page}],
            "spans": [{"offset": offset, "length": 100}]}


PL_FACE = _table([
    ["", "Note", "Year ended March 31, 2026", "Year ended March 31, 2025"],
    ["Revenue from operations", "10", "2,20,938", "2,14,853"],
    ["Other income", "11", "10,036", "9,642"],
    ["TOTAL INCOME", "", "2,30,974", "2,24,495"],
    ["Expenses", "", "", ""],
    ["Employee benefit expenses", "12", "1,12,142", "1,07,300"],
    ["TOTAL EXPENSES", "", "1,64,101", "1,61,847"],
    ["PROFIT BEFORE EXCEPTIONAL ITEMS AND TAX", "", "66,873", "62,648"],
    ["PROFIT BEFORE TAX", "", "66,873", "62,648"],
    ["Deferred tax", "15", "(468)", "(232)"],
    ["PROFIT FOR THE YEAR", "", "50,179", "46,585"],
])


def test_reference_grammar_spans_issuer_styles():
    """TCS numbers notes 13(a); RITES numbers them 2.01. The earlier grammar
    parsed 0 of RITES's 30 references."""
    assert parse_note_ref("10") == "10"
    assert parse_note_ref("13(a)") == "13(a)"
    assert parse_note_ref("2.01") == "2.01"
    assert parse_note_ref("2.08.2") == "2.08.2"


def test_ranges_and_conjunctions_are_not_references():
    """'1 to 44' names the whole notes section, not one note."""
    assert parse_note_ref("1 to 44") == ""
    assert parse_note_ref("1 & 2") == ""
    assert parse_note_ref("2,20,938") == ""
    assert parse_note_ref(None) == ""


def test_statement_is_identified_by_its_own_rows():
    assert score_statement(PL_FACE, "profit_and_loss") >= 4
    assert score_statement(PL_FACE, "balance_sheet") == 0
    found = find_statement_tables([PL_FACE], "profit_and_loss", 1, 400)
    assert found and found[0].page == 253


def test_tables_outside_the_basis_region_are_not_candidates():
    """TCS carries consolidated AND standalone faces; scoping picks one."""
    assert find_statement_tables([PL_FACE], "profit_and_loss", 1, 100) == []


def test_note_column_is_found_once_from_the_header():
    """Scanning rows for 'a cell that looks like a ref' read the VALUE 45 as
    note 45. Anchoring on the header makes that impossible."""
    rows = parse_face_table(PL_FACE)
    by_label = {r.label: r for r in rows}
    assert by_label["Revenue from operations"].ref == "10"
    # '2,20,938' sits in a value column and must never be read as a reference
    assert all(r.ref in ("", "10", "11", "12", "15") for r in rows)


def test_amounts_parse_deterministically_including_negatives():
    rows = {r.label: r for r in parse_face_table(PL_FACE)}
    assert rows["Revenue from operations"].current == 220938.0
    assert rows["Revenue from operations"].prior == 214853.0
    assert rows["TOTAL INCOME"].current == 230974.0
    assert rows["Deferred tax"].current == -468.0


def test_a_row_with_no_amount_becomes_the_caption_for_rows_below():
    """RIL prints 'a) Owners of the Company' under three captions; the caption
    is what disambiguates it."""
    rows = {r.label: r for r in parse_face_table(PL_FACE)}
    assert "Expenses" not in rows                       # caption, not a row
    assert rows["Employee benefit expenses"].caption == "Expenses"
    assert rows["Revenue from operations"].caption == ""


def test_footing_verifies_the_right_table_was_chosen():
    rows = parse_face_table(PL_FACE)
    assert verify_footing(rows, ["TOTAL INCOME"], ["TOTAL EXPENSES"],
                          "PROFIT BEFORE EXCEPTIONAL") is True
    # a missing line yields None -- unverifiable, which is not the same as false
    assert verify_footing(rows, ["NOT PRESENT"], [], "TOTAL INCOME") is None


def test_offset_window_separates_two_notes_on_one_page():
    """TCS p301 holds note 13(a) and 13(b); page granularity cannot split them."""
    a = _table([["Raw materials", "41"]], page=301, offset=897_095)
    b = _table([["Fees to external consultants", "22,306"]], page=301, offset=897_486)
    assert tables_in_window([a, b], (897_000, 897_400)) == [a]
    assert tables_in_window([a, b], (897_400, 898_050)) == [b]


def test_heading_grammar_matches_the_reference_grammar():
    """REGRESSION. The heading matcher used to be a caller argument, and the
    caller's version accepted only flat `10.` headings while parse_note_ref
    accepted dotted refs. RITES's face cites 2.17-2.30 and its notes are headed
    "2.17 Revenue From Operations", so all 10 references resolved to nothing.
    Whatever parse_note_ref reads off the face, note_windows must find."""
    paras = [
        _heading("10. Revenue from operations", 300, 1_000),      # TCS, flat
        _heading("(a) Cost of equipment", 301, 2_000),            # TCS, sub-note
        _heading("2.17 Revenue From Operations", 79, 3_000),      # RITES, dotted
        _heading("2.18", 79, 4_000),                              # RITES, bare
    ]
    windows = note_windows(paras, 1, 400)
    assert set(windows) == {"10", "10(a)", "2.17", "2.18"}


def test_a_note_window_ends_where_the_next_note_begins():
    paras = [_heading("13. Expenses", 301, 897_095),
             _heading("14. Finance costs", 301, 897_486)]
    windows = note_windows(paras, 300, 310)
    assert (windows["13"].start, windows["13"].end) == (897_095, 897_486)
    assert windows["14"].start == 897_486       # last note runs on to a tail
    # the title travels with the window: a note the face never cites is
    # reachable only by its own heading.
    assert windows["13"].title == "Expenses"
    assert windows["13"].page == 301


def test_headings_outside_the_basis_region_are_ignored():
    paras = [_heading("10. Revenue from operations", 120, 1_000)]
    assert note_windows(paras, 244, 360) == {}


def test_a_late_row_mentioning_a_note_is_not_the_header():
    """REGRESSION. RIL's cash flow ends with 'Closing Balance of Cash and Cash
    Equivalents (Refer Note 9)' in the LAST row. Matching 'note' anywhere made
    that the header row, and every row at or before it was skipped -- the whole
    44-row statement parsed to nothing."""
    table = _table([
        ["", "2025-26", "2024-25"],
        ["A. Cash Flow from Operating Activities", "", ""],
        ["Net Profit Before Tax", "54,076", "46,128"],
        ["Depreciation / Amortisation", "17,105", "17,981"],
        ["Closing Balance of Cash and Cash Equivalents (Refer Note 9)", "3,012", "2,201"],
    ])
    rows = parse_face_table(table)
    labels = [r.label for r in rows]

    assert "Net Profit Before Tax" in labels
    assert len(rows) == 3
    # the prose row is a VALUE row, and nothing in this table is a note column
    assert all(r.ref == "" for r in rows)
    assert rows[0].caption == "A. Cash Flow from Operating Activities"


def test_a_real_note_column_header_is_still_found():
    rows = parse_face_table(PL_FACE)
    assert next(r for r in rows if r.label == "Revenue from operations").ref == "10"


def test_a_cash_flow_statement_scores_clear_of_the_threshold():
    """The original anchors put TCS and RIL at EXACTLY MIN_ANCHORS, so one bad
    OCR row would have lost the statement. Every line here is one an Ind AS 7
    statement prints."""
    cf = _table([
        ["A. Cash Flow from Operating Activities", "", ""],
        ["Net Profit Before Tax", "54,076", "46,128"],
        ["Operating profit before working capital changes", "71,181", "64,109"],
        ["Cash generated from operations", "68,220", "61,004"],
        ["B. Cash Flow from Investing Activities", "", ""],
        ["C. Cash Flow from Financing Activities", "", ""],
        ["Cash and cash equivalents at the beginning of the year", "2,201", "1,904"],
        ["Closing Balance of Cash and Cash Equivalents (Refer Note 9)", "3,012", "2,201"],
    ], page=61)

    assert score_statement(cf, "cash_flow") > MIN_ANCHORS
    assert score_statement(cf, "balance_sheet") == 0
    found = find_statement_tables([cf], "cash_flow", 1, 400)
    assert found and found[0].statement == "cash_flow"
    # and it still parses -- the trailing "Refer Note 9" must not eat the table
    assert len(parse_face_table(cf)) == 5


def test_everything_is_total_on_malformed_input():
    assert note_windows([None, 7, {}], 1, 9) == {}
    assert find_statement_tables([None, 7], "profit_and_loss", 1, 9) == []
    assert parse_face_table({}) == []
    assert parse_face_table({"cells": "junk"}) == []
    assert tables_in_window([None], (0, 10)) == []
    assert score_statement({}, "profit_and_loss") == 0
