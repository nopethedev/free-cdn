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
import json
import os
import secrets
import shutil
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import quote, urljoin, urlsplit

import httpx

from .config import settings
from .remote import Fetch, open_remote
from .resolvers import Resolved
from .utils import (
    RangeNotSatisfiable,
    base_name,
    guess_mime,
    join_path,
    normalize_path,
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


class DropboxConnector(Connector):
    kind = "dropbox"
    API = "https://api.dropboxapi.com/2"
    CONTENT = "https://content.dropboxapi.com/2"

    def __init__(self, node) -> None:
        super().__init__(node)
        self.token = str(self.config.get("access_token") or "").strip()
        self.capabilities = {"direct"} | ({"list", "read"} if self.token else set())

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.token}"}
        if extra:
            headers.update(extra)
        return headers

    def _api_path(self, path: str) -> str:
        path = normalize_path(path)
        return "" if path == "/" else path

    async def ping(self) -> tuple[bool, str]:
        if not self.token:
            return True, "link-only mode (add an access token to browse)"
        try:
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

    async def direct(self, path: str) -> Resolved | None:
        if not self.token:
            return None
        try:
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


class GDriveConnector(Connector):
    kind = "gdrive"
    API = "https://www.googleapis.com/drive/v3/files"

    def __init__(self, node) -> None:
        super().__init__(node)
        self.token = str(self.config.get("access_token") or "").strip()
        self.capabilities = {"direct"} | ({"list", "read"} if self.token else set())

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    async def ping(self) -> tuple[bool, str]:
        if not self.token:
            return True, "link-only mode (add an access token to browse)"
        try:
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

    async def direct(self, path: str) -> Resolved | None:
        file_id = normalize_path(path).strip("/").rsplit("/", 1)[-1]
        if not file_id:
            return None
        return Resolved(
            url=f"https://drive.usercontent.google.com/download?id={quote(file_id)}&export=download&confirm=t",
            provider="gdrive",
            public=True,
        )


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
        "blurb": "Bridge a FileBrowser or FileBrowser Quantum instance over its REST API.",
        "fields": [
            {"key": "base_url", "label": "Base URL", "placeholder": "https://files.example.com", "required": True},
            {"key": "username", "label": "Username", "placeholder": "admin", "required": True},
            {"key": "password", "label": "Password", "type": "password", "required": True},
        ],
    },
    "dropbox": {
        "label": "Dropbox",
        "blurb": "Browse and stream with an access token, or resolve shared links.",
        "fields": [
            {
                "key": "access_token",
                "label": "Access token",
                "type": "password",
                "help": "Optional. A Dropbox API token unlocks browsing and temporary links.",
            }
        ],
    },
    "gdrive": {
        "label": "Google Drive",
        "blurb": "Browse folders and resolve public share links into direct downloads.",
        "fields": [
            {
                "key": "access_token",
                "label": "OAuth access token",
                "type": "password",
                "help": "Optional. Needed only to browse or stream private files.",
            },
            {
                "key": "folder_id",
                "label": "Root folder id",
                "placeholder": "root",
                "help": "Folder shown as / on this node.",
            },
        ],
    },
    "terabox": {
        "label": "TeraBox",
        "blurb": "Resolve TeraBox share links via page API, yt-dlp, or a URL template.",
        "fields": [
            {
                "key": "cookie",
                "label": "Browser cookie",
                "type": "password",
                "help": "Optional. Paste the ndus=... cookie for private shares.",
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
    "dropbox": DropboxConnector,
    "gdrive": GDriveConnector,
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
        out.append({"kind": kind, "label": meta["label"], "blurb": meta["blurb"], "fields": meta["fields"]})
    return out
