"""Storage node connectors.

A node is one place files can live. The connector interface is deliberately
small so new providers stay easy to add:

    list(path)         -> [FileItem]
    fetch(path, range) -> Fetch
    upload(path, iter) -> bytes written
    mkdir/delete/rename
    direct(path)       -> Resolved | None   (only for providers that expose URLs)

Nodes that are link-only (TeraBox, plain HTTP) still work as sources: their
links resolve through `resolvers.resolve_direct`.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import secrets
import shutil
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import quote, unquote, urljoin, urlsplit

import httpx

from . import oauth
from .config import settings
from .remote import Fetch, open_remote
from .resolvers import Resolved
from .utils import (
    RangeNotSatisfiable,
    base_name,
    guess_mime,
    join_path,
    normalize_path,
    parent_path,
    parse_range,
    safe_join,
    split_name,
)


class NodeError(Exception):
    """A node operation failed in a way worth showing to the user."""


class Unsupported(NodeError):
    pass


@dataclass
class FileItem:
    name: str
    path: str
    is_dir: bool = False
    size: int = 0
    modified: int | None = None
    mime: str = ""

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if not data["mime"]:
            data["mime"] = "inode/directory" if self.is_dir else guess_mime(self.name)
        return data


def _config_of(node) -> dict[str, Any]:
    raw = node["config"] if node is not None else "{}"
    if isinstance(raw, dict):
        return raw
    try:
        data = json.loads(raw or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _parse_time(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if text.isdigit():
        return int(text)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            try:
                parsed = datetime.fromisoformat(text.split(".")[0] + "+00:00")
            except ValueError:
                return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


class Connector:
    kind = "base"
    capabilities: set[str] = set()

    def __init__(self, node) -> None:
        self.node = node
        self.node_id = int(node["id"]) if node is not None else 0
        self.name = node["name"] if node is not None else self.kind
        self.config = _config_of(node)

    async def ping(self) -> tuple[bool, str]:
        return True, "ok"

    async def list(self, path: str) -> list[FileItem]:
        raise Unsupported(f"{self.kind} nodes cannot be browsed")

    async def fetch(self, path: str, range_header: str | None = None) -> Fetch:
        raise Unsupported(f"{self.kind} nodes cannot be read directly")

    async def upload(
        self,
        path: str,
        body: AsyncIterator[bytes],
        size: int | None = None,
        mime: str | None = None,
    ) -> int:
        raise Unsupported(f"{self.kind} nodes are read-only")

    async def mkdir(self, path: str) -> None:
        raise Unsupported(f"{self.kind} nodes are read-only")

    async def delete(self, path: str) -> None:
        raise Unsupported(f"{self.kind} nodes are read-only")

    async def rename(self, path: str, new_path: str) -> None:
        raise Unsupported(f"{self.kind} nodes are read-only")

    async def direct(self, path: str) -> Resolved | None:
        return None

    def capabilities_list(self) -> list[str]:
        return sorted(self.capabilities)


class OAuthMixin:
    """Shared OAuth token refresh for cloud-drive connectors."""

    def _init_oauth(self) -> None:
        self.token = str(self.config.get("access_token") or "").strip()
        self.refresh_token = str(self.config.get("refresh_token") or "").strip()
        try:
            self.token_expires_at = int(self.config.get("token_expires_at") or 0)
        except (TypeError, ValueError):
            self.token_expires_at = 0
        self.has_credentials = bool(self.token or self.refresh_token)

    async def _ensure_token(self) -> str:
        """Return a usable access token, refreshing it when possible."""
        if self.token and (
            not self.token_expires_at or self.token_expires_at > time.time() + 60
        ):
            return self.token
        if self.refresh_token:
            try:
                payload = await oauth.refresh_access_token(self.kind, self.refresh_token)
            except Exception as exc:
                raise NodeError(
                    f"{self.name or self.kind} sign-in expired and could not be refreshed: {exc}"
                ) from exc
            self.token = str(payload.get("access_token") or "").strip()
            if not self.token:
                raise NodeError(f"{self.name or self.kind} did not return a refreshed access token")
            self.token_expires_at = oauth.token_expiry(payload)
            return self.token
        if self.token:
            return self.token
        raise NodeError(f"{self.name or self.kind} needs a cloud-drive sign-in or access token")


# ---------------------------------------------------------------------------
# Local disk
# ---------------------------------------------------------------------------


class LocalConnector(Connector):
    kind = "local"
    capabilities = {"list", "read", "write"}

    def _root(self) -> Path:
        raw = str(self.config.get("root") or "").strip().strip("/")
        return safe_join(settings.files_dir, raw)

    def _ensure_root(self) -> Path:
        root = self._root()
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _resolve(self, path: str) -> Path:
        return safe_join(self._ensure_root(), path)

    async def ping(self) -> tuple[bool, str]:
        try:
            root = await asyncio.to_thread(self._ensure_root)
            probe = root / ".cascade-write-test"
            await asyncio.to_thread(probe.write_text, "ok", encoding="utf-8")
            await asyncio.to_thread(probe.unlink)
            return True, f"writable at {root}"
        except (OSError, ValueError) as exc:
            return False, str(exc)

    def _list_sync(self, path: str) -> list[FileItem]:
        target = self._resolve(path)
        if not target.is_dir():
            raise NodeError("not a directory")
        items: list[FileItem] = []
        with os.scandir(target) as entries:
            for entry in entries:
                if entry.name.startswith(".cascade-tmp-"):
                    continue
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                    stat = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                items.append(
                    FileItem(
                        name=entry.name,
                        path=join_path(path, entry.name),
                        is_dir=is_dir,
                        size=0 if is_dir else stat.st_size,
                        modified=int(stat.st_mtime),
                        mime="inode/directory" if is_dir else guess_mime(entry.name),
                    )
                )
        items.sort(key=lambda item: (not item.is_dir, item.name.lower()))
        return items

    async def list(self, path: str) -> list[FileItem]:
        try:
            return await asyncio.to_thread(self._list_sync, path)
        except ValueError as exc:
            raise NodeError(str(exc)) from exc
        except OSError as exc:
            raise NodeError(f"could not list folder: {exc}") from exc

    async def fetch(self, path: str, range_header: str | None = None) -> Fetch:
        try:
            target = self._resolve(path)
        except ValueError as exc:
            raise NodeError(str(exc)) from exc
        if not target.exists():
            raise NodeError("file not found")
        if target.is_dir():
            raise NodeError("that path is a folder")
        try:
            size = target.stat().st_size
        except OSError as exc:
            raise NodeError(str(exc)) from exc
        filename = target.name
        mime = guess_mime(filename)
        try:
            byte_range = parse_range(range_header, size)
        except RangeNotSatisfiable:
            from .remote import empty_fetch

            return empty_fetch(416, size)

        if byte_range is None:
            start, end, status = 0, max(0, size - 1), 200
            length = size
        else:
            start, end = byte_range
            status = 206
            length = end - start + 1

        try:
            handle = await asyncio.to_thread(open, target, "rb")
            await asyncio.to_thread(handle.seek, start)
        except OSError as exc:
            raise NodeError(str(exc)) from exc

        async def body() -> AsyncIterator[bytes]:
            remaining = length
            try:
                while remaining > 0:
                    chunk = await asyncio.to_thread(handle.read, min(256 * 1024, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk
            finally:
                await asyncio.to_thread(handle.close)

        headers = {
            "content-type": mime,
            "content-length": str(length),
            "accept-ranges": "bytes",
        }
        if status == 206:
            headers["content-range"] = f"bytes {start}-{end}/{size}"
        return Fetch(
            status=status,
            headers=headers,
            body=body(),
            filename=filename,
            mime=mime,
            size=length,
            url=str(target),
        )

    async def upload(
        self,
        path: str,
        body: AsyncIterator[bytes],
        size: int | None = None,
        mime: str | None = None,
    ) -> int:
        try:
            target = self._resolve(path)
        except ValueError as exc:
            raise NodeError(str(exc)) from exc
        if target.exists() and target.is_dir():
            raise NodeError("a folder already exists at that path")
        try:
            await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
        except OSError as exc:
            raise NodeError(str(exc)) from exc
        temp = target.parent / f".cascade-tmp-{secrets.token_hex(8)}"
        written = 0
        handle = None
        try:
            handle = await asyncio.to_thread(open, temp, "wb")
            async for chunk in body:
                if not chunk:
                    continue
                await asyncio.to_thread(handle.write, chunk)
                written += len(chunk)
            await asyncio.to_thread(handle.close)
            handle = None
            await asyncio.to_thread(os.replace, temp, target)
        except OSError as exc:
            raise NodeError(f"write failed: {exc}") from exc
        finally:
            if handle is not None:
                try:
                    await asyncio.to_thread(handle.close)
                except OSError:
                    pass
            if temp.exists():
                try:
                    await asyncio.to_thread(temp.unlink)
                except OSError:
                    pass
        return written

    async def mkdir(self, path: str) -> None:
        try:
            target = self._resolve(path)
            if target.exists() and not target.is_dir():
                raise NodeError("a file already exists at that path")
            await asyncio.to_thread(target.mkdir, parents=True, exist_ok=True)
        except ValueError as exc:
            raise NodeError(str(exc)) from exc
        except OSError as exc:
            raise NodeError(str(exc)) from exc

    async def delete(self, path: str) -> None:
        try:
            root = self._ensure_root()
            target = safe_join(root, path)
            if target == root or path.strip("/") == "":
                raise NodeError("refusing to delete the node root")
            if target.is_dir() and not target.is_symlink():
                await asyncio.to_thread(shutil.rmtree, target)
            else:
                await asyncio.to_thread(target.unlink)
        except FileNotFoundError as exc:
            raise NodeError("file not found") from exc
        except ValueError as exc:
            raise NodeError(str(exc)) from exc
        except OSError as exc:
            raise NodeError(str(exc)) from exc

    async def rename(self, path: str, new_path: str) -> None:
        try:
            root = self._ensure_root()
            source = safe_join(root, path)
            target = safe_join(root, new_path)
            if source == root:
                raise NodeError("refusing to move the node root")
            if not source.exists():
                raise NodeError("file not found")
            if target.exists():
                raise NodeError("destination already exists")
            await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(shutil.move, str(source), str(target))
        except ValueError as exc:
            raise NodeError(str(exc)) from exc
        except OSError as exc:
            raise NodeError(str(exc)) from exc


# ---------------------------------------------------------------------------
# FileBrowser / FileBrowser Quantum
# ---------------------------------------------------------------------------

_FB_TOKENS: dict[str, tuple[str, float]] = {}
_FB_DOWNLOAD_STYLE: dict[str, str] = {}


def _http_error_message(response: httpx.Response, fallback: str) -> str:
    try:
        data = response.json()
        if isinstance(data, dict):
            for key in ("error", "message", "detail"):
                if data.get(key):
                    return str(data[key])[:300]
    except ValueError:
        pass
    text = (response.text or "").strip()
    return text[:200] if text else fallback


class FileBrowserConnector(Connector):
    kind = "filebrowser"
    capabilities = {"list", "read", "write"}

    def _base(self) -> str:
        base = str(self.config.get("base_url") or "").strip().rstrip("/")
        if not base.startswith(("http://", "https://")):
            raise NodeError("FileBrowser base URL must start with http:// or https://")
        return base

    def _client(self, timeout: float = 30.0) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base(),
            timeout=httpx.Timeout(timeout, connect=15.0, read=None),
            follow_redirects=True,
        )

    def _auth_headers(self, token: str) -> dict[str, str]:
        return {"X-Auth": token, "Authorization": f"Bearer {token}"}

    async def _login(self, client: httpx.AsyncClient, force: bool = False) -> str:
        base = self._base()
        cached = _FB_TOKENS.get(base)
        if cached and not force and cached[1] > time.time():
            return cached[0]
        username = str(self.config.get("username") or "")
        password = str(self.config.get("password") or "")
        if not username or not password:
            token = str(self.config.get("token") or "").strip()
            if token:
                _FB_TOKENS[base] = (token, time.time() + 3600)
                return token
            raise NodeError("FileBrowser username and password are required")
        try:
            response = await client.post(
                "/api/login", json={"username": username, "password": password}
            )
        except httpx.HTTPError as exc:
            raise NodeError(f"cannot reach FileBrowser: {exc}") from exc
        if response.status_code >= 400:
            raise NodeError(f"FileBrowser login failed: {_http_error_message(response, 'check credentials')}")
        text = (response.text or "").strip()
        token = ""
        try:
            data = response.json()
            if isinstance(data, str):
                token = data
            elif isinstance(data, dict):
                token = str(data.get("token") or data.get("jwt") or "")
        except ValueError:
            token = text.strip('"')
        if not token:
            raise NodeError("FileBrowser login returned no token")
        _FB_TOKENS[base] = (token, time.time() + 6 * 3600)
        return token

    async def ping(self) -> tuple[bool, str]:
        try:
            async with self._client(timeout=15) as client:
                try:
                    response = await client.get("/health")
                    if response.status_code < 400:
                        return True, "healthy"
                except httpx.HTTPError:
                    pass
                token = await self._login(client)
                response = await client.get(
                    "/api/resources/", headers=self._auth_headers(token)
                )
                if response.status_code < 400:
                    return True, "API reachable"
                return False, _http_error_message(response, f"HTTP {response.status_code}")
        except NodeError as exc:
            return False, str(exc)
        except httpx.HTTPError as exc:
            return False, str(exc)

    async def _api_get(self, client: httpx.AsyncClient, url: str, token: str) -> httpx.Response:
        response = await client.get(url, headers=self._auth_headers(token))
        if response.status_code == 401:
            token = await self._login(client, force=True)
            response = await client.get(url, headers=self._auth_headers(token))
        return response

    async def list(self, path: str) -> list[FileItem]:
        path = normalize_path(path)
        encoded = quote(path, safe="/")
        try:
            async with self._client() as client:
                token = await self._login(client)
                response = await self._api_get(client, f"/api/resources{encoded}", token)
        except httpx.HTTPError as exc:
            raise NodeError(f"FileBrowser request failed: {exc}") from exc
        if response.status_code >= 400:
            raise NodeError(_http_error_message(response, f"FileBrowser returned HTTP {response.status_code}"))
        try:
            data = response.json()
        except ValueError as exc:
            raise NodeError("FileBrowser returned invalid JSON") from exc
        raw_items = data.get("items", []) if isinstance(data, dict) else data
        if not isinstance(raw_items, list):
            raise NodeError("FileBrowser listing was not a list")
        items: list[FileItem] = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or base_name(str(item.get("path") or "")))
            if not name:
                continue
            item_path = str(item.get("path") or join_path(path, name))
            is_dir = bool(item.get("isDir")) or item.get("type") in ("directory", "dir")
            try:
                size = int(item.get("size") or 0)
            except (TypeError, ValueError):
                size = 0
            items.append(
                FileItem(
                    name=name,
                    path=normalize_path(item_path),
                    is_dir=is_dir,
                    size=0 if is_dir else size,
                    modified=_parse_time(item.get("modified")),
                    mime="inode/directory" if is_dir else (item.get("mime") or guess_mime(name)),
                )
            )
        items.sort(key=lambda item: (not item.is_dir, item.name.lower()))
        return items

    async def fetch(self, path: str, range_header: str | None = None) -> Fetch:
        path = normalize_path(path)
        encoded = quote(path, safe="/")
        base = self._base()
        style = _FB_DOWNLOAD_STYLE.get(base)
        candidates: list[tuple[str, str]] = []
        if style != "resources":
            candidates.append(("raw", f"/api/raw{encoded}"))
        candidates.append(
            ("resources", f"/api/resources/download?path={quote(path, safe='')}&inline=true")
        )
        candidates.append(("resources_path", f"/api/resources/download{encoded}"))
        if style == "resources":
            candidates.sort(key=lambda item: 0 if item[0].startswith("resources") else 1)

        client = self._client()
        token = ""
        last_error = "download failed"
        try:
            token = await self._login(client)
            for _attempt in range(2):
                for label, url in candidates:
                    headers = self._auth_headers(token)
                    if range_header:
                        headers["Range"] = range_header
                    request = client.build_request("GET", url, headers=headers)
                    try:
                        response = await client.send(request, stream=True)
                    except httpx.HTTPError as exc:
                        last_error = str(exc)
                        break
                    if response.status_code == 401:
                        await response.aclose()
                        token = await self._login(client, force=True)
                        break
                    if response.status_code in (400, 404, 405, 501):
                        last_error = f"HTTP {response.status_code}"
                        await response.aclose()
                        continue
                    if response.status_code >= 400:
                        body = await response.aread()
                        await response.aclose()
                        last_error = body.decode("utf-8", "replace")[:200] or f"HTTP {response.status_code}"
                        raise NodeError(last_error)
                    _FB_DOWNLOAD_STYLE[base] = "resources" if label.startswith("resources") else "raw"
                    out_headers: dict[str, str] = {}
                    for name in (
                        "content-type",
                        "content-length",
                        "content-range",
                        "accept-ranges",
                        "etag",
                        "last-modified",
                    ):
                        if response.headers.get(name):
                            out_headers[name] = response.headers[name]
                    out_headers.setdefault("accept-ranges", "bytes")
                    filename = base_name(path) or "download"

                    async def body(response=response, client=client) -> AsyncIterator[bytes]:
                        try:
                            async for chunk in response.aiter_raw(128 * 1024):
                                yield chunk
                        finally:
                            await response.aclose()
                            await client.aclose()

                    size: int | None = None
                    if out_headers.get("content-length"):
                        try:
                            size = int(out_headers["content-length"])
                        except ValueError:
                            size = None
                    return Fetch(
                        status=response.status_code,
                        headers=out_headers,
                        body=body(),
                        filename=filename,
                        mime=out_headers.get("content-type", "").split(";")[0]
                        or guess_mime(filename),
                        size=size,
                        url=url,
                    )
                else:
                    continue
                continue
            raise NodeError(f"FileBrowser download failed: {last_error}")
        except NodeError:
            await client.aclose()
            raise
        except (httpx.HTTPError, ValueError) as exc:
            await client.aclose()
            raise NodeError(f"FileBrowser download failed: {exc}") from exc

    async def upload(
        self,
        path: str,
        body: AsyncIterator[bytes],
        size: int | None = None,
        mime: str | None = None,
    ) -> int:
        path = normalize_path(path)
        directory, filename = split_name(path)
        if not filename:
            raise NodeError("upload needs a file name")
        boundary = "cascade" + secrets.token_hex(12)
        stream = _MultipartStream(boundary, "files", filename, body, size, mime)
        headers = {
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        }
        length = stream.length()
        if length is not None:
            headers["Content-Length"] = str(length)
        try:
            async with self._client(timeout=3600) as client:
                token = await self._login(client)
                url = f"/api/resources{quote(directory, safe='/')}?override=true"
                response = await client.post(url, headers={**self._auth_headers(token), **headers}, content=stream)
                if response.status_code == 401:
                    token = await self._login(client, force=True)
                    response = await client.post(
                        url, headers={**self._auth_headers(token), **headers}, content=stream
                    )
        except httpx.HTTPError as exc:
            raise NodeError(f"upload failed: {exc}") from exc
        if response.status_code >= 400:
            raise NodeError(f"FileBrowser upload failed: {_http_error_message(response, str(response.status_code))}")
        return size if size is not None else 0

    async def mkdir(self, path: str) -> None:
        path = normalize_path(path)
        try:
            async with self._client() as client:
                token = await self._login(client)
                url = f"/api/resources{quote(path, safe='/')}/"
                response = await client.post(url, headers=self._auth_headers(token), content=b"")
                if response.status_code in (404, 405):
                    response = await client.put(url, headers=self._auth_headers(token), content=b"")
        except httpx.HTTPError as exc:
            raise NodeError(f"mkdir failed: {exc}") from exc
        if response.status_code >= 400:
            raise NodeError(_http_error_message(response, f"FileBrowser returned HTTP {response.status_code}"))

    async def delete(self, path: str) -> None:
        path = normalize_path(path)
        try:
            async with self._client() as client:
                token = await self._login(client)
                response = await client.delete(
                    f"/api/resources{quote(path, safe='/')}", headers=self._auth_headers(token)
                )
        except httpx.HTTPError as exc:
            raise NodeError(f"delete failed: {exc}") from exc
        if response.status_code >= 400:
            raise NodeError(_http_error_message(response, f"FileBrowser returned HTTP {response.status_code}"))

    async def rename(self, path: str, new_path: str) -> None:
        path = normalize_path(path)
        new_path = normalize_path(new_path)
        payload = {"action": "rename", "destination": new_path, "override": False}
        try:
            async with self._client() as client:
                token = await self._login(client)
                response = await client.patch(
                    f"/api/resources{quote(path, safe='/')}",
                    headers={**self._auth_headers(token), "Content-Type": "application/json"},
                    json=payload,
                )
        except httpx.HTTPError as exc:
            raise NodeError(f"rename failed: {exc}") from exc
        if response.status_code >= 400:
            raise NodeError(_http_error_message(response, f"FileBrowser returned HTTP {response.status_code}"))


class _MultipartStream(httpx.AsyncByteStream):
    """Streams a multipart/form-data body without buffering the file."""

    def __init__(
        self,
        boundary: str,
        field_name: str,
        filename: str,
        body: AsyncIterator[bytes],
        size: int | None = None,
        mime: str | None = None,
    ) -> None:
        safe_name = filename.replace('"', "").replace("\r", "").replace("\n", "")
        self.preamble = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{field_name}"; filename="{safe_name}"\r\n'
            f"Content-Type: {mime or guess_mime(filename)}\r\n\r\n"
        ).encode("utf-8")
        self.epilogue = f"\r\n--{boundary}--\r\n".encode("utf-8")
        self.body = body
        self.size = size

    def length(self) -> int | None:
        if self.size is None:
            return None
        return len(self.preamble) + self.size + len(self.epilogue)

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self.preamble
        async for chunk in self.body:
            if chunk:
                yield chunk
        yield self.epilogue


# ---------------------------------------------------------------------------
# Dropbox
# ---------------------------------------------------------------------------


class DropboxConnector(OAuthMixin, Connector):
    kind = "dropbox"
    API = "https://api.dropboxapi.com/2"
    CONTENT = "https://content.dropboxapi.com/2"

    def __init__(self, node) -> None:
        super().__init__(node)
        self._init_oauth()
        self.capabilities = {"direct"} | (
            {"list", "read", "write"} if self.has_credentials else set()
        )

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.token}"}
        if extra:
            headers.update(extra)
        return headers

    def _api_path(self, path: str) -> str:
        path = normalize_path(path)
        return "" if path == "/" else path

    async def ping(self) -> tuple[bool, str]:
        if not self.has_credentials:
            return True, "link-only mode (add an access token to browse)"
        try:
            await self._ensure_token()
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.post(
                    f"{self.API}/users/get_current_account", headers=self._headers()
                )
        except httpx.HTTPError as exc:
            return False, str(exc)
        if response.status_code >= 400:
            return False, _http_error_message(response, f"HTTP {response.status_code}")
        try:
            name = response.json().get("name", {}).get("display_name", "account")
        except ValueError:
            name = "account"
        return True, f"connected to {name}"

    async def list(self, path: str) -> list[FileItem]:
        await self._ensure_token()
        if not self.token:
            raise Unsupported("Dropbox nodes need an access token to be browsed")
        api_path = self._api_path(path)
        entries: list[dict[str, Any]] = []
        cursor: str | None = None
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    f"{self.API}/files/list_folder",
                    headers=self._headers({"Content-Type": "application/json"}),
                    json={"path": api_path, "limit": 2000, "recursive": False},
                )
                if response.status_code >= 400:
                    raise NodeError(_http_error_message(response, f"Dropbox returned HTTP {response.status_code}"))
                data = response.json()
                entries.extend(data.get("entries", []))
                cursor = data.get("cursor")
                for _ in range(4):
                    if not data.get("has_more") or not cursor:
                        break
                    response = await client.post(
                        f"{self.API}/files/list_folder/continue",
                        headers=self._headers({"Content-Type": "application/json"}),
                        json={"cursor": cursor},
                    )
                    if response.status_code >= 400:
                        break
                    data = response.json()
                    entries.extend(data.get("entries", []))
                    cursor = data.get("cursor")
        except httpx.HTTPError as exc:
            raise NodeError(f"Dropbox request failed: {exc}") from exc
        items: list[FileItem] = []
        for entry in entries:
            tag = entry.get(".tag")
            name = entry.get("name") or base_name(entry.get("path_display") or "")
            if not name:
                continue
            is_dir = tag == "folder"
            items.append(
                FileItem(
                    name=name,
                    path=join_path(path, name),
                    is_dir=is_dir,
                    size=0 if is_dir else int(entry.get("size") or 0),
                    modified=_parse_time(entry.get("server_modified")),
                    mime="inode/directory" if is_dir else guess_mime(name),
                )
            )
        items.sort(key=lambda item: (not item.is_dir, item.name.lower()))
        return items

    async def fetch(self, path: str, range_header: str | None = None) -> Fetch:
        await self._ensure_token()
        if not self.token:
            raise Unsupported("Dropbox nodes need an access token to stream files")
        api_path = self._api_path(path)
        arg = json.dumps({"path": api_path})
        headers = self._headers({"Dropbox-API-Arg": arg})
        filename = base_name(path)
        try:
            return await open_remote(
                f"{self.CONTENT}/files/download",
                headers=headers,
                range_header=range_header,
                method="POST",
                filename=filename,
                mime=guess_mime(filename),
            )
        except Exception as exc:
            raise NodeError(f"Dropbox download failed: {exc}") from exc

    SIMPLE_UPLOAD_LIMIT = 150 * 1024 * 1024
    SESSION_CHUNK = 8 * 1024 * 1024

    def _require_token(self) -> None:
        if not self.token:
            raise NodeError("Dropbox access token is required")

    def _content_headers(self, arg: dict[str, Any]) -> dict[str, str]:
        return self._headers(
            {
                "Content-Type": "application/octet-stream",
                "Dropbox-API-Arg": json.dumps(arg),
            }
        )

    def _check(self, response: httpx.Response, action: str) -> None:
        if response.status_code >= 400:
            raise NodeError(
                f"Dropbox {action} failed: "
                f"{_http_error_message(response, f'HTTP {response.status_code}')}"
            )

    async def _append_session(
        self, client: httpx.AsyncClient, session_id: str, offset: int, chunk: bytes
    ) -> None:
        if not chunk:
            return
        response = await client.post(
            f"{self.CONTENT}/files/upload_session/append_v2",
            headers=self._content_headers(
                {"cursor": {"session_id": session_id, "offset": offset}}
            ),
            content=chunk,
        )
        self._check(response, "upload")

    async def _upload_session(
        self,
        client: httpx.AsyncClient,
        api_path: str,
        body: AsyncIterator[bytes],
    ) -> None:
        response = await client.post(
            f"{self.CONTENT}/files/upload_session/start",
            headers=self._content_headers({"close": False}),
            content=b"",
        )
        self._check(response, "upload session")
        try:
            session_id = str(response.json().get("session_id") or "")
        except ValueError as exc:
            raise NodeError("Dropbox returned an invalid upload session") from exc
        if not session_id:
            raise NodeError("Dropbox did not return an upload session")

        offset = 0
        pending = bytearray()
        async for part in body:
            pending.extend(part)
            while len(pending) > self.SESSION_CHUNK:
                chunk = bytes(pending[: self.SESSION_CHUNK])
                del pending[: self.SESSION_CHUNK]
                await self._append_session(client, session_id, offset, chunk)
                offset += len(chunk)
        response = await client.post(
            f"{self.CONTENT}/files/upload_session/finish",
            headers=self._content_headers(
                {
                    "cursor": {"session_id": session_id, "offset": offset},
                    "commit": {
                        "path": api_path,
                        "mode": "add",
                        "autorename": False,
                        "mute": True,
                    },
                }
            ),
            content=bytes(pending),
        )
        self._check(response, "upload")

    async def upload(
        self,
        path: str,
        body: AsyncIterator[bytes],
        size: int | None = None,
        mime: str | None = None,
    ) -> int:
        await self._ensure_token()
        api_path = self._api_path(path)
        if not api_path:
            raise NodeError("choose a file path")
        total = 0

        async def counted() -> AsyncIterator[bytes]:
            nonlocal total
            async for chunk in body:
                if chunk:
                    total += len(chunk)
                    yield chunk

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(3600, connect=30)) as client:
                if size is not None and size <= self.SIMPLE_UPLOAD_LIMIT:
                    response = await client.post(
                        f"{self.CONTENT}/files/upload",
                        headers=self._content_headers(
                            {
                                "path": api_path,
                                "mode": "add",
                                "autorename": False,
                                "mute": True,
                            }
                        ),
                        content=_BodyStream(counted(), size),
                    )
                    self._check(response, "upload")
                else:
                    await self._upload_session(client, api_path, counted())
        except httpx.HTTPError as exc:
            raise NodeError(f"Dropbox upload failed: {exc}") from exc
        return total

    async def mkdir(self, path: str) -> None:
        await self._ensure_token()
        api_path = self._api_path(path)
        if not api_path:
            raise NodeError("choose a folder path")
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    f"{self.API}/files/create_folder_v2",
                    headers=self._headers({"Content-Type": "application/json"}),
                    json={"path": api_path, "autorename": False},
                )
        except httpx.HTTPError as exc:
            raise NodeError(f"Dropbox mkdir failed: {exc}") from exc
        self._check(response, "mkdir")

    async def delete(self, path: str) -> None:
        await self._ensure_token()
        api_path = self._api_path(path)
        if not api_path:
            raise NodeError("refusing to delete the node root")
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    f"{self.API}/files/delete_v2",
                    headers=self._headers({"Content-Type": "application/json"}),
                    json={"path": api_path},
                )
        except httpx.HTTPError as exc:
            raise NodeError(f"Dropbox delete failed: {exc}") from exc
        self._check(response, "delete")

    async def rename(self, path: str, new_path: str) -> None:
        await self._ensure_token()
        api_path = self._api_path(path)
        new_api_path = self._api_path(new_path)
        if not api_path or not new_api_path:
            raise NodeError("choose a file or folder to rename")
        if api_path == new_api_path:
            return
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    f"{self.API}/files/move_v2",
                    headers=self._headers({"Content-Type": "application/json"}),
                    json={
                        "from_path": api_path,
                        "to_path": new_api_path,
                        "autorename": False,
                    },
                )
        except httpx.HTTPError as exc:
            raise NodeError(f"Dropbox rename failed: {exc}") from exc
        self._check(response, "rename")

    async def direct(self, path: str) -> Resolved | None:
        if not self.has_credentials:
            return None
        try:
            await self._ensure_token()
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.post(
                    f"{self.API}/files/get_temporary_link",
                    headers=self._headers({"Content-Type": "application/json"}),
                    json={"path": self._api_path(path)},
                )
                if response.status_code >= 400:
                    return None
                link = response.json().get("link")
        except (httpx.HTTPError, ValueError):
            return None
        if not link:
            return None
        return Resolved(url=link, provider="dropbox", public=True)


# ---------------------------------------------------------------------------
# Google Drive
# ---------------------------------------------------------------------------


class GDriveConnector(OAuthMixin, Connector):
    kind = "gdrive"
    API = "https://www.googleapis.com/drive/v3/files"

    def __init__(self, node) -> None:
        super().__init__(node)
        self._init_oauth()
        self.capabilities = {"direct"} | (
            {"list", "read", "write"} if self.has_credentials else set()
        )

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    async def ping(self) -> tuple[bool, str]:
        if not self.has_credentials:
            return True, "link-only mode (add an access token to browse)"
        try:
            await self._ensure_token()
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.get(
                    "https://www.googleapis.com/drive/v3/about?fields=user",
                    headers=self._headers(),
                )
        except httpx.HTTPError as exc:
            return False, str(exc)
        if response.status_code >= 400:
            return False, _http_error_message(response, f"HTTP {response.status_code}")
        try:
            email = response.json().get("user", {}).get("emailAddress", "account")
        except ValueError:
            email = "account"
        return True, f"connected as {email}"

    async def list(self, path: str) -> list[FileItem]:
        await self._ensure_token()
        if not self.token:
            raise Unsupported("Google Drive nodes need an access token to be browsed")
        path = normalize_path(path)
        folder_id = path.strip("/").rsplit("/", 1)[-1] if path != "/" else str(
            self.config.get("folder_id") or "root"
        )
        files: list[dict[str, Any]] = []
        page_token: str | None = None
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                for _ in range(5):
                    params = {
                        "q": f"'{folder_id}' in parents and trashed = false",
                        "fields": "nextPageToken,files(id,name,mimeType,size,modifiedTime)",
                        "pageSize": "200",
                        "supportsAllDrives": "true",
                        "includeItemsFromAllDrives": "true",
                        "orderBy": "folder,name",
                    }
                    if page_token:
                        params["pageToken"] = page_token
                    response = await client.get(
                        self.API, params=params, headers=self._headers()
                    )
                    if response.status_code >= 400:
                        raise NodeError(
                            _http_error_message(response, f"Google Drive returned HTTP {response.status_code}")
                        )
                    data = response.json()
                    files.extend(data.get("files", []))
                    page_token = data.get("nextPageToken")
                    if not page_token:
                        break
        except httpx.HTTPError as exc:
            raise NodeError(f"Google Drive request failed: {exc}") from exc
        items: list[FileItem] = []
        for entry in files:
            is_dir = entry.get("mimeType") == "application/vnd.google-apps.folder"
            file_id = str(entry.get("id") or "")
            name = str(entry.get("name") or file_id)
            if not file_id:
                continue
            try:
                size = int(entry.get("size") or 0)
            except (TypeError, ValueError):
                size = 0
            items.append(
                FileItem(
                    name=name,
                    path=join_path(path, file_id),
                    is_dir=is_dir,
                    size=0 if is_dir else size,
                    modified=_parse_time(entry.get("modifiedTime")),
                    mime="inode/directory" if is_dir else str(entry.get("mimeType") or guess_mime(name)),
                )
            )
        items.sort(key=lambda item: (not item.is_dir, item.name.lower()))
        return items

    async def fetch(self, path: str, range_header: str | None = None) -> Fetch:
        await self._ensure_token()
        if not self.token:
            raise Unsupported("Google Drive nodes need an access token to stream files")
        file_id = normalize_path(path).strip("/").rsplit("/", 1)[-1]
        if not file_id:
            raise NodeError("no file id in that path")
        url = f"{self.API}/{quote(file_id)}?alt=media&supportsAllDrives=true"
        try:
            return await open_remote(
                url,
                headers=self._headers(),
                range_header=range_header,
                filename=base_name(path) or "drive-file",
                mime="application/octet-stream",
            )
        except Exception as exc:
            raise NodeError(f"Google Drive download failed: {exc}") from exc

    UPLOAD_API = "https://www.googleapis.com/upload/drive/v3/files"

    def _require_token(self) -> None:
        if not self.token:
            raise NodeError("Google Drive access token is required")

    def _folder_id(self, path: str) -> str:
        normalized = normalize_path(path)
        if normalized == "/":
            return str(self.config.get("folder_id") or "root")
        return normalized.strip("/").rsplit("/", 1)[-1]

    def _file_id(self, path: str) -> str:
        normalized = normalize_path(path)
        if normalized == "/":
            raise NodeError("choose a file or folder")
        return normalized.strip("/").rsplit("/", 1)[-1]

    def _check(self, response: httpx.Response, action: str) -> None:
        if response.status_code >= 300:
            raise NodeError(
                f"Google Drive {action} failed: "
                f"{_http_error_message(response, f'HTTP {response.status_code}')}"
            )

    async def upload(
        self,
        path: str,
        body: AsyncIterator[bytes],
        size: int | None = None,
        mime: str | None = None,
    ) -> int:
        await self._ensure_token()
        name = base_name(path)
        if not name:
            raise NodeError("choose a file path")
        parent = self._folder_id(parent_path(path))
        total = 0

        async def counted() -> AsyncIterator[bytes]:
            nonlocal total
            async for chunk in body:
                if chunk:
                    total += len(chunk)
                    yield chunk

        content_type = mime or guess_mime(name)
        metadata = {"name": name, "parents": [parent], "mimeType": content_type}
        headers = {
            **self._headers(),
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Type": content_type,
        }
        if size is not None:
            headers["X-Upload-Content-Length"] = str(size)
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(3600, connect=30)) as client:
                response = await client.post(
                    self.UPLOAD_API,
                    params={
                        "uploadType": "resumable",
                        "supportsAllDrives": "true",
                        "fields": "id,name,size",
                    },
                    headers=headers,
                    json=metadata,
                )
                self._check(response, "upload")
                upload_url = response.headers.get("location")
                if not upload_url:
                    raise NodeError("Google Drive did not return an upload URL")
                put_headers = {"Content-Type": content_type}
                if size is not None:
                    put_headers["Content-Length"] = str(size)
                response = await client.put(
                    upload_url,
                    headers=put_headers,
                    content=_BodyStream(counted(), size),
                )
                self._check(response, "upload")
        except httpx.HTTPError as exc:
            raise NodeError(f"Google Drive upload failed: {exc}") from exc
        return total

    async def mkdir(self, path: str) -> None:
        await self._ensure_token()
        name = base_name(path)
        if not name:
            raise NodeError("choose a folder path")
        parent = self._folder_id(parent_path(path))
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    self.API,
                    params={"supportsAllDrives": "true", "fields": "id,name"},
                    headers={**self._headers(), "Content-Type": "application/json"},
                    json={
                        "name": name,
                        "mimeType": "application/vnd.google-apps.folder",
                        "parents": [parent],
                    },
                )
        except httpx.HTTPError as exc:
            raise NodeError(f"Google Drive mkdir failed: {exc}") from exc
        self._check(response, "mkdir")

    async def delete(self, path: str) -> None:
        await self._ensure_token()
        file_id = self._file_id(path)
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.delete(
                    f"{self.API}/{quote(file_id, safe='')}",
                    params={"supportsAllDrives": "true"},
                    headers=self._headers(),
                )
        except httpx.HTTPError as exc:
            raise NodeError(f"Google Drive delete failed: {exc}") from exc
        self._check(response, "delete")

    async def rename(self, path: str, new_path: str) -> None:
        await self._ensure_token()
        file_id = self._file_id(path)
        new_name = base_name(new_path)
        if not new_name:
            raise NodeError("choose a new name")
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.patch(
                    f"{self.API}/{quote(file_id, safe='')}",
                    params={"supportsAllDrives": "true", "fields": "id,name"},
                    headers={**self._headers(), "Content-Type": "application/json"},
                    json={"name": new_name},
                )
        except httpx.HTTPError as exc:
            raise NodeError(f"Google Drive rename failed: {exc}") from exc
        self._check(response, "rename")

    async def direct(self, path: str) -> Resolved | None:
        if not self.has_credentials:
            return None
        try:
            await self._ensure_token()
        except NodeError:
            return None
        file_id = normalize_path(path).strip("/").rsplit("/", 1)[-1]
        if not file_id:
            return None
        return Resolved(
            url=f"https://drive.usercontent.google.com/download?id={quote(file_id)}&export=download&confirm=t",
            provider="gdrive",
            public=True,
        )


class _BodyStream(httpx.AsyncByteStream):
    """An async request body with a known length, used for uploads."""

    def __init__(self, body: AsyncIterator[bytes], length: int | None = None) -> None:
        self.body = body
        self.length = length

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self.body:
            if chunk:
                yield chunk


def _local_tag(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _xml_error(content: bytes) -> str:
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return content.decode("utf-8", "replace")[:300]
    for element in root.iter():
        if _local_tag(element.tag) == "Message" and element.text:
            return element.text[:300]
        if _local_tag(element.tag) == "Code" and element.text:
            return element.text[:300]
    return "upstream storage request failed"


# ---------------------------------------------------------------------------
# S3-compatible object storage
# ---------------------------------------------------------------------------


class S3Connector(Connector):
    """AWS S3, Cloudflare R2, Backblaze B2, MinIO and other S3-compatible APIs."""

    kind = "s3"
    capabilities = {"list", "read", "write", "direct"}

    def __init__(self, node) -> None:
        super().__init__(node)
        self.endpoint = str(self.config.get("endpoint") or "").strip().rstrip("/")
        self.region = str(self.config.get("region") or "us-east-1").strip() or "us-east-1"
        self.bucket = str(self.config.get("bucket") or "").strip()
        self.access_key = str(self.config.get("access_key") or "").strip()
        self.secret_key = str(self.config.get("secret_key") or "").strip()
        self.session_token = str(self.config.get("session_token") or "").strip()
        self.prefix = normalize_path(str(self.config.get("prefix") or "")).strip("/")
        self.path_style = str(self.config.get("path_style") or "path").strip().lower() != "virtual"

    def _endpoint(self) -> str:
        if not self.endpoint:
            return f"https://s3.{self.region}.amazonaws.com"
        parts = urlsplit(self.endpoint)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise NodeError("S3 endpoint must start with http:// or https://")
        return self.endpoint

    def _host(self) -> str:
        return urlsplit(self._endpoint()).netloc

    def _bucket_url(self) -> str:
        endpoint = self._endpoint()
        if self.path_style:
            return f"{endpoint}/{quote(self.bucket)}"
        parts = urlsplit(endpoint)
        return f"{parts.scheme}://{self.bucket}.{parts.netloc}"

    def _ensure_credentials(self) -> None:
        if not self.bucket or not self.access_key or not self.secret_key:
            raise NodeError("S3 bucket, access key and secret key are required")

    def _object_key(self, path: str) -> str:
        path = normalize_path(path)
        relative = path.strip("/")
        if self.prefix and relative:
            return f"{self.prefix}/{relative}"
        return self.prefix or relative

    def _list_prefix(self, path: str) -> str:
        base = self._object_key(path)
        return f"{base}/" if base else ""

    def _url_for_key(self, key: str | None) -> str:
        base = self._bucket_url()
        if key is None:
            return base
        return f"{base}/{quote(key, safe='/-_.~')}"

    def _canonical_uri(self, key: str | None) -> str:
        if key is None:
            return f"/{self.bucket}" if self.path_style else "/"
        encoded = quote(key, safe="/-_.~")
        return f"/{self.bucket}/{encoded}" if self.path_style else f"/{encoded}"

    @staticmethod
    def _canonical_query(params: dict[str, Any]) -> str:
        return "&".join(
            f"{quote(str(key), safe='-_.~')}={quote(str(value), safe='-_.~')}"
            for key, value in sorted(params.items())
        )

    @staticmethod
    def _amz_dates() -> tuple[str, str]:
        current = datetime.now(timezone.utc)
        return current.strftime("%Y%m%dT%H%M%SZ"), current.strftime("%Y%m%d")

    def _signing_key(self, date_stamp: str) -> bytes:
        key = ("AWS4" + self.secret_key).encode("utf-8")
        for part in (date_stamp, self.region, "s3", "aws4_request"):
            key = hmac.new(key, part.encode("utf-8"), hashlib.sha256).digest()
        return key

    def _signature(
        self,
        method: str,
        canonical_uri: str,
        canonical_query: str,
        canonical_headers: str,
        signed_headers: str,
        payload_hash: str,
        amz_date: str,
        date_stamp: str,
    ) -> str:
        scope = f"{date_stamp}/{self.region}/s3/aws4_request"
        canonical_request = "\n".join(
            (method, canonical_uri, canonical_query, canonical_headers, signed_headers, payload_hash)
        )
        string_to_sign = "\n".join(
            (
                "AWS4-HMAC-SHA256",
                amz_date,
                scope,
                hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
            )
        )
        return hmac.new(
            self._signing_key(date_stamp), string_to_sign.encode("utf-8"), hashlib.sha256
        ).hexdigest()

    def _signed_headers(
        self, method: str, key: str, payload_hash: str, extra: dict[str, str] | None = None
    ) -> dict[str, str]:
        self._ensure_credentials()
        amz_date, date_stamp = self._amz_dates()
        headers = {
            "host": self._host(),
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
        }
        if self.session_token:
            headers["x-amz-security-token"] = self.session_token
        if extra:
            headers.update({name.lower(): str(value) for name, value in extra.items()})
        canonical_headers = "".join(
            f"{name}:{value.strip()}\n" for name, value in sorted(headers.items())
        )
        signed_headers = ";".join(sorted(headers))
        signature = self._signature(
            method,
            self._canonical_uri(key),
            "",
            canonical_headers,
            signed_headers,
            payload_hash,
            amz_date,
            date_stamp,
        )
        scope = f"{date_stamp}/{self.region}/s3/aws4_request"
        output = {name: value for name, value in headers.items() if name != "host"}
        output["authorization"] = (
            f"AWS4-HMAC-SHA256 Credential={self.access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )
        return output

    def _presign(self, method: str, key: str | None, query: dict[str, Any] | None = None, expires: int = 3600) -> str:
        self._ensure_credentials()
        amz_date, date_stamp = self._amz_dates()
        scope = f"{date_stamp}/{self.region}/s3/aws4_request"
        params: dict[str, Any] = {
            "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
            "X-Amz-Credential": f"{self.access_key}/{scope}",
            "X-Amz-Date": amz_date,
            "X-Amz-Expires": str(expires),
            "X-Amz-SignedHeaders": "host",
        }
        if self.session_token:
            params["X-Amz-Security-Token"] = self.session_token
        if query:
            params.update(query)
        canonical_query = self._canonical_query(params)
        canonical_headers = f"host:{self._host()}\n"
        signature = self._signature(
            method,
            self._canonical_uri(key),
            canonical_query,
            canonical_headers,
            "host",
            "UNSIGNED-PAYLOAD",
            amz_date,
            date_stamp,
        )
        params["X-Amz-Signature"] = signature
        return f"{self._url_for_key(key)}?{self._canonical_query(params)}"

    def _error(self, response: httpx.Response) -> str:
        return _xml_error(response.content) if response.content else f"HTTP {response.status_code}"

    async def _list_page(
        self, prefix: str, delimiter: str | None, token: str | None = None
    ) -> tuple[list[dict[str, str]], list[str], str | None]:
        params: dict[str, Any] = {"list-type": "2", "prefix": prefix, "max-keys": "1000"}
        if delimiter:
            params["delimiter"] = delimiter
        if token:
            params["continuation-token"] = token
        url = self._presign("GET", None, params)
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                response = await client.get(url)
        except httpx.HTTPError as exc:
            raise NodeError(f"S3 request failed: {exc}") from exc
        if response.status_code >= 400:
            raise NodeError(f"S3 returned HTTP {response.status_code}: {self._error(response)}")
        try:
            root = ET.fromstring(response.content)
        except ET.ParseError as exc:
            raise NodeError("S3 returned invalid XML") from exc
        objects: list[dict[str, str]] = []
        folders: list[str] = []
        next_token: str | None = None
        for element in root:
            tag = _local_tag(element.tag)
            if tag == "Contents":
                values = {
                    _local_tag(child.tag): child.text or "" for child in element
                }
                objects.append(values)
            elif tag == "CommonPrefixes":
                child = next(
                    (item for item in element if _local_tag(item.tag) == "Prefix"), None
                )
                if child is not None and child.text:
                    folders.append(child.text)
            elif tag == "NextContinuationToken":
                next_token = element.text or None
        truncated = any(
            _local_tag(element.tag) == "IsTruncated"
            and (element.text or "").lower() == "true"
            for element in root
        )
        return objects, folders, next_token if truncated else None

    async def _all_keys(self, prefix: str) -> list[str]:
        keys: list[str] = []
        token: str | None = None
        for _ in range(50):
            objects, _folders, token = await self._list_page(prefix, None, token)
            keys.extend(item.get("Key", "") for item in objects if item.get("Key"))
            if not token:
                break
        return keys

    async def _head_exists(self, key: str) -> bool:
        url = self._presign("HEAD", key)
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
                response = await client.head(url)
        except httpx.HTTPError as exc:
            raise NodeError(f"S3 request failed: {exc}") from exc
        if response.status_code in (200, 204):
            return True
        if response.status_code == 404:
            return False
        raise NodeError(f"S3 returned HTTP {response.status_code}: {self._error(response)}")

    async def _delete_key(self, key: str) -> None:
        url = self._presign("DELETE", key)
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                response = await client.delete(url)
        except httpx.HTTPError as exc:
            raise NodeError(f"S3 delete failed: {exc}") from exc
        if response.status_code >= 400 and response.status_code != 404:
            raise NodeError(f"S3 delete failed: {self._error(response)}")

    async def _copy_key(self, source: str, destination: str) -> None:
        source_header = f"/{self.bucket}/{quote(source, safe='/-_.~')}"
        payload_hash = hashlib.sha256(b"").hexdigest()
        headers = self._signed_headers(
            "PUT",
            destination,
            payload_hash,
            {
                "content-length": "0",
                "x-amz-copy-source": source_header,
                "x-amz-metadata-directive": "COPY",
            },
        )
        try:
            async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
                response = await client.put(
                    self._url_for_key(destination), headers=headers, content=b""
                )
        except httpx.HTTPError as exc:
            raise NodeError(f"S3 copy failed: {exc}") from exc
        if response.status_code >= 300:
            raise NodeError(f"S3 copy failed: {self._error(response)}")
        if b"<Error>" in response.content:
            raise NodeError(f"S3 copy failed: {_xml_error(response.content)}")

    async def ping(self) -> tuple[bool, str]:
        try:
            self._ensure_credentials()
            url = self._presign("HEAD", None)
            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
                response = await client.head(url)
            if response.status_code in (200, 204):
                return True, f"bucket {self.bucket} reachable"
            if response.status_code == 403:
                return False, "credentials do not allow bucket access"
            return False, f"HTTP {response.status_code}"
        except (NodeError, httpx.HTTPError) as exc:
            return False, str(exc)

    async def list(self, path: str) -> list[FileItem]:
        path = normalize_path(path)
        list_prefix = self._list_prefix(path)
        items: list[FileItem] = []
        seen: set[str] = set()
        token: str | None = None
        for _ in range(20):
            objects, folders, token = await self._list_page(list_prefix, "/", token)
            for entry in objects:
                key = entry.get("Key", "")
                relative = key[len(list_prefix):] if key.startswith(list_prefix) else ""
                if not relative or relative.endswith("/") or "/" in relative:
                    continue
                virtual_path = join_path(path, relative)
                if virtual_path in seen:
                    continue
                seen.add(virtual_path)
                try:
                    size = int(entry.get("Size") or 0)
                except (TypeError, ValueError):
                    size = 0
                items.append(
                    FileItem(
                        name=relative,
                        path=virtual_path,
                        size=size,
                        modified=_parse_time(entry.get("LastModified")),
                        mime=guess_mime(relative),
                    )
                )
            for folder in folders:
                relative = folder[len(list_prefix):].rstrip("/") if folder.startswith(list_prefix) else ""
                if not relative or "/" in relative:
                    continue
                virtual_path = join_path(path, relative)
                if virtual_path in seen:
                    continue
                seen.add(virtual_path)
                items.append(
                    FileItem(
                        name=relative,
                        path=virtual_path,
                        is_dir=True,
                        mime="inode/directory",
                    )
                )
            if not token:
                break
        items.sort(key=lambda item: (not item.is_dir, item.name.lower()))
        return items

    async def fetch(self, path: str, range_header: str | None = None) -> Fetch:
        key = self._object_key(path)
        if not key:
            raise NodeError("choose a file, not the bucket root")
        try:
            return await open_remote(
                self._presign("GET", key),
                range_header=range_header,
                filename=base_name(path) or "download",
                mime=guess_mime(base_name(path)),
            )
        except Exception as exc:
            raise NodeError(f"S3 download failed: {exc}") from exc

    async def direct(self, path: str) -> Resolved | None:
        key = self._object_key(path)
        if not key:
            return None
        return Resolved(url=self._presign("GET", key), provider="s3", public=True)

    async def upload(
        self,
        path: str,
        body: AsyncIterator[bytes],
        size: int | None = None,
        mime: str | None = None,
    ) -> int:
        key = self._object_key(path)
        if not key or normalize_path(path) == "/":
            raise NodeError("choose a file path")
        if size is None:
            raise NodeError("S3 uploads need a known file size")
        headers = self._signed_headers(
            "PUT", key, "UNSIGNED-PAYLOAD", {"content-length": str(size)}
        )
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(3600, connect=30)) as client:
                response = await client.put(
                    self._url_for_key(key), headers=headers, content=_BodyStream(body, size)
                )
        except httpx.HTTPError as exc:
            raise NodeError(f"S3 upload failed: {exc}") from exc
        if response.status_code >= 300:
            raise NodeError(f"S3 upload failed: {self._error(response)}")
        return size

    async def _put_bytes(self, key: str, data: bytes) -> None:
        payload_hash = hashlib.sha256(data).hexdigest()
        headers = self._signed_headers(
            "PUT", key, payload_hash, {"content-length": str(len(data))}
        )
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                response = await client.put(self._url_for_key(key), headers=headers, content=data)
        except httpx.HTTPError as exc:
            raise NodeError(f"S3 request failed: {exc}") from exc
        if response.status_code >= 300:
            raise NodeError(f"S3 request failed: {self._error(response)}")

    async def mkdir(self, path: str) -> None:
        key = self._object_key(path)
        if not key or normalize_path(path) == "/":
            raise NodeError("choose a folder path")
        await self._put_bytes(key.rstrip("/") + "/", b"")

    async def delete(self, path: str) -> None:
        key = self._object_key(path)
        if not key or normalize_path(path) == "/":
            raise NodeError("refusing to delete the bucket root")
        keys = await self._all_keys(key.rstrip("/") + "/")
        if keys:
            for item in keys:
                await self._delete_key(item)
            return
        if not await self._head_exists(key):
            raise NodeError("file not found")
        await self._delete_key(key)

    async def rename(self, path: str, new_path: str) -> None:
        source = self._object_key(path)
        destination = self._object_key(new_path)
        if not source or not destination or source == destination:
            return
        prefix = source.rstrip("/") + "/"
        keys = await self._all_keys(prefix)
        if keys:
            for item in keys:
                suffix = item[len(prefix):]
                await self._copy_key(item, destination.rstrip("/") + "/" + suffix)
            for item in keys:
                await self._delete_key(item)
            return
        if not await self._head_exists(source):
            raise NodeError("file not found")
        await self._copy_key(source, destination)
        await self._delete_key(source)


# ---------------------------------------------------------------------------
# WebDAV (Nextcloud, ownCloud, generic)
# ---------------------------------------------------------------------------


class WebDAVConnector(Connector):
    kind = "webdav"
    capabilities = {"list", "read", "write"}
    PROPFIND_BODY = (
        b'<?xml version="1.0"?>'
        b'<d:propfind xmlns:d="DAV:"><d:prop>'
        b'<d:displayname/><d:resourcetype/><d:getcontentlength/>'
        b'<d:getlastmodified/><d:getcontenttype/>'
        b'</d:prop></d:propfind>'
    )

    def __init__(self, node) -> None:
        super().__init__(node)
        self.username = str(self.config.get("username") or "")
        self.password = str(self.config.get("password") or "")

    def _base(self) -> str:
        base = str(self.config.get("base_url") or "").strip().rstrip("/")
        if not base.startswith(("http://", "https://")):
            raise NodeError("WebDAV URL must start with http:// or https://")
        return base

    def _auth_headers(self) -> dict[str, str]:
        raw = f"{self.username}:{self.password}".encode("utf-8")
        return {"Authorization": "Basic " + base64.b64encode(raw).decode("ascii")}

    def _url(self, path: str) -> str:
        normalized = normalize_path(path)
        relative = quote(normalized.lstrip("/"), safe="/-_.~")
        return f"{self._base()}/{relative}" if relative else f"{self._base()}/"

    def _client(self, timeout: float = 60) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=20, read=None),
            follow_redirects=True,
        )

    async def ping(self) -> tuple[bool, str]:
        try:
            async with self._client(20) as client:
                response = await client.request(
                    "PROPFIND",
                    self._url("/"),
                    headers={**self._auth_headers(), "Depth": "0", "Content-Type": "application/xml"},
                    content=self.PROPFIND_BODY,
                )
            if response.status_code in (200, 207):
                return True, "WebDAV reachable"
            return False, _http_error_message(response, f"HTTP {response.status_code}")
        except (NodeError, httpx.HTTPError) as exc:
            return False, str(exc)

    async def list(self, path: str) -> list[FileItem]:
        try:
            async with self._client(30) as client:
                response = await client.request(
                    "PROPFIND",
                    self._url(path),
                    headers={**self._auth_headers(), "Depth": "1", "Content-Type": "application/xml"},
                    content=self.PROPFIND_BODY,
                )
        except httpx.HTTPError as exc:
            raise NodeError(f"WebDAV request failed: {exc}") from exc
        if response.status_code not in (200, 207):
            raise NodeError(_http_error_message(response, f"WebDAV returned HTTP {response.status_code}"))
        try:
            root = ET.fromstring(response.content)
        except ET.ParseError as exc:
            raise NodeError("WebDAV returned invalid XML") from exc
        base_path = unquote(urlsplit(self._base()).path).rstrip("/")
        items: list[FileItem] = []
        for node in root:
            if _local_tag(node.tag) != "response":
                continue
            href_element = next(
                (child for child in node if _local_tag(child.tag) == "href"), None
            )
            if href_element is None or not href_element.text:
                continue
            href_path = unquote(urlsplit(href_element.text).path)
            if base_path and href_path.startswith(base_path):
                relative = href_path[len(base_path):].strip("/")
            else:
                relative = href_path.strip("/")
            if not relative:
                continue
            name = relative.rsplit("/", 1)[-1]
            if not name:
                continue
            props: dict[str, str] = {}
            for element in node.iter():
                tag = _local_tag(element.tag)
                if tag in ("getcontentlength", "getlastmodified", "getcontenttype"):
                    props[tag] = element.text or ""
            resource_type = next(
                (element for element in node.iter() if _local_tag(element.tag) == "resourcetype"),
                None,
            )
            is_dir = resource_type is not None and any(
                _local_tag(child.tag) == "collection" for child in resource_type
            )
            try:
                size = int(props.get("getcontentlength") or 0)
            except ValueError:
                size = 0
            items.append(
                FileItem(
                    name=name,
                    path=join_path(path, name),
                    is_dir=is_dir,
                    size=0 if is_dir else size,
                    modified=_parse_time(props.get("getlastmodified")),
                    mime="inode/directory"
                    if is_dir
                    else (props.get("getcontenttype") or guess_mime(name)),
                )
            )
        items.sort(key=lambda item: (not item.is_dir, item.name.lower()))
        return items

    async def fetch(self, path: str, range_header: str | None = None) -> Fetch:
        try:
            return await open_remote(
                self._url(path),
                headers=self._auth_headers(),
                range_header=range_header,
                filename=base_name(path) or "download",
                mime=guess_mime(base_name(path)),
            )
        except Exception as exc:
            raise NodeError(f"WebDAV download failed: {exc}") from exc

    async def upload(
        self,
        path: str,
        body: AsyncIterator[bytes],
        size: int | None = None,
        mime: str | None = None,
    ) -> int:
        headers = self._auth_headers()
        if mime:
            headers["Content-Type"] = mime
        if size is not None:
            headers["Content-Length"] = str(size)
        try:
            async with self._client(3600) as client:
                response = await client.put(
                    self._url(path), headers=headers, content=_BodyStream(body, size)
                )
        except httpx.HTTPError as exc:
            raise NodeError(f"WebDAV upload failed: {exc}") from exc
        if response.status_code >= 300:
            raise NodeError(_http_error_message(response, f"WebDAV upload failed: HTTP {response.status_code}"))
        return size if size is not None else 0

    async def mkdir(self, path: str) -> None:
        try:
            async with self._client(30) as client:
                response = await client.request(
                    "MKCOL",
                    self._url(path) + "/",
                    headers=self._auth_headers(),
                )
        except httpx.HTTPError as exc:
            raise NodeError(f"WebDAV mkdir failed: {exc}") from exc
        if response.status_code == 405:
            return
        if response.status_code >= 300:
            raise NodeError(_http_error_message(response, f"WebDAV mkdir failed: HTTP {response.status_code}"))

    async def delete(self, path: str) -> None:
        try:
            async with self._client(30) as client:
                response = await client.delete(self._url(path), headers=self._auth_headers())
        except httpx.HTTPError as exc:
            raise NodeError(f"WebDAV delete failed: {exc}") from exc
        if response.status_code >= 300:
            raise NodeError(_http_error_message(response, f"WebDAV delete failed: HTTP {response.status_code}"))

    async def rename(self, path: str, new_path: str) -> None:
        headers = {
            **self._auth_headers(),
            "Destination": self._url(new_path),
            "Overwrite": "F",
        }
        try:
            async with self._client(30) as client:
                response = await client.request("MOVE", self._url(path), headers=headers)
        except httpx.HTTPError as exc:
            raise NodeError(f"WebDAV rename failed: {exc}") from exc
        if response.status_code >= 300:
            raise NodeError(_http_error_message(response, f"WebDAV rename failed: HTTP {response.status_code}"))


# ---------------------------------------------------------------------------
# Microsoft OneDrive
# ---------------------------------------------------------------------------


class OneDriveConnector(OAuthMixin, Connector):
    kind = "onedrive"
    capabilities = {"direct"}
    API = "https://graph.microsoft.com/v1.0"
    SIMPLE_UPLOAD_LIMIT = 250 * 1024 * 1024
    UPLOAD_CHUNK = 10 * 1024 * 1024

    def __init__(self, node) -> None:
        super().__init__(node)
        self._init_oauth()
        self.capabilities = {"direct"} | (
            {"list", "read", "write"} if self.has_credentials else set()
        )

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def _id(self, path: str) -> str:
        normalized = normalize_path(path)
        if normalized == "/":
            return str(self.config.get("folder_id") or "root")
        return normalized.strip("/").rsplit("/", 1)[-1]

    def _file_id(self, path: str) -> str:
        normalized = normalize_path(path)
        if normalized == "/":
            raise NodeError("choose a file or folder")
        return normalized.strip("/").rsplit("/", 1)[-1]

    def _children_url(self, folder_id: str) -> str:
        if folder_id == "root":
            return f"{self.API}/me/drive/root/children"
        return f"{self.API}/me/drive/items/{quote(folder_id, safe='')}/children"

    def _item_url(self, item_id: str) -> str:
        return f"{self.API}/me/drive/items/{quote(item_id, safe='')}"

    def _path_url(self, path: str) -> str:
        parent_id = self._id(parent_path(path))
        name = quote(base_name(path), safe="")
        if parent_id == "root":
            return f"{self.API}/me/drive/root:/{name}:/"
        return f"{self.API}/me/drive/items/{quote(parent_id, safe='')}:/{name}:/"

    def _content_url(self, path: str) -> str:
        return self._path_url(path) + "content"

    def _upload_session_url(self, path: str) -> str:
        return self._path_url(path) + "createUploadSession"

    def _require_token(self) -> None:
        if not self.token:
            raise NodeError("OneDrive access token is required")

    def _check(self, response: httpx.Response, action: str) -> None:
        if response.status_code >= 300:
            raise NodeError(
                f"OneDrive {action} failed: "
                f"{_http_error_message(response, f'HTTP {response.status_code}')}"
            )

    async def _put_chunk(
        self,
        client: httpx.AsyncClient,
        upload_url: str,
        offset: int,
        chunk: bytes,
        total: int,
    ) -> None:
        response = await client.put(
            upload_url,
            headers={
                "Content-Length": str(len(chunk)),
                "Content-Range": f"bytes {offset}-{offset + len(chunk) - 1}/{total}",
            },
            content=chunk,
        )
        self._check(response, "upload")

    async def _upload_chunks(
        self,
        client: httpx.AsyncClient,
        upload_url: str,
        body: AsyncIterator[bytes],
        total: int,
    ) -> None:
        offset = 0
        pending = bytearray()
        async for part in body:
            pending.extend(part)
            while (
                len(pending) >= self.UPLOAD_CHUNK
                and offset + self.UPLOAD_CHUNK <= total
            ):
                chunk = bytes(pending[: self.UPLOAD_CHUNK])
                del pending[: self.UPLOAD_CHUNK]
                await self._put_chunk(client, upload_url, offset, chunk, total)
                offset += len(chunk)
        if offset < total:
            chunk = bytes(pending)
            if len(chunk) != total - offset:
                raise NodeError("OneDrive upload body did not match the expected size")
            await self._put_chunk(client, upload_url, offset, chunk, total)
            offset += len(chunk)
        elif pending:
            raise NodeError("OneDrive upload body exceeded the expected size")
        if offset != total:
            raise NodeError("OneDrive upload did not send the expected number of bytes")

    async def ping(self) -> tuple[bool, str]:
        if not self.has_credentials:
            return False, "OneDrive access token is required"
        try:
            await self._ensure_token()
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.get(
                    f"{self.API}/me/drive", params={"$select": "id,driveType,owner"}, headers=self._headers()
                )
        except httpx.HTTPError as exc:
            return False, str(exc)
        if response.status_code >= 400:
            return False, _http_error_message(response, f"HTTP {response.status_code}")
        return True, "connected to OneDrive"

    async def list(self, path: str) -> list[FileItem]:
        await self._ensure_token()
        if not self.token:
            raise NodeError("OneDrive access token is required")
        folder_id = self._id(path)
        url = self._children_url(folder_id)
        params = {
            "$top": "200",
            "$select": "id,name,size,folder,file,lastModifiedDateTime",
        }
        entries: list[dict[str, Any]] = []
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.get(url, params=params, headers=self._headers())
                for _ in range(5):
                    if response.status_code >= 400:
                        raise NodeError(
                            _http_error_message(response, f"OneDrive returned HTTP {response.status_code}")
                        )
                    data = response.json()
                    entries.extend(data.get("value", []))
                    next_link = data.get("@odata.nextLink")
                    if not next_link:
                        break
                    response = await client.get(next_link, headers=self._headers())
        except httpx.HTTPError as exc:
            raise NodeError(f"OneDrive request failed: {exc}") from exc
        items: list[FileItem] = []
        for entry in entries:
            item_id = str(entry.get("id") or "")
            name = str(entry.get("name") or item_id)
            if not item_id:
                continue
            is_dir = "folder" in entry
            file_info = entry.get("file") if isinstance(entry.get("file"), dict) else {}
            try:
                size = int(entry.get("size") or 0)
            except (TypeError, ValueError):
                size = 0
            items.append(
                FileItem(
                    name=name,
                    path=join_path(path, item_id),
                    is_dir=is_dir,
                    size=0 if is_dir else size,
                    modified=_parse_time(entry.get("lastModifiedDateTime")),
                    mime="inode/directory" if is_dir else str(file_info.get("mimeType") or guess_mime(name)),
                )
            )
        items.sort(key=lambda item: (not item.is_dir, item.name.lower()))
        return items

    async def fetch(self, path: str, range_header: str | None = None) -> Fetch:
        await self._ensure_token()
        if not self.token:
            raise NodeError("OneDrive access token is required")
        item_id = self._id(path)
        if item_id == "root":
            raise NodeError("choose a file, not the drive root")
        url = f"{self.API}/me/drive/items/{quote(item_id, safe='')}/content"
        try:
            return await open_remote(
                url,
                headers=self._headers(),
                range_header=range_header,
                filename=base_name(path) or "download",
                mime=guess_mime(base_name(path)),
            )
        except Exception as exc:
            raise NodeError(f"OneDrive download failed: {exc}") from exc

    async def upload(
        self,
        path: str,
        body: AsyncIterator[bytes],
        size: int | None = None,
        mime: str | None = None,
    ) -> int:
        await self._ensure_token()
        name = base_name(path)
        if not name:
            raise NodeError("choose a file path")
        if size is None:
            raise NodeError("OneDrive uploads need a known file size")
        total = 0

        async def counted() -> AsyncIterator[bytes]:
            nonlocal total
            async for chunk in body:
                if chunk:
                    total += len(chunk)
                    yield chunk

        content_type = mime or guess_mime(name)
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(3600, connect=30)) as client:
                if size <= self.SIMPLE_UPLOAD_LIMIT:
                    response = await client.put(
                        self._content_url(path),
                        headers={
                            **self._headers(),
                            "Content-Type": content_type,
                            "Content-Length": str(size),
                        },
                        content=_BodyStream(counted(), size),
                    )
                    self._check(response, "upload")
                else:
                    response = await client.post(
                        self._upload_session_url(path),
                        headers={**self._headers(), "Content-Type": "application/json"},
                        json={
                            "item": {
                                "@microsoft.graph.conflictBehavior": "replace",
                                "name": name,
                            }
                        },
                    )
                    self._check(response, "upload session")
                    try:
                        upload_url = str(response.json().get("uploadUrl") or "")
                    except ValueError as exc:
                        raise NodeError("OneDrive returned an invalid upload session") from exc
                    if not upload_url:
                        raise NodeError("OneDrive did not return an upload URL")
                    await self._upload_chunks(client, upload_url, counted(), size)
        except httpx.HTTPError as exc:
            raise NodeError(f"OneDrive upload failed: {exc}") from exc
        return total

    async def mkdir(self, path: str) -> None:
        await self._ensure_token()
        name = base_name(path)
        if not name:
            raise NodeError("choose a folder path")
        parent_id = self._id(parent_path(path))
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    self._children_url(parent_id),
                    headers={**self._headers(), "Content-Type": "application/json"},
                    json={
                        "name": name,
                        "folder": {},
                        "@microsoft.graph.conflictBehavior": "fail",
                    },
                )
        except httpx.HTTPError as exc:
            raise NodeError(f"OneDrive mkdir failed: {exc}") from exc
        self._check(response, "mkdir")

    async def delete(self, path: str) -> None:
        await self._ensure_token()
        item_id = self._file_id(path)
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.delete(
                    self._item_url(item_id), headers=self._headers()
                )
        except httpx.HTTPError as exc:
            raise NodeError(f"OneDrive delete failed: {exc}") from exc
        self._check(response, "delete")

    async def rename(self, path: str, new_path: str) -> None:
        await self._ensure_token()
        item_id = self._file_id(path)
        new_name = base_name(new_path)
        if not new_name:
            raise NodeError("choose a new name")
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.patch(
                    self._item_url(item_id),
                    headers={**self._headers(), "Content-Type": "application/json"},
                    json={"name": new_name},
                )
        except httpx.HTTPError as exc:
            raise NodeError(f"OneDrive rename failed: {exc}") from exc
        self._check(response, "rename")

    async def direct(self, path: str) -> Resolved | None:
        if not self.has_credentials:
            return None
        item_id = self._id(path)
        if item_id == "root":
            return None
        try:
            await self._ensure_token()
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.get(
                    f"{self.API}/me/drive/items/{quote(item_id, safe='')}",
                    params={"$select": "@microsoft.graph.downloadUrl"},
                    headers=self._headers(),
                )
                if response.status_code >= 400:
                    return None
                url = response.json().get("@microsoft.graph.downloadUrl")
        except (httpx.HTTPError, ValueError):
            return None
        if not url:
            return None
        return Resolved(url=url, provider="onedrive", public=True)


# ---------------------------------------------------------------------------
# Link-only providers
# ---------------------------------------------------------------------------


class TeraBoxConnector(Connector):
    kind = "terabox"
    capabilities = {"direct"}

    async def ping(self) -> tuple[bool, str]:
        if self.config.get("direct_url_template"):
            return True, "direct URL template configured"
        return True, "link-only mode (URL resolution happens when a link is opened)"

    async def direct(self, path: str) -> Resolved | None:
        return None


class HttpConnector(Connector):
    kind = "http"
    capabilities = {"read", "direct"}

    def _base(self) -> str:
        base = str(self.config.get("base_url") or "").strip()
        if not base.startswith(("http://", "https://")):
            raise NodeError("HTTP node needs a base_url starting with http:// or https://")
        return base if base.endswith("/") else base + "/"

    def direct_url(self, path: str) -> str:
        raw = (path or "").strip()
        if raw.startswith(("http://", "https://")):
            return raw
        return urljoin(self._base(), quote(raw.lstrip("/"), safe="/"))

    def _extra_headers(self) -> dict[str, str]:
        raw = self.config.get("headers") or {}
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                raw = {}
        if isinstance(raw, dict):
            return {str(k): str(v) for k, v in raw.items()}
        return {}

    async def ping(self) -> tuple[bool, str]:
        try:
            url = self._base()
        except NodeError as exc:
            return False, str(exc)
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                response = await client.head(url, headers=self._extra_headers())
            if response.status_code >= 400:
                return False, f"HTTP {response.status_code}"
            return True, "reachable"
        except httpx.HTTPError as exc:
            return False, str(exc)

    async def fetch(self, path: str, range_header: str | None = None) -> Fetch:
        url = self.direct_url(path)
        return await open_remote(
            url,
            headers=self._extra_headers(),
            range_header=range_header,
            filename=base_name(urlsplit(url).path),
        )

    async def direct(self, path: str) -> Resolved | None:
        return Resolved(url=self.direct_url(path), provider="http", public=True)


# ---------------------------------------------------------------------------
# Node kinds & factory
# ---------------------------------------------------------------------------

NODE_KINDS: dict[str, dict[str, Any]] = {
    "local": {
        "label": "Local disk",
        "group": "Local",
        "blurb": "Files stored on this server. Fast, seekable, writable.",
        "fields": [
            {
                "key": "root",
                "label": "Subfolder",
                "placeholder": "media",
                "help": "Optional folder inside the Cascade data directory.",
            }
        ],
    },
    "filebrowser": {
        "label": "FileBrowser / Quantum",
        "group": "Server & storage",
        "blurb": "Bridge a FileBrowser or FileBrowser Quantum instance over its REST API.",
        "fields": [
            {"key": "base_url", "label": "Base URL", "placeholder": "https://files.example.com", "required": True},
            {"key": "username", "label": "Username", "placeholder": "admin", "required": True},
            {"key": "password", "label": "Password", "type": "password", "required": True},
        ],
    },
    "webdav": {
        "label": "WebDAV",
        "group": "Server & storage",
        "blurb": "Nextcloud, ownCloud or any WebDAV folder. Browse, stream, upload and organize files.",
        "fields": [
            {
                "key": "base_url",
                "label": "WebDAV URL",
                "placeholder": "https://cloud.example.com/remote.php/dav/files/me",
                "required": True,
                "help": "The folder URL your WebDAV client uses. Nextcloud usually ends in /remote.php/dav/files/<user>.",
            },
            {"key": "username", "label": "Username", "required": True},
            {"key": "password", "label": "Password", "type": "password", "required": True},
        ],
    },
    "s3": {
        "label": "S3-compatible",
        "group": "Server & storage",
        "blurb": "AWS S3, Cloudflare R2, Backblaze B2, MinIO or any S3 API. Browse, stream, upload and forward through signed URLs.",
        "fields": [
            {
                "key": "endpoint",
                "label": "Endpoint",
                "placeholder": "https://<account>.r2.cloudflarestorage.com",
                "help": "Optional. Leave blank for AWS. R2, B2 and MinIO need their own endpoint.",
            },
            {
                "key": "region",
                "label": "Region",
                "placeholder": "us-east-1",
                "help": "Use auto for R2 if your provider accepts it; otherwise match the bucket region.",
            },
            {"key": "bucket", "label": "Bucket", "required": True},
            {
                "key": "prefix",
                "label": "Prefix",
                "placeholder": "media",
                "help": "Optional folder inside the bucket. Leave blank to use the bucket root.",
            },
            {"key": "access_key", "label": "Access key ID", "required": True},
            {
                "key": "secret_key",
                "label": "Secret access key",
                "type": "password",
                "required": True,
            },
            {
                "key": "session_token",
                "label": "Session token",
                "type": "password",
                "help": "Optional. Only needed for temporary AWS credentials.",
            },
            {
                "key": "path_style",
                "label": "Addressing style",
                "type": "select",
                "default": "path",
                "options": [
                    {"value": "path", "label": "Path-style (R2, MinIO, B2)"},
                    {"value": "virtual", "label": "Virtual-hosted (AWS S3)"},
                ],
                "help": "Path-style puts the bucket in the URL path. Most non-AWS providers use it.",
            },
        ],
    },
    "dropbox": {
        "label": "Dropbox",
        "group": "Cloud drives",
        "blurb": "Sign in to browse, stream and upload, or paste an access token as a manual fallback.",
        "oauth": True,
        "fields": [
            {
                "key": "access_token",
                "label": "Access token",
                "type": "password",
                "help": "Optional fallback. Use Connect Dropbox on the Nodes page for OAuth sign-in and automatic token refresh.",
            }
        ],
    },
    "gdrive": {
        "label": "Google Drive",
        "group": "Cloud drives",
        "blurb": "Sign in to browse, stream and upload Drive files, or resolve public share links.",
        "oauth": True,
        "fields": [
            {
                "key": "access_token",
                "label": "OAuth access token",
                "type": "password",
                "help": "Optional fallback. Use Connect Google Drive on the Nodes page for OAuth sign-in and automatic token refresh.",
            },
            {
                "key": "folder_id",
                "label": "Root folder id",
                "placeholder": "root",
                "help": "Folder shown as / on this node.",
            },
        ],
    },
    "onedrive": {
        "label": "Microsoft OneDrive",
        "group": "Cloud drives",
        "blurb": "Sign in with Microsoft to browse, stream and upload OneDrive files.",
        "oauth": True,
        "fields": [
            {
                "key": "access_token",
                "label": "Graph access token",
                "type": "password",
                "required": True,
                "help": "Optional fallback. Use Connect Microsoft OneDrive on the Nodes page for OAuth sign-in and automatic token refresh.",
            },
            {
                "key": "folder_id",
                "label": "Root folder ID",
                "placeholder": "root",
                "help": "Optional. The folder shown as / on this node.",
            },
        ],
    },
    "terabox": {
        "label": "TeraBox",
        "group": "Cloud drives",
        "blurb": "Resolve TeraBox share links via page API, yt-dlp, or a URL template.",
        "browser_login": {
            "label": "Sign in with browser",
            "hint": (
                "Opens a Chromium window on the machine running Cascade. Sign in normally; "
                "Cascade saves the session cookie and closes the window when it expires."
            ),
        },
        "fields": [
            {
                "key": "cookie",
                "label": "Browser cookie",
                "type": "password",
                "help": (
                    "Optional. Use Sign in with browser in Add node; paste ndus=... only as a fallback."
                ),
            },
            {
                "key": "direct_url_template",
                "label": "Direct URL template",
                "placeholder": "https://d.terabox.com/file/{id}",
                "help": "Optional escape hatch. Supports {url} and {id}.",
            },
        ],
    },
    "http": {
        "label": "HTTP / direct",
        "group": "Other",
        "blurb": "Any server that exposes plain file URLs. Link-only or browsable by path.",
        "fields": [
            {
                "key": "base_url",
                "label": "Base URL",
                "placeholder": "https://cdn.example.com/files/",
                "required": True,
            },
            {
                "key": "headers",
                "label": "Extra headers (JSON)",
                "type": "textarea",
                "placeholder": '{"Referer": "https://example.com"}',
            },
        ],
    },
}

CONNECTOR_MAP = {
    "local": LocalConnector,
    "filebrowser": FileBrowserConnector,
    "webdav": WebDAVConnector,
    "s3": S3Connector,
    "dropbox": DropboxConnector,
    "gdrive": GDriveConnector,
    "onedrive": OneDriveConnector,
    "terabox": TeraBoxConnector,
    "http": HttpConnector,
}


def build_connector(node) -> Connector:
    if node is None:
        raise NodeError("node not found")
    kind = str(node["kind"])
    cls = CONNECTOR_MAP.get(kind)
    if cls is None:
        raise NodeError(f"unknown node kind '{kind}'")
    return cls(node)


async def test_node(node) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        connector = build_connector(node)
        ok, message = await connector.ping()
        capabilities = connector.capabilities_list()
    except Exception as exc:  # pragma: no cover - defensive
        ok, message, capabilities = False, str(exc)[:300], []
    latency = int((time.perf_counter() - started) * 1000)
    return {"ok": ok, "message": message[:300], "latency_ms": latency, "capabilities": capabilities}


def node_kinds_for_ui() -> list[dict[str, Any]]:
    out = []
    for kind, meta in NODE_KINDS.items():
        out.append(
            {
                "kind": kind,
                "label": meta["label"],
                "group": meta.get("group", "Other"),
                "blurb": meta["blurb"],
                "fields": meta["fields"],
                "oauth": bool(meta.get("oauth")),
                "browser_login": meta.get("browser_login"),
            }
        )
    return out
