"""Upstream HTTP streaming with byte-range passthrough."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import AsyncIterator
from urllib.parse import unquote, urlsplit

import httpx

CHUNK_SIZE = 128 * 1024
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class RemoteError(Exception):
    """Raised when an upstream request cannot be started."""


@dataclass
class Fetch:
    status: int
    headers: dict[str, str]
    body: AsyncIterator[bytes]
    filename: str = ""
    mime: str = "application/octet-stream"
    size: int | None = None
    url: str = ""
    extra: dict = field(default_factory=dict)


def filename_from_url(url: str) -> str:
    try:
        name = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
    except Exception:
        return "download"
    return name or "download"


async def _empty_body() -> AsyncIterator[bytes]:
    if False:  # pragma: no cover - makes this an async generator
        yield b""


def empty_fetch(status: int = 416, size: int = 0) -> Fetch:
    headers = {"accept-ranges": "bytes", "content-range": f"bytes */{size}"}
    return Fetch(status=status, headers=headers, body=_empty_body(), size=0)


async def open_remote(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    range_header: str | None = None,
    method: str = "GET",
    content: bytes | None = None,
    filename: str = "",
    mime: str = "",
) -> Fetch:
    """Open an upstream URL and return a streaming Fetch.

    The HTTP client and response are closed when the body iterator finishes or
    is closed. Range requests are forwarded untouched so seeking works.
    """
    request_headers = {"User-Agent": DEFAULT_UA, "Accept": "*/*"}
    if headers:
        request_headers.update({k: v for k, v in headers.items() if v})
    if range_header:
        request_headers["Range"] = range_header

    client = httpx.AsyncClient(
        follow_redirects=True,
        timeout=httpx.Timeout(connect=20.0, read=None, write=60.0, pool=20.0),
    )
    try:
        request = client.build_request(method, url, headers=request_headers, content=content)
        response = await client.send(request, stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        raise RemoteError(f"upstream request failed: {exc}") from exc
    except Exception:
        await client.aclose()
        raise

    out_headers: dict[str, str] = {}
    for name in (
        "content-type",
        "content-length",
        "content-range",
        "accept-ranges",
        "content-encoding",
        "etag",
        "last-modified",
    ):
        value = response.headers.get(name)
        if value:
            out_headers[name] = value
    # httpx decodes nothing on aiter_raw, so content-length stays valid.
    if "accept-ranges" not in out_headers and response.status_code < 400:
        out_headers["accept-ranges"] = "bytes"

    upstream_type = (response.headers.get("content-type") or "").split(";")[0].strip()
    final_mime = mime or upstream_type or "application/octet-stream"
    final_name = filename or filename_from_url(str(response.url))
    size: int | None = None
    length = response.headers.get("content-length")
    if length and "content-encoding" not in out_headers:
        try:
            size = int(length)
        except ValueError:
            size = None

    async def body() -> AsyncIterator[bytes]:
        try:
            async for chunk in response.aiter_raw(CHUNK_SIZE):
                yield chunk
        finally:
            await response.aclose()
            await client.aclose()

    return Fetch(
        status=response.status_code,
        headers=out_headers,
        body=body(),
        filename=final_name,
        mime=final_mime,
        size=size,
        url=str(response.url),
    )
