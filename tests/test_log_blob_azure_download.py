"""Targeted tests for AzureLogBlobBackend.get_download_response.

The Azure backend used to 302-redirect the browser to a SAS URL on
``*.blob.core.windows.net``. That broke the UI's client-side zip flow
because the Blob container has no CORS policy, so a browser fetch()
following the redirect trips the cross-origin check and throws
``TypeError: Failed to fetch``.

These tests lock in the new behaviour: download responses stream the
blob bytes back through the CMS (same-origin) using ``chunks()`` so the
CMS never buffers the whole tar.gz.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from cms.services import log_blob


class _FakeDownloader:
    """Mimics the shape of the async Azure ``StorageStreamDownloader``."""

    def __init__(self, chunks: list[bytes]):
        self._chunks = chunks
        self.size = sum(len(c) for c in chunks)

    def chunks(self):
        async def _iter():
            for chunk in self._chunks:
                yield chunk

        return _iter()


def _make_backend(downloader: _FakeDownloader) -> log_blob.AzureLogBlobBackend:
    backend = log_blob.AzureLogBlobBackend.__new__(log_blob.AzureLogBlobBackend)
    backend._account_name = "acct"
    backend._account_key = "key"
    backend._connection_string = ""
    backend._service_client = MagicMock()

    blob_client = MagicMock()
    blob_client.download_blob = AsyncMock(return_value=downloader)
    backend._blob_client = MagicMock(return_value=blob_client)  # type: ignore[assignment]
    return backend


@pytest.mark.asyncio
async def test_get_download_response_streams_bytes_not_redirect(tmp_path):
    from fastapi.responses import StreamingResponse, RedirectResponse

    downloader = _FakeDownloader([b"hello", b"world"])
    backend = _make_backend(downloader)

    resp = await backend.get_download_response("dev/r1.tar.gz", "r1.tar.gz")

    assert isinstance(resp, StreamingResponse)
    assert not isinstance(resp, RedirectResponse)
    assert resp.media_type == "application/gzip"
    assert resp.headers["content-disposition"] == 'attachment; filename="r1.tar.gz"'
    # Content-Length is populated when the downloader reports a size.
    assert resp.headers.get("content-length") == "10"

    # The body iterator yields the exact chunks from the downloader.
    collected = []
    async for chunk in resp.body_iterator:
        collected.append(chunk)
    assert b"".join(collected) == b"helloworld"


@pytest.mark.asyncio
async def test_get_download_response_omits_content_length_when_unknown():
    downloader = _FakeDownloader([b"abc"])
    downloader.size = None  # type: ignore[assignment]
    backend = _make_backend(downloader)

    resp = await backend.get_download_response("dev/r2.tar.gz", "r2.tar.gz")

    # No Content-Length when the downloader doesn't report it.
    assert "content-length" not in {k.lower() for k in resp.headers.keys()}


@pytest.mark.asyncio
async def test_get_download_response_no_redirect_for_same_origin_fetch():
    """Regression: a UI fetch() following a 302 to *.blob.core.windows.net
    trips CORS. The new response stays same-origin (no Location header)."""
    from fastapi.responses import StreamingResponse

    downloader = _FakeDownloader([b"payload"])
    backend = _make_backend(downloader)

    resp = await backend.get_download_response("dev/r3.tar.gz", "r3.tar.gz")

    assert isinstance(resp, StreamingResponse)
    assert "location" not in {k.lower() for k in resp.headers.keys()}
    assert resp.status_code == 200
