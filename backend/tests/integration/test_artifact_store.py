"""ArtifactStore against a real MinIO, over the generic S3 API only.

The suite deliberately reaches past the SDK in two places -- it fetches the
presigned URL over plain HTTP, and it inspects urllib3's connection pool --
because both are claims about what a *client outside this process* sees, and an
assertion made through the same SDK that produced the value cannot see either.
"""

from __future__ import annotations

import threading
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from minio import Minio

from fdi.platform.obs.logging import (
    bind_correlation_id,
    clear_correlation_id,
    get_correlation_id,
)
from fdi.platform.storage.artifact_store import ArtifactStore

pytestmark = pytest.mark.integration


@pytest.fixture
async def store(minio_client: Minio) -> ArtifactStore:
    s = ArtifactStore(minio_client, bucket="test-artifacts")
    await s.ensure_bucket()
    return s


async def test_ensure_bucket_is_idempotent(store: ArtifactStore) -> None:
    await store.ensure_bucket()
    await store.ensure_bucket()


async def test_put_then_get_roundtrips(store: ArtifactStore) -> None:
    key = await store.put_json("a/b/ping.json", {"hello": "world", "n": 1})
    assert key == "a/b/ping.json"
    assert await store.get_json("a/b/ping.json") == {"hello": "world", "n": 1}


async def test_put_json_body_is_canonical(store: ArtifactStore) -> None:
    """Global Constraint 6: the body is a pure function of the input, so key
    order in the source dict must not change the stored bytes."""
    await store.put_json("k1.json", {"b": 1, "a": 2})
    await store.put_json("k2.json", {"a": 2, "b": 1})
    assert await store.get_raw("k1.json") == await store.get_raw("k2.json")
    assert await store.get_raw("k1.json") == b'{"a":2,"b":1}'


async def test_get_missing_raises(store: ArtifactStore) -> None:
    from minio.error import S3Error

    with pytest.raises(S3Error):
        await store.get_json("does/not/exist.json")


async def test_presigned_url_is_returned(store: ArtifactStore, minio_endpoint: str) -> None:
    """Fetch the URL. `startswith("http")` and `"p.json" in url` prove almost nothing.

    A presigned URL is the one thing here whose only consumer is OUTSIDE this
    process -- a browser. It embeds the endpoint THIS client was configured with,
    so a URL that is signed correctly but points at a hostname only the signer can
    resolve is a URL that passes every in-process assertion and 404s in the
    address bar. The only assertion that can tell those apart is an actual GET.

    The unsigned control request is the other half: without it, a `presigned_get_url`
    that returned a plain unsigned object URL against a public bucket would fetch
    200 and look identical to a correctly signed one.
    """
    await store.put_json("p.json", {"x": 1})
    url = await store.presigned_get_url("p.json", ttl_seconds=60)
    assert url.startswith("http")
    assert "p.json" in url

    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    assert parsed.netloc == minio_endpoint, (
        f"presigned URL points at {parsed.netloc!r} but the client is configured for "
        f"{minio_endpoint!r} -- a URL nobody outside this process can resolve"
    )
    assert query["X-Amz-Expires"] == ["60"], "ttl_seconds never reached the signature"
    assert "X-Amz-Signature" in query

    async with httpx.AsyncClient() as client:
        signed = await client.get(url)
        unsigned = await client.get(f"{parsed.scheme}://{parsed.netloc}{parsed.path}")

    assert signed.status_code == 200, f"presigned URL is not fetchable: {signed.text}"
    assert signed.content == b'{"x":1}', "presigned URL served bytes we never stored"
    assert signed.headers["content-type"] == "application/json"
    assert "content-encoding" not in signed.headers, (
        "S3 re-encoded the body on the wire -- stored bytes are no longer a pure "
        "function of the input"
    )
    assert unsigned.status_code == 403, (
        "the same URL without its signature was served anyway -- the signature is "
        "decoration and the bucket is public"
    )


async def test_correlation_id_survives_to_thread(
    store: ArtifactStore, minio_client: Minio, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stated reason `to_thread` was chosen at all, with nothing watching it.

    Global Constraint 4 establishes that contextvars is copy-IN / no-copy-OUT
    across `asyncio.run()`. `to_thread` is a DIFFERENT mechanism and the module
    docstring makes a separate, stronger claim about it -- that a correlation ID
    bound on the loop is visible inside the worker thread. This test is the only
    thing that checks it.

    The thread-identity assertion is load-bearing. Replace `asyncio.to_thread(_get)`
    with a plain `_get()` and the correlation ID still "survives" trivially -- the
    test would stay green while proving nothing about to_thread. Asserting the body
    ran off the calling thread is what makes the other assertion mean something.
    """
    await store.put_json("cid.json", {"x": 1})

    observed: dict[str, Any] = {}
    real_get_object = minio_client.get_object

    def spy(*args: Any, **kwargs: Any) -> Any:
        observed["correlation_id"] = get_correlation_id()
        observed["thread"] = threading.current_thread().ident
        return real_get_object(*args, **kwargs)

    monkeypatch.setattr(minio_client, "get_object", spy)

    bind_correlation_id("cid-survives-to-thread")
    try:
        assert await store.get_raw("cid.json") == b'{"x":1}'
    finally:
        clear_correlation_id()

    assert observed["thread"] != threading.current_thread().ident, (
        "the SDK call ran on the calling thread -- to_thread was bypassed, so this "
        "test can say nothing about contextvars crossing a thread boundary"
    )
    assert observed["correlation_id"] == "cid-survives-to-thread", (
        "correlation ID did NOT propagate into to_thread -- the module docstring's "
        "central claim is false and log lines from inside the SDK are unattributable"
    )


@pytest.mark.parametrize(
    ("obj", "expected"),
    [
        pytest.param({}, b"{}", id="empty-dict"),
        pytest.param({"a": ""}, b'{"a":""}', id="empty-string-value"),
        pytest.param({"inr": "₹", "e": "é"}, b'{"e":"\xc3\xa9","inr":"\xe2\x82\xb9"}',
                     id="multi-byte-utf8"),
    ],
)
async def test_put_json_uploads_every_byte(
    store: ArtifactStore, request: pytest.FixtureRequest, obj: Any, expected: bytes
) -> None:
    """`put_object`'s length argument counts BYTES, and canonical_json emits real UTF-8.

    `canonical_json` sets `ensure_ascii=False` on purpose -- annual reports carry
    the rupee sign. That makes `len(body)` and `len(canonical_json(obj))` two
    different numbers, and put_object reads exactly `length` bytes off the stream.
    Encode-then-measure is correct; measure-then-encode would upload a prefix.

    Verified to bite: changing `len(body)` to `len(canonical_json(obj))` leaves the
    ascii params green and turns `multi-byte-utf8` RED -- the rupee sign is 3 bytes
    counted as 1 char, so two bytes fall off the end and the stored object is
    truncated mid-codepoint into invalid JSON.

    The empty params pin the other end: a 2-byte body is the smallest thing this
    store can ever write, and `length=0` is unreachable through put_json because no
    JSON value encodes to zero bytes.
    """
    # Keyed off the param id, not hash() -- str/bytes hashing is PYTHONHASHSEED-salted,
    # so that would write a different key every run into a bucket nothing cleans up.
    key = f"len/{request.node.callspec.id}.json"
    await store.put_json(key, obj)
    assert await store.get_raw(key) == expected
    assert await store.get_json(key) == obj


async def test_exists_reflects_presence(store: ArtifactStore) -> None:
    key = "artifacts/doc1/ping/v1/abc.json"
    assert await store.exists(key) is False
    await store.put_json(key, {"kind": "ping"})
    assert await store.exists(key) is True


async def test_put_and_get_bytes(store: ArtifactStore) -> None:
    key = await store.put_bytes("documents/deadbeef.pdf", b"%PDF-1.7 ...", "application/pdf")
    assert key == "documents/deadbeef.pdf"
    assert await store.get_bytes("documents/deadbeef.pdf") == b"%PDF-1.7 ..."


# There is deliberately NO test that `get_raw`'s `finally: close(); release_conn()`
# prevents a connection leak, because on this code path it provably does not do
# anything -- and a green test claiming otherwise would be worse than no test.
# Measured against this container (urllib3 2.7.0, pool maxsize=10), new sockets
# opened over 25 sequential GETs:
#
#     full read, no close/release at all .......  0
#     partial read (stream not drained), none ... 24
#     partial read, with close+release_conn .....  1
#
# `response.read()` drains to EOF, and urllib3's `_error_catcher` already calls
# `release_conn()` on the way out (`if self._original_response.isclosed()`), so the
# connection is back in the pool before our `finally` runs. The cleanup is the
# minio SDK's documented contract and it stays -- it is what stops the middle row
# above from happening the day someone streams a large artifact instead of
# read()-ing it whole. It is simply not observable from here today.
