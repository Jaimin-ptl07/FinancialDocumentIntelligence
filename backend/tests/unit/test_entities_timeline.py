from fdi.core.entities import build_entity_timeline


def _member(doc_id: str, version: int | None, concepts: list[str], ok: bool) -> dict[str, object]:
    return {
        "doc_id": doc_id,
        "doc_key": "acme.pdf",
        "version": version,
        "knowledge": {
            "section_count": 3,
            "concepts": concepts,
            "concept_count": len(concepts),
            "quality": {"ok": ok, "error_count": 0 if ok else 2, "warning_count": 1},
        },
    }


def test_build_entity_timeline_two_versions_ordered() -> None:
    members = [
        _member("d2", 2, ["NET_PROFIT"], True),
        _member("d1", 1, ["REVENUE", "NET_PROFIT"], True),
    ]
    out = build_entity_timeline("acme.pdf", members)
    assert out["schema_version"] == 1
    assert out["doc_key"] == "acme.pdf"
    assert out["member_count"] == 2
    assert "entity_id" not in out

    timeline = out["timeline"]
    assert [t["doc_id"] for t in timeline] == ["d1", "d2"]  # version-ordered, not input order
    assert [t["version"] for t in timeline] == [1, 2]
    assert timeline[0]["concepts"] == ["NET_PROFIT", "REVENUE"]
    assert timeline[1]["concepts"] == ["NET_PROFIT"]
    assert timeline[0]["section_count"] == 3
    assert timeline[0]["quality"] == {"ok": True, "error_count": 0, "warning_count": 1}
    assert out["concepts"] == ["NET_PROFIT", "REVENUE"]


def test_build_entity_timeline_missing_knowledge_degrades() -> None:
    members = [{"doc_id": "d1", "doc_key": "k", "version": 1, "knowledge": {}}]
    out = build_entity_timeline("k", members)
    t = out["timeline"][0]
    assert t["section_count"] == 0
    assert t["concept_count"] == 0
    assert t["quality_ok"] is False
    assert t["concepts"] == []


def test_build_entity_timeline_none_version_sorts_last() -> None:
    members = [
        {"doc_id": "d1", "doc_key": "k", "version": None, "knowledge": {}},
        {"doc_id": "d2", "doc_key": "k", "version": 1, "knowledge": {}},
    ]
    out = build_entity_timeline("k", members)
    assert [t["doc_id"] for t in out["timeline"]] == ["d2", "d1"]


def test_build_entity_timeline_empty_members() -> None:
    assert build_entity_timeline("k", []) == {
        "schema_version": 1, "doc_key": "k", "member_count": 0,
        "timeline": [], "concepts": [], "concept_presence": {},
    }
