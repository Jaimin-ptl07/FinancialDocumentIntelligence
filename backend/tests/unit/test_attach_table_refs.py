from fdi.core.ast import attach_table_refs


def _ast_two_sections() -> dict[str, object]:
    return {
        "schema_version": 2,
        "title": "ACME LTD",
        "title_source_index": 0,
        "sections": [
            {
                "heading": "Balance Sheet",
                "heading_source_index": 1,
                "paragraphs": ["Total Assets 100"],
                "paragraph_source_indices": [2],
            },
            {
                "heading": "Directors' Report",
                "heading_source_index": 3,
                "paragraphs": ["The board is pleased."],
                "paragraph_source_indices": [4],
            },
        ],
    }


def test_tables_and_figures_land_on_the_right_section_by_hierarchy() -> None:
    ast = _ast_two_sections()
    ocr = {
        "tables": [{"caption": None}, {"caption": None}],
        "figures": [{"caption": "Org chart"}],
        "sections": [
            {
                "elements": [
                    "/paragraphs/1",  # opens Balance Sheet (heading_source_index=1)
                    "/tables/0",
                    "/paragraphs/3",  # opens Directors' Report (heading_source_index=3)
                    "/tables/1",
                    "/figures/0",
                ]
            },
        ],
    }
    result = attach_table_refs(ast, ocr)
    assert result["sections"][0]["table_refs"] == [0]
    assert result["sections"][0]["figure_refs"] == []
    assert result["sections"][1]["table_refs"] == [1]
    assert result["sections"][1]["figure_refs"] == [{"index": 0, "caption": "Org chart"}]


def test_nested_subsection_reached_via_sections_ref_is_walked_in_order() -> None:
    ast = _ast_two_sections()
    ocr = {
        "tables": [{"caption": None}],
        "figures": [],
        "sections": [
            {"elements": ["/paragraphs/1", "/sections/1", "/paragraphs/3"]},
            {"elements": ["/tables/0"]},  # nested subsection, reached via /sections/1
        ],
    }
    result = attach_table_refs(ast, ocr)
    # the nested section's table ref is encountered while Balance Sheet (s0) is
    # still open (the "/paragraphs/3" ref that opens s1 comes AFTER /sections/1)
    assert result["sections"][0]["table_refs"] == [0]
    assert result["sections"][1]["table_refs"] == []


def test_table_before_any_heading_attaches_to_existing_preamble_section() -> None:
    ast = {
        "sections": [
            {
                "heading": None,
                "heading_source_index": None,
                "paragraphs": ["Preamble text."],
                "paragraph_source_indices": [0],
            },
            {
                "heading": "Section One",
                "heading_source_index": 1,
                "paragraphs": ["In section."],
                "paragraph_source_indices": [2],
            },
        ],
    }
    ocr = {
        "tables": [{"caption": None}],
        "figures": [],
        "sections": [{"elements": ["/tables/0", "/paragraphs/1"]}],
    }
    result = attach_table_refs(ast, ocr)
    assert result["sections"][0]["heading"] is None
    assert result["sections"][0]["table_refs"] == [0]
    assert result["sections"][1]["table_refs"] == []


def test_table_before_any_heading_creates_synthetic_preamble_when_absent() -> None:
    ast = {
        "sections": [
            {
                "heading": "Section One",
                "heading_source_index": 0,
                "paragraphs": ["In section."],
                "paragraph_source_indices": [1],
            },
        ],
    }
    ocr = {
        "tables": [{"caption": None}],
        "figures": [],
        "sections": [{"elements": ["/tables/0", "/paragraphs/0"]}],
    }
    result = attach_table_refs(ast, ocr)
    sections = result["sections"]
    assert isinstance(sections, list)
    assert len(sections) == 2
    assert sections[0]["heading"] is None
    assert sections[0]["table_refs"] == [0]
    assert sections[0]["figure_refs"] == []
    # the original section shifted from index 0 -> 1, and still resolves
    assert sections[1]["heading"] == "Section One"
    assert sections[1]["table_refs"] == []


def test_cyclic_sections_ref_terminates_without_hanging() -> None:
    ast = _ast_two_sections()
    ocr = {
        "tables": [{"caption": None}],
        "figures": [],
        "sections": [
            {"elements": ["/paragraphs/1", "/sections/1"]},
            {"elements": ["/tables/0", "/sections/0"]},  # cycles back to root
        ],
    }
    result = attach_table_refs(ast, ocr)
    # must terminate; the table ref on the cyclic branch is still recorded once
    assert result["sections"][0]["table_refs"] == [0]


def test_malformed_and_dangling_refs_are_skipped_never_raise() -> None:
    ast = _ast_two_sections()
    ocr = {
        "tables": [{"caption": None}],
        "figures": [{"caption": "Fig"}],
        "sections": [
            {
                "elements": [
                    "/paragraphs/1",
                    "not-a-ref",
                    123,
                    "/widgets/9",
                    "/tables/99",  # out of range
                    "/figures/99",  # out of range
                    "/sections/50",  # dangling section ref
                    "/tables/0",
                ]
            },
        ],
    }
    result = attach_table_refs(ast, ocr)
    assert result["sections"][0]["table_refs"] == [0]
    assert result["sections"][0]["figure_refs"] == []


def test_empty_or_absent_ocr_sections_gives_every_section_empty_refs() -> None:
    ast = _ast_two_sections()
    result = attach_table_refs(ast, {"tables": [], "figures": [], "sections": []})
    assert result["sections"][0]["table_refs"] == []
    assert result["sections"][0]["figure_refs"] == []
    assert result["sections"][1]["table_refs"] == []
    assert result["sections"][1]["figure_refs"] == []

    result2 = attach_table_refs(ast, {})
    assert result2["sections"][0]["table_refs"] == []
    assert result2["sections"][1]["figure_refs"] == []


def test_figure_ref_with_blank_or_missing_caption_still_records_empty_string() -> None:
    ast = _ast_two_sections()
    ocr = {
        "tables": [],
        "figures": [{"caption": ""}, {}],
        "sections": [
            {"elements": ["/paragraphs/1", "/figures/0", "/figures/1"]},
        ],
    }
    result = attach_table_refs(ast, ocr)
    assert result["sections"][0]["figure_refs"] == [
        {"index": 0, "caption": ""},
        {"index": 1, "caption": ""},
    ]


def test_build_ast_itself_is_unmodified_by_this_module() -> None:
    # attach_table_refs must not mutate the ast argument in place.
    ast = _ast_two_sections()
    original = {
        "schema_version": ast["schema_version"],
        "title": ast["title"],
        "title_source_index": ast["title_source_index"],
        "sections": [dict(s) for s in ast["sections"]],  # type: ignore[union-attr]
    }
    attach_table_refs(ast, {"tables": [], "figures": [], "sections": []})
    assert ast == original
