"""Issuer identification: which Company issued a Report, read from its text.

Fixture passages are cut verbatim from the corpus's real Reports (TCS, RIL,
REMC, Silver Touch) -- page numbers included -- so the ranking is exercised on
the wording annual reports actually print, not on invented prose."""

from fdi.core.issuer import identify_issuer

TCS_CIN = "L22210MH1995PLC084781"
RIL_CIN = "L17110MH1973PLC019786"
REMC_CIN = "U93000DL2013GOI256661"
STTL_CIN = "L72200GJ1995PLC024465"


def _report(*sections: tuple[str | None, list[str], int]) -> tuple[dict, dict]:
    """Build `ast` + `provenance` artifacts from (heading, paragraphs, page)."""
    ast_sections = []
    node_provenance = []
    for i, (heading, paragraphs, page) in enumerate(sections):
        ast_sections.append({"heading": heading, "paragraphs": paragraphs})
        node_provenance.append({"node_id": f"s{i}", "page": page})
        for j in range(len(paragraphs)):
            node_provenance.append({"node_id": f"s{i}p{j}", "page": page})
    return {"sections": ast_sections}, {"node_provenance": node_provenance}


def test_tcs_cin_found_deep_in_the_report() -> None:
    ast, prov = _report(
        ("Letter to Shareholders", ["Tata Consultancy Services Limited delivered growth."], 4),
        (
            "CERTIFICATE OF NON-DISQUALIFICATION OF DIRECTORS",
            [
                "We have examined the relevant registers, records, forms, returns and "
                "disclosures received from the Directors of Tata Consultancy Services "
                f"Limited having CIN {TCS_CIN} and having registered office at 9th "
                "Floor, Nirmal Building, Nariman Point, Mumbai"
            ],
            104,
        ),
    )

    result = identify_issuer(ast, prov)

    assert result["cin"] == TCS_CIN
    assert result["confidence"] == "high"
    assert result["issuer_name"] == "Tata Consultancy Services Limited"
    (best,) = result["cin_candidates"]
    assert best["evidence"]["page"] == 104
    assert TCS_CIN in best["evidence"]["text"]


def _ril() -> tuple[dict, dict]:
    """RIL prints its own CIN once in the body while subsidiaries' CINs appear
    more often -- RRVL's twice, on two different pages."""
    rrvl = (
        "To, The Members, Reliance Retail Ventures Limited CIN: U51909MH2006PLC166166 "
        "4th Floor, Court House, Lokmanya Tilak Marg, Dhobi Talao, Mumbai- 400002."
    )
    return _report(
        ("Chairman's Statement", ["Reliance Industries Limited had a record year."], 3),
        ("Business Review", ["Reliance Industries Limited grew EBITDA."], 12),
        ("Reliance Industries Limited", ["Reliance Industries Limited at a glance."], 14),
        (
            "Certificate on Compliance with Code of Conduct",
            [
                "I have examined the relevant registers, records, forms, returns and "
                "disclosures received from the Directors of Reliance Industries Limited "
                f"having CIN {RIL_CIN} and registered office at 3rd Floor, Maker "
                "Chambers IV, 222, Nariman Point, Mumbai"
            ],
            34,
        ),
        (
            "Form No. MR-3 Secretarial Audit Report For the Financial Year",
            [
                "We have conducted the Secretarial Audit of the compliance of applicable "
                "statutory provisions and adherence to good corporate practices by Jio "
                "Platforms Limited [CIN: U72900GJ2019PLC110816] (hereinafter called the "
                "'Company') for the financial year ended March 31, 2026"
            ],
            35,
        ),
        ("Annexure A", [rrvl], 37),
        ("Corporate Governance Report", [rrvl], 38),
    )


def test_ril_own_cin_beats_more_frequent_subsidiary_cins() -> None:
    ast, prov = _ril()

    result = identify_issuer(ast, prov)

    assert result["cin"] == RIL_CIN
    assert result["confidence"] == "high"
    assert result["issuer_name"] == "Reliance Industries Limited"
    assert result["cin_candidates"][0]["evidence"]["page"] == 34
    assert {c["cin"] for c in result["cin_candidates"]} == {
        RIL_CIN, "U72900GJ2019PLC110816", "U51909MH2006PLC166166",
    }


def test_ril_back_cover_cin_on_its_own_line_still_reads_the_issuer_name() -> None:
    """The back cover prints the name, the exchange codes and the CIN as
    separate lines: the name is three lines up, under a registrar's heading."""
    ast, prov = _report(
        ("Reliance Industries Limited", ["Reliance Industries Limited at a glance."], 2),
        (
            "KFin Technologies Limited",
            [
                "Selenium Tower B, Plot 31-32, Gachibowli, Financial District, "
                "Nanakramguda, Hyderabad - 500 032",
                "Reliance Industries Limited Growth is Life",
                "BSE. 500325 NSE. RELIANCE",
                "BLOOMBERG. RELIANCE:IN",
                f"CIN. {RIL_CIN}",
            ],
            147,
        ),
    )

    result = identify_issuer(ast, prov)

    assert result["cin"] == RIL_CIN
    assert result["cin_candidates"][0]["evidence"] == {
        "node_id": "s1p4", "page": 147, "text": f"CIN. {RIL_CIN}",
    }


def test_remc_named_as_the_report_prints_it_not_in_capitals() -> None:
    ast, prov = _report(
        ("REMC Limited (A Joint Venture of Indian Railways and RITES Limited)", [], 1),
        ("Directors' Report", ["REMC Limited supplies renewable power to Indian Railways."], 9),
        (
            "CERTIFICATE",
            [
                "We have examined the compliance of conditions of Corporate Governance "
                f"by REMC LIMITED (CIN: {REMC_CIN}) for the period ended on 31st March, "
                "2025 as stipulated in the DPE guidelines"
            ],
            31,
        ),
    )

    result = identify_issuer(ast, prov)

    assert result["cin"] == REMC_CIN
    assert result["issuer_name"] == "REMC Limited"
    assert result["cin_candidates"][0]["evidence"]["page"] == 31


def test_subsidiary_described_right_after_the_issuers_name_does_not_win() -> None:
    """Silver Touch's Directors' report names the issuer, then gives the
    subsidiary's CIN in the same sentence; the registrar's CIN follows the
    registrar's name."""
    ast, prov = _report(
        ("ANNUAL REPORT 2023-24", ["Silver Touch Technologies Limited at a glance."], 1),
        (
            "GLOBAL PRESENCE",
            [f"Company Information Silver Touch Technologies Limited CIN: {STTL_CIN}"],
            19,
        ),
        (
            "Silver Touch Auto Tech Private Limited:",
            [
                "Silver Touch Auto Tech Private Limited, which is wholly owned subsidiary "
                "of Silver Touch Technologies Ltd, is a private limited company "
                "incorporated on July, 25 2022 with CIN: U72900GJ2022PTC134200."
            ],
            32,
        ),
        (
            "MEANS OF COMMUNICATION",
            [
                "M/s Link intime India Private Limited",
                "CIN: U67190MH1999PTC118368 C-101, 1st Floor, 247 Park, Vikhroli (West)",
                f"M/s Silver Touch Technologies Limited CIN: {STTL_CIN}",
            ],
            74,
        ),
    )

    result = identify_issuer(ast, prov)

    assert result["cin"] == STTL_CIN
    assert result["issuer_name"] == "Silver Touch Technologies Limited"


def test_no_cin_anywhere_is_not_identified_and_says_why() -> None:
    ast, prov = _report(("Directors' Report", ["Acme Widgets Limited had a good year."], 5))

    result = identify_issuer(ast, prov)

    assert result["cin"] is None
    assert result["confidence"] == "none"
    assert "no CIN" in result["reason"]
    assert result["cin_candidates"] == []


def test_total_on_malformed_input() -> None:
    for ast, prov in [
        ({}, {}),
        (None, None),
        ({"sections": "x"}, {"node_provenance": 3}),
        (
            {"sections": [None, {"heading": 5, "paragraphs": [None, 7]}]},
            {"node_provenance": [None]},
        ),
    ]:
        result = identify_issuer(ast, prov)  # type: ignore[arg-type]
        assert result["cin"] is None


def test_cin_split_by_ocr_spaces_is_normalised() -> None:
    spaced = "L22210 MH 1995 PLC 084781"
    ast, prov = _report(
        (
            "Notice",
            [f"Tata Consultancy Services Limited CIN: {spaced} Registered Office: Mumbai"],
            2,
        )
    )

    assert identify_issuer(ast, prov)["cin"] == TCS_CIN


def test_registered_name_spelled_out_even_when_abbreviated_more_often() -> None:
    ast, prov = _report(
        ("REMC Limited (A Joint Venture of Indian Railways and RITES Limited)", [], 1),
        ("Directors' Report", ["REMC Ltd. supplies power.", "REMC Ltd. grew."], 9),
        ("Notice", ["REMC Ltd. hired."], 12),
        (
            "CERTIFICATE",
            [f"Corporate Governance by REMC LIMITED (CIN: {REMC_CIN}) for the period"],
            31,
        ),
    )

    assert identify_issuer(ast, prov)["issuer_name"] == "REMC Limited"


def test_name_candidates_carry_evidence() -> None:
    ast, prov = _ril()

    names = {n["name"]: n for n in identify_issuer(ast, prov)["name_candidates"]}

    ril = names["Reliance Industries Limited"]
    assert ril["evidence"]["page"] == 34
    assert "Reliance Industries Limited" in ril["evidence"]["text"]


def test_cin_evidence_is_the_matched_passage_not_a_whole_long_paragraph() -> None:
    filler = "The Board reviewed the business in detail. " * 40
    ast, prov = _report(
        ("Notice", [f"{filler}Tata Consultancy Services Limited CIN: {TCS_CIN} {filler}"], 7)
    )

    evidence = identify_issuer(ast, prov)["cin_candidates"][0]["evidence"]

    assert TCS_CIN in evidence["text"]
    assert "Tata Consultancy Services Limited" in evidence["text"]
    assert len(evidence["text"]) < 400


def test_a_name_merely_containing_the_issuers_name_does_not_count_as_the_issuer() -> None:
    """"Arc Limited" is inside "Monarc Limited" as a substring, not as a name."""
    ast, prov = _report(
        ("Arc Limited", ["Arc Limited grew.", "Arc Limited hired.", "Arc Limited won."], 1),
        (
            "Directors' Report",
            ["Arc Limited CIN: L11111MH2001PLC111111 Registered Office: Pune"],
            9,
        ),
        ("Subsidiaries", ["Monarc Limited CIN: U22222MH2002PLC222222 Registered Office: Pune"], 30),
    )

    candidates = {c["cin"]: c["score"] for c in identify_issuer(ast, prov)["cin_candidates"]}

    assert candidates["L11111MH2001PLC111111"] - candidates["U22222MH2002PLC222222"] >= 5


# --- Fiscal year and period (S2.2) -------------------------------------------


def test_year_ended_31st_march_is_the_indian_fiscal_year_it_closes() -> None:
    ast, prov = _report(
        (
            "Standalone Statement of Profit and Loss for the year ended 31st March 2026",
            ["Revenue from operations 2,55,324"],
            101,
        ),
    )

    result = identify_issuer(ast, prov)

    assert result["fiscal_year"] == "FY2025-26"
    assert result["period_start"] == "2025-04-01"
    assert result["period_end"] == "2026-03-31"
    assert result["fiscal_year_confidence"] == "high"
    (best,) = result["fiscal_year_candidates"]
    assert best["fiscal_year"] == "FY2025-26"
    assert best["source"] == "statements"
    assert best["evidence"]["page"] == 101
    assert "year ended 31st March 2026" in best["evidence"]["text"]


def test_the_comparative_year_is_not_the_reports_fiscal_year() -> None:
    """Statements print last year's column too; it never outranks the
    Report's own year, however often or early it appears."""
    ast, prov = _report(
        ("Notes", ["Restated figures for the year ended 31st March 2025."], 60),
        (
            "Statement of Profit and Loss for the year ended 31st March 2026",
            ["Year ended 31st March 2026 Year ended 31st March 2025"],
            101,
        ),
        ("Notes", ["As reported for the year ended 31st March 2025."], 140),
    )

    result = identify_issuer(ast, prov)

    assert result["fiscal_year"] == "FY2025-26"
    assert [c["fiscal_year"] for c in result["fiscal_year_candidates"]] == [
        "FY2025-26", "FY2024-25",
    ]


def test_tcs_style_month_first_date_and_remc_period_ended_on() -> None:
    tcs, tcs_prov = _report(
        ("Directors' Report", ["for the financial year ended March 31, 2026"], 20),
    )
    remc, remc_prov = _report(
        (
            "CERTIFICATE",
            ["Corporate Governance by REMC LIMITED for the period ended on 31st March, 2025"],
            31,
        ),
    )

    assert identify_issuer(tcs, tcs_prov)["fiscal_year"] == "FY2025-26"
    remc_result = identify_issuer(remc, remc_prov)
    assert remc_result["fiscal_year"] == "FY2024-25"
    assert (remc_result["period_start"], remc_result["period_end"]) == (
        "2024-04-01", "2025-03-31",
    )


def test_a_half_year_is_not_the_reports_fiscal_year() -> None:
    ast, prov = _report(
        ("Annual Report 2025-26", [], 1),
        ("Results", ["Unaudited results for the half year ended 30th September 2026."], 9),
    )

    result = identify_issuer(ast, prov)

    assert result["fiscal_year"] == "FY2025-26"
    assert result["period_end"] == "2026-03-31"
    assert result["fiscal_year_candidates"] == [
        {
            "fiscal_year": "FY2025-26",
            "period_start": "2025-04-01",
            "period_end": "2026-03-31",
            "source": "cover",
            "evidence": {"node_id": "s0", "page": 1, "text": "Annual Report 2025-26"},
        }
    ]


def test_cover_and_statements_agreeing_is_high_confidence() -> None:
    ast, prov = _report(
        ("Integrated Annual Report 2025-26", ["Growth is Life"], 1),
        ("Balance Sheet", ["Consolidated figures for the year ended 31st March 2026"], 120),
    )

    result = identify_issuer(ast, prov)

    assert result["fiscal_year"] == "FY2025-26"
    assert result["fiscal_year_confidence"] == "high"
    assert {c["source"] for c in result["fiscal_year_candidates"]} == {"cover", "statements"}


def test_an_older_report_cited_early_is_not_the_reports_own_title() -> None:
    """Measured on RIL FY2025-26: page 18 cites "the Integrated Annual Report
    2022-23"; the Report's own title repeats as a running header after it."""
    ast, prov = _report(
        (
            "Materiality",
            ["Details can be found on Pg 164 of the Integrated Annual Report 2022-23."],
            18,
        ),
        ("Strategy", ["Integrated Annual Report 2025-26"], 25),
        ("Statement of Profit and Loss for the year ended 31st March 2026", ["Total"], 101),
        ("People", ["Integrated Annual Report 2025-26"], 47),
    )

    result = identify_issuer(ast, prov)

    assert result["fiscal_year"] == "FY2025-26"
    assert result["fiscal_year_confidence"] == "high"
    cover = [c for c in result["fiscal_year_candidates"] if c["source"] == "cover"]
    assert [(c["fiscal_year"], c["evidence"]["page"]) for c in cover] == [("FY2025-26", 25)]


def test_cover_and_statements_conflicting_is_reported_not_resolved() -> None:
    ast, prov = _report(
        ("Annual Report 2025-26", ["Growth is Life"], 1),
        (
            "Statement of Profit and Loss for the year ended 31st March 2025",
            ["Revenue from operations 1,000"],
            88,
        ),
    )

    result = identify_issuer(ast, prov)

    assert result["fiscal_year"] is None
    assert result["period_start"] is None
    assert result["fiscal_year_confidence"] == "low"
    assert "FY2025-26" in result["fiscal_year_reason"]
    assert "FY2024-25" in result["fiscal_year_reason"]
    assert {(c["source"], c["fiscal_year"]) for c in result["fiscal_year_candidates"]} == {
        ("cover", "FY2025-26"), ("statements", "FY2024-25"),
    }


def test_no_fiscal_year_printed_says_why() -> None:
    ast, prov = _report(("Directors' Report", ["Acme Widgets Limited had a good year."], 5))

    result = identify_issuer(ast, prov)

    assert result["fiscal_year"] is None
    assert result["fiscal_year_confidence"] == "none"
    assert result["fiscal_year_reason"]
    assert result["fiscal_year_candidates"] == []
