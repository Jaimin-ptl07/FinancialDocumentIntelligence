"""S3 artifact store.

Targets the generic S3 API only — never a MinIO-specific feature. MinIO is the
local-dev container; production is S3/Azure Blob. That is what makes swapping it
a compose change rather than a code change.

The minio SDK is sync, so calls go through asyncio.to_thread. contextvars DO
propagate into to_thread, so correlation IDs survive.
"""

import asyncio
import io
import json
from datetime import timedelta
from typing import Any

from minio import Minio

from fdi.core.serialization import canonical_json


class ArtifactStore:
    def __init__(
        self, client: Minio, bucket: str, *, presign_client: Minio | None = None
    ) -> None:
        self._client = client
        self._bucket = bucket
        # Presigning embeds the signing client's endpoint in the URL. `client` is built
        # for the in-network address (minio:9000), which a browser cannot resolve.
        # `presign_client`, when supplied, is built for a host-reachable address and is
        # used ONLY to sign GET URLs. Falls back to `client` when absent, so callers
        # that never presign (the worker) pass nothing and are unaffected.
        self._presign_client = presign_client or client

    async def ensure_bucket(self) -> None:
        exists = await asyncio.to_thread(self._client.bucket_exists, self._bucket)
        if not exists:
            await asyncio.to_thread(self._client.make_bucket, self._bucket)

    async def put_json(self, key: str, obj: Any) -> str:
        body = canonical_json(obj).encode("utf-8")
        await asyncio.to_thread(
            self._client.put_object,
            self._bucket,
            key,
            io.BytesIO(body),
            len(body),
            content_type="application/json",
        )
        return key

    async def put_bytes(self, key: str, data: bytes, content_type: str) -> str:
        await asyncio.to_thread(
            self._client.put_object,
            self._bucket,
            key,
            io.BytesIO(data),
            len(data),
            content_type=content_type,
        )
        return key

    async def get_bytes(self, key: str) -> bytes:
        return await self.get_raw(key)

    async def get_raw(self, key: str) -> bytes:
        def _get() -> bytes:
            # get_object OUTSIDE the try: it can raise, and `response` would then be
            # unbound in the finally. The SDK's own example needs an `if response:`
            # guard for exactly this.
            response = self._client.get_object(self._bucket, key)
            try:
                data: bytes = response.read()
                return data
            finally:
                # The minio SDK's documented contract. Measured redundant TODAY --
                # read() drains to EOF and urllib3 releases the connection itself on
                # the way out, so removing these leaks nothing (0 new sockets over 25
                # GETs). Keep them anyway: the same 25 GETs leak 24 sockets the moment
                # the body stops being read whole. This is the cheap insurance that
                # makes a future streaming read safe, not dead code.
                response.close()
                response.release_conn()

        return await asyncio.to_thread(_get)

    async def get_json(self, key: str) -> Any:
        return json.loads(await self.get_raw(key))

    async def exists(self, key: str) -> bool:
        """True iff an object exists at key. stat_object raises S3Error(NoSuchKey) when absent."""
        from minio.error import S3Error

        def _stat() -> bool:
            try:
                self._client.stat_object(self._bucket, key)
                return True
            except S3Error as exc:
                if exc.code in {"NoSuchKey", "NoSuchObject"}:
                    return False
                raise

        return await asyncio.to_thread(_stat)

    async def presigned_get_url(self, key: str, ttl_seconds: int) -> str:
        url: str = await asyncio.to_thread(
            self._presign_client.presigned_get_object,
            self._bucket,
            key,
            timedelta(seconds=ttl_seconds),
        )
        return url
