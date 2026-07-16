"""Content-addressed keys and the hashes that feed them. All pure, all deterministic."""

from fdi.pipeline.keys import (
    artifact_key,
    derived_input_hash,
    output_hash,
    root_input_hash,
)


def test_output_hash_is_deterministic_and_canonical() -> None:
    # Key order must not matter: canonical_json sorts keys.
    assert output_hash({"a": 1, "b": 2}) == output_hash({"b": 2, "a": 1})
    assert len(output_hash({"a": 1})) == 64  # sha256 hex


def test_root_input_hash_handles_null_checksum() -> None:
    assert root_input_hash(None) == root_input_hash(b"")
    assert root_input_hash(b"abc") != root_input_hash(b"abd")


def test_derived_input_hash_is_order_sensitive() -> None:
    assert derived_input_hash(["aa", "bb"]) != derived_input_hash(["bb", "aa"])
    assert len(derived_input_hash(["aa"])) == 64


def test_artifact_key_layout() -> None:
    assert (
        artifact_key("doc1", "ping", 1, "deadbeef")
        == "artifacts/doc1/ping/v1/deadbeef.json"
    )
