import pytest

from fdi.core.crossdoc import cosine, document_vector


def test_document_vector_is_normalized_mean_of_chunk_vectors() -> None:
    emb = {"chunks": [{"vector": [3.0, 0.0]}, {"vector": [0.0, 4.0]}]}
    # mean = [1.5, 2.0], norm = 2.5 -> [0.6, 0.8]
    assert document_vector(emb) == pytest.approx([0.6, 0.8])


def test_document_vector_empty_or_malformed_returns_empty() -> None:
    assert document_vector({}) == []
    assert document_vector({"chunks": []}) == []
    assert document_vector({"chunks": "nope"}) == []
    assert document_vector({"chunks": [{"vector": []}]}) == []
    assert document_vector({"chunks": [{"vector": [0.0, 0.0]}]}) == []  # zero norm
    assert document_vector({"chunks": ["not-a-mapping"]}) == []


def test_document_vector_skips_mismatched_dimension_chunks() -> None:
    # first valid vector sets dim=2; the 3-d vector is skipped, not zero-padded
    emb = {"chunks": [{"vector": [3.0, 0.0]}, {"vector": [1.0, 1.0, 1.0]}]}
    assert document_vector(emb) == pytest.approx([1.0, 0.0])


def test_document_vector_rejects_non_numeric_and_bool() -> None:
    assert document_vector({"chunks": [{"vector": [1.0, "x"]}]}) == []
    assert document_vector({"chunks": [{"vector": [True, False]}]}) == []


def test_cosine_basic_cases() -> None:
    assert cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert cosine([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)


def test_cosine_guards() -> None:
    assert cosine([1.0], [1.0, 2.0]) == 0.0  # length mismatch
    assert cosine([], []) == 0.0
    assert cosine([0.0, 0.0], [1.0, 1.0]) == 0.0  # zero norm
