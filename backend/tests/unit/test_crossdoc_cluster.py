from fdi.core.crossdoc import cluster, consolidate_entity


def _k(concepts: list[str], ok: bool = True, errors: int = 0, warnings: int = 0,
       section_count: int = 0, concept_count: int | None = None) -> dict[str, object]:
    return {
        "schema_version": 1,
        "section_count": section_count,
        "concepts": concepts,
        "concept_count": len(concepts) if concept_count is None else concept_count,
        "quality": {"ok": ok, "error_count": errors, "warning_count": warnings},
    }


def test_cluster_two_groups_and_a_singleton() -> None:
    vectors = {
        "a": [1.0, 0.0],
        "b": [0.99, 0.14],   # ~cos 0.99 with a
        "c": [0.0, 1.0],     # orthogonal to a/b -> its own entity
    }
    assert cluster(vectors, 0.85) == [["a", "b"], ["c"]]


def test_cluster_single_linkage_chains_transitively() -> None:
    # a~b and b~c but a NOT ~c directly; single-linkage still merges all three.
    # b is symmetric (equal angle to both a and c) so cos(a,b) == cos(b,c) ==
    # 1/sqrt(2) ~= 0.7071 (deliberately, not the plan's [0.72, 0.69], whose
    # cos(b,c) computes to ~0.6919 -- below 0.70 -- and so never chains).
    vectors = {
        "a": [1.0, 0.0],
        "b": [1.0, 1.0],     # ~cos 0.7071 with a, ~0.7071 with c
        "c": [0.0, 1.0],
    }
    assert cluster(vectors, 0.70) == [["a", "b", "c"]]
    # at a stricter threshold a and c split off from b's reach
    assert cluster(vectors, 0.99) == [["a"], ["b"], ["c"]]


def test_cluster_is_order_independent() -> None:
    base = {"a": [1.0, 0.0], "b": [0.99, 0.14], "c": [0.0, 1.0]}
    reordered = {"c": base["c"], "b": base["b"], "a": base["a"]}
    assert cluster(base, 0.85) == cluster(reordered, 0.85)


def test_cluster_empty() -> None:
    assert cluster({}, 0.85) == []


def test_consolidate_entity_folds_members_version_ordered() -> None:
    members = [
        {"doc_id": "d2", "doc_key": "acme", "version": 2,
         "knowledge": _k(["MARGIN", "REVENUE"], section_count=13)},
        {"doc_id": "d1", "doc_key": "acme", "version": 1,
         "knowledge": _k(["REVENUE"], warnings=1, section_count=12)},
    ]
    entity = consolidate_entity(members)
    assert entity["schema_version"] == 1
    assert entity["entity_id"] == "d1"           # min doc_id
    assert entity["member_count"] == 2
    assert [m["version"] for m in entity["members"]] == [1, 2]   # version-ordered
    assert entity["concepts"] == ["MARGIN", "REVENUE"]           # sorted union
    assert entity["concept_presence"] == {"MARGIN": [2], "REVENUE": [1, 2]}
    assert entity["quality_trend"] == [
        {"version": 1, "ok": True, "error_count": 0, "warning_count": 1},
        {"version": 2, "ok": True, "error_count": 0, "warning_count": 0},
    ]


def test_consolidate_entity_version_none_sorts_last_and_uses_doc_id_presence() -> None:
    members = [
        {"doc_id": "z", "doc_key": None, "version": None, "knowledge": _k(["TAX"])},
        {"doc_id": "a", "doc_key": "k", "version": 1, "knowledge": _k(["TAX"])},
    ]
    entity = consolidate_entity(members)
    assert [m["doc_id"] for m in entity["members"]] == ["a", "z"]   # None ordered last
    assert entity["concept_presence"] == {"TAX": [1, "z"]}          # doc_id for the None-version


def test_consolidate_entity_missing_knowledge_degrades() -> None:
    members = [{"doc_id": "a", "doc_key": "k", "version": 1}]  # no knowledge key
    entity = consolidate_entity(members)
    assert entity["members"] == [
        {"doc_id": "a", "doc_key": "k", "version": 1,
         "section_count": 0, "concept_count": 0, "quality_ok": False}
    ]
    assert entity["concepts"] == []
    assert entity["concept_presence"] == {}
    assert entity["quality_trend"] == [
        {"version": 1, "ok": False, "error_count": 0, "warning_count": 0}
    ]
