"""Content-addressed artifact keys and the hashes that key them. Pure."""

import hashlib

from fdi.core.serialization import canonical_json_bytes


def output_hash(body: object) -> str:
    """sha256 of a body's canonical bytes — recorded per stage_run for downstream."""
    return hashlib.sha256(canonical_json_bytes(body)).hexdigest()


def root_input_hash(checksum: bytes | None) -> str:
    """Input hash for a stage with no upstream: the document's source checksum."""
    return hashlib.sha256(checksum or b"").hexdigest()


def derived_input_hash(upstream_output_hashes: list[str]) -> str:
    """Input hash for a derived stage: hash of its deps' output_hashes, in order."""
    joined = "\n".join(upstream_output_hashes).encode("utf-8")
    return hashlib.sha256(joined).hexdigest()


def artifact_key(doc_id: str, stage: str, version: int, input_hash: str) -> str:
    return f"artifacts/{doc_id}/{stage}/v{version}/{input_hash}.json"
