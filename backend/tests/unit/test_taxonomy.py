"""Pure parsing of the customer taxonomy raw grid -> normalized taxonomy doc."""

import json
import pathlib

from fdi.core.taxonomy import parse_taxonomy

# A hand-built mini P&L-shaped sheet exercising every mapping rule.
_MINI = {
    "schema_version": 1,
    "sheets": [
        {
            "title": "B-1-PROFIT & LOSS",
            "grid": [
                ["B 1 - PROFIT & LOSS", None, None, None, None, None, None, None, None],
                ["BLOCKID", "ICICI TAG", "Sr. No.", None, "Particulars", None,
                 "Comments", "redirection (DatasetNo-2)", "Q3Redirection"],
                [None, None, None, None, "Revenue From Operations", None, None, None, None],
                ["PL01", "RFSP", "1.1", None, "Sale of  Products", None, None,
                 "NOT CONSIDERED", None],
                ["PL01", "rfss", "1.2", None, "Sale of Services", None, "new", None, None],
                ["PL01", "RFOGRS", None, None, "Total Gross Rev\n (Total 1.1 to 1.5)", None,
                 "donot tag", None, None],
                ["PL01", "TOTX", None, None, "A kept total", None, "total to tag", None, None],
                ["PL01", "DROPME", None, None, "Excluded", None, "not considered", None, None],
                ["PL01", "OLDSUB", None, None, "Old subtotal", None, "donot tag - old tag",
                 None, None],
                ["PL01", "REDIR", None, None, "Redirected", None, None, "NONOI", "INTOI"],
            ],
        }
    ],
}


def _rows(doc):
    return doc["statements"][0]["rows"]


def _by_tag(doc, tag):
    for r in _rows(doc):
        if r["tag"] == tag:
            return r
    raise AssertionError(f"tag {tag} not found")


def test_statement_and_columns_mapped():
    doc = parse_taxonomy(_MINI)
    st = doc["statements"][0]
    assert st["statement"] == "profit_and_loss"
    assert st["sheet"] == "B-1-PROFIT & LOSS"


def test_tag_uppercased_and_particulars_whitespace_collapsed():
    doc = parse_taxonomy(_MINI)
    r = _by_tag(doc, "RFSS")  # 'rfss' upper-cased
    assert r["tag"] == "RFSS"
    r2 = _by_tag(doc, "RFSP")
    assert r2["particulars"] == "Sale of Products"  # double space collapsed


def test_section_header_row_detected():
    doc = parse_taxonomy(_MINI)
    headers = [r for r in _rows(doc) if r["is_section_header"]]
    assert any(r["particulars"] == "Revenue From Operations" for r in headers)
    assert all(r["tag"] is None for r in headers)


def test_flag_semantics():
    doc = parse_taxonomy(_MINI)
    assert _by_tag(doc, "RFSS")["flag"] == "new"
    assert _by_tag(doc, "RFSS")["origin"] == "extracted"
    assert _by_tag(doc, "RFSS")["is_new"] is True

    assert _by_tag(doc, "RFOGRS")["flag"] == "derived"
    assert _by_tag(doc, "RFOGRS")["origin"] == "derived"

    assert _by_tag(doc, "OLDSUB")["flag"] == "derived"  # 'donot tag - old tag'
    assert _by_tag(doc, "OLDSUB")["origin"] == "derived"

    assert _by_tag(doc, "TOTX")["flag"] == "total_tagged"
    assert _by_tag(doc, "TOTX")["origin"] == "extracted"

    assert _by_tag(doc, "DROPME")["flag"] == "excluded"
    assert _by_tag(doc, "DROPME")["excluded"] is True


def test_redirection_captured():
    doc = parse_taxonomy(_MINI)
    r = _by_tag(doc, "REDIR")
    assert r["redirect_dataset2"] == "NONOI"
    assert r["redirect_dataset1"] == "INTOI"
    # 'NOT CONSIDERED' in a redirection cell is NOT a rollup target
    assert _by_tag(doc, "RFSP")["redirect_dataset2"] is None


def test_duplicate_tag_warns():
    dup = json.loads(json.dumps(_MINI))
    dup["sheets"][0]["grid"].append(
        ["PL01", "RFSP", "9.9", None, "Duplicate of RFSP", None, None, None, None]
    )
    doc = parse_taxonomy(dup)
    assert any("RFSP" in w and "duplicate" in w.lower() for w in doc["warnings"])


# --- Against the committed real taxonomy grid (deterministic) ---

_RAW_PATH = pathlib.Path(__file__).resolve().parents[2] / "docs" if False else None
_REAL = (
    pathlib.Path(__file__).resolve().parents[3]
    / "docs" / "architecture" / "taxonomy" / "taxonomy.raw.json"
)


def test_real_taxonomy_tag_counts():
    raw = json.loads(_REAL.read_text(encoding="utf-8"))
    doc = parse_taxonomy(raw)
    counts = {}
    for st in doc["statements"]:
        tags = {r["tag"] for r in st["rows"] if r["tag"] and not r["excluded"]}
        counts[st["statement"]] = len(tags)
    # From structural analysis: ~224 / 133 / 106 / 49 unique tags.
    assert counts["profit_and_loss"] >= 200
    assert counts["assets"] >= 120
    assert counts["liabilities"] >= 100
    assert counts["equity"] >= 45
    assert len(doc["statements"]) == 4
