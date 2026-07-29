"""Pure parsing of subtotal formulas out of Particulars text."""

from fdi.core.formula import operand_matches, parse_formula, parse_sr, sr_in_range


def test_parse_sr():
    assert parse_sr("1.1") == (1, 1)
    assert parse_sr("3") == (3,)
    assert parse_sr("4.1.1") == (4, 1, 1)
    assert parse_sr("x") is None
    assert parse_sr("") is None


def test_sum_range():
    f = parse_formula("Total Gross Revenue (Total of 1.1 to 1.5)")
    assert f == {"op": "sum", "operands": [("range", (1, 1), (1, 5))]}


def test_sum_union_of_range_and_single():
    f = parse_formula("Total Current Assets (Total of 4.1 to 4.4 & 5 to 6)")
    assert f["op"] == "sum"
    assert ("range", (4, 1), (4, 4)) in f["operands"]
    assert ("range", (5,), (6,)) in f["operands"]


def test_sum_single_and_range():
    f = parse_formula("Total NCA (Total of 1.2 & 1.4 to 1.11)")
    assert ("single", (1, 2)) in f["operands"]
    assert ("range", (1, 4), (1, 11)) in f["operands"]


def test_sum_two_singles():
    f = parse_formula("Total Non-Current & Current Assets (Total of 3 & 7)")
    assert f == {"op": "sum", "operands": [("single", (3,)), ("single", (7,))]}


def test_paren_range_without_total_of():
    f = parse_formula("Total Borrowings (1.1 to 1.13)")
    assert f == {"op": "sum", "operands": [("range", (1, 1), (1, 13))]}


def test_diff():
    f = parse_formula("Net Revenue From Operations (1 minus 2)")
    assert f == {"op": "diff", "left": ("single", (1,)), "right": ("single", (2,))}
    f2 = parse_formula("Net Sales Nature Wise (1.3 minus 2)")
    assert f2 == {"op": "diff", "left": ("single", (1, 3)), "right": ("single", (2,))}


def test_plus_minus_is_ambiguous_none():
    assert parse_formula("Profit before minority interest (11 plus / minus 12)") is None


def test_no_reference_returns_none():
    assert parse_formula("Revenue from Sale of Products") is None
    assert parse_formula("") is None


def test_range_membership():
    assert sr_in_range((1, 3), (1, 1), (1, 5))
    assert not sr_in_range((1, 6), (1, 1), (1, 5))
    assert not sr_in_range((1, 1, 2), (1, 1), (1, 5))  # deeper depth excluded
    assert operand_matches(("range", (1, 1), (1, 5)), (1, 4))
    assert operand_matches(("single", (3,)), (3,))
    assert not operand_matches(("single", (3,)), (4,))
