from fdi.core.graph import numeric_id_sort_key, order_reading_chain


def _n(i: str) -> dict[str, object]:
    return {"id": i, "type": "section", "label": i}


def _ro(source: str, target: str) -> dict[str, object]:
    return {"source": source, "target": target, "type": "READING_ORDER"}


def test_orders_a_linear_chain() -> None:
    nodes = [_n("s1"), _n("s0"), _n("s2")]  # deliberately NOT in chain order
    edges = [_ro("s0", "s1"), _ro("s1", "s2")]
    assert [n["id"] for n in order_reading_chain(nodes, edges)] == ["s0", "s1", "s2"]


def test_empty_input() -> None:
    assert order_reading_chain([], []) == []


def test_single_node_no_edges() -> None:
    assert [n["id"] for n in order_reading_chain([_n("s0")], [])] == ["s0"]


def test_two_disconnected_chains_both_walked_heads_sorted() -> None:
    nodes = [_n("b0"), _n("b1"), _n("a0"), _n("a1")]
    edges = [_ro("b0", "b1"), _ro("a0", "a1")]
    # heads are "a0" and "b0" -- sorted -> "a" chain first
    assert [n["id"] for n in order_reading_chain(nodes, edges)] == ["a0", "a1", "b0", "b1"]


def test_cycle_does_not_infinite_loop() -> None:
    nodes = [_n("s0"), _n("s1")]
    edges = [_ro("s0", "s1"), _ro("s1", "s0")]  # a 2-cycle: no node lacks an incoming edge
    result = order_reading_chain(nodes, edges)
    assert {n["id"] for n in result} == {"s0", "s1"}
    assert len(result) == 2  # each node appears exactly once, walk terminates


def test_edge_referencing_unknown_id_is_ignored() -> None:
    nodes = [_n("s0")]
    edges = [_ro("s0", "ghost")]
    assert [n["id"] for n in order_reading_chain(nodes, edges)] == ["s0"]


def test_non_reading_order_edge_type_is_ignored() -> None:
    nodes = [_n("s0"), _n("s1")]
    edges = [{"source": "s0", "target": "s1", "type": "CONTAINS"}]
    # no READING_ORDER edge between them -> both are heads, sorted by id
    assert [n["id"] for n in order_reading_chain(nodes, edges)] == ["s0", "s1"]


def test_numeric_id_sort_key_sorts_t2_before_t10_not_lexicographically() -> None:
    ids = ["d1:t10", "d1:t2"]
    assert sorted(ids, key=lambda i: numeric_id_sort_key("d1", i)) == ["d1:t2", "d1:t10"]


def test_numeric_id_sort_key_handles_bare_unnamespaced_ids() -> None:
    ids = ["f10", "f2"]
    assert sorted(ids, key=lambda i: numeric_id_sort_key("d1", i)) == ["f2", "f10"]


def test_numeric_id_sort_key_non_parsing_id_sorts_after_real_ints() -> None:
    ids = ["d1:t2", "d1:ghost"]
    assert sorted(ids, key=lambda i: numeric_id_sort_key("d1", i)) == ["d1:t2", "d1:ghost"]
