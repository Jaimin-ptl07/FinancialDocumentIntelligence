def test_tool_schemas_has_seven_entries_all_dispatched() -> None:
    from fdi.pipeline.agent_tools import _DISPATCH, TOOL_SCHEMAS

    assert len(TOOL_SCHEMAS) == 7
    names = {s["function"]["name"] for s in TOOL_SCHEMAS}
    assert names == set(_DISPATCH.keys())


def test_every_schema_required_is_subset_of_properties() -> None:
    from fdi.pipeline.agent_tools import TOOL_SCHEMAS

    for schema in TOOL_SCHEMAS:
        params = schema["function"]["parameters"]
        required = set(params.get("required", []))
        properties = set(params["properties"].keys())
        assert required <= properties, schema["function"]["name"]


def test_every_schema_has_a_non_empty_description() -> None:
    from fdi.pipeline.agent_tools import TOOL_SCHEMAS

    for schema in TOOL_SCHEMAS:
        assert schema["function"]["description"].strip()
