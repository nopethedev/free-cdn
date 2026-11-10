"""Resolve share pages into direct, CDN-style file URLs.

Order of attack for any URL:
  1. provider-specific rewrite (Dropbox, Google Drive, TeraBox)
  2. cached result from a previous resolution
  3. a ranged probe that follows redirects and rejects HTML pages
  4. yt-dlp, if installed, as the universal escape hatch

The result is either a URL that can be proxied by `/s/` or redirected to by
`/f/`. Results are cached in SQLite so repeated hits stay cheap.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlsplit, urlunsplit

import httpx

from . import db
from .config import settings
from .remote import DEFAULT_UA
from .utils import now

CACHE_TTL = 1200
USER_AGENT = DEFAULT_UA
GDRIVE_HOSTS = ("drive.google.com", "docs.google.com", "drive.usercontent.google.com")
DROPBOX_HOSTS = ("dropbox.com", "www.dropbox.com", "dl.dropboxusercontent.com")
TERABOX_HOSTS = (
    "terabox.com",
    "www.terabox.com",
    "1024tera.com",
    "www.1024tera.com",
    "teraboxapp.com",
    "www.teraboxapp.com",
    "4funbox.com",
    "www.4funbox.com",
    "mirrobox.com",
    "nephobox.com",
    "freeterabox.com",
    "www.terabox.app",
)


class ResolveError(Exception):
    """Raised when no direct URL can be found."""

    def __init__(self, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.detail = detail


@dataclass
class Resolved:
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    provider: str = "http"
    public: bool = True
    size: int | None = None
    content_type: str = ""
    expires_at: int | None = None


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _match_host(host: str, hosts: tuple[str, ...]) -> bool:
    return any(host == h or host.endswith("." + h) for h in hosts)


def is_gdrive(url: str) -> bool:
    return _match_host(_host(url), GDRIVE_HOSTS)


def is_dropbox(url: str) -> bool:
    return _match_host(_host(url), DROPBOX_HOSTS)


def is_terabox(url: str) -> bool:
    return _match_host(_host(url), TERABOX_HOSTS)


def extract_gdrive_id(url: str) -> str | None:
    if not is_gdrive(url):
        return None
    patterns = (
        r"/file/d/([A-Za-z0-9_-]{10,})",
        r"/d/([A-Za-z0-9_-]{10,})",
        r"[?&]id=([A-Za-z0-9_-]{10,})",
        r"/open/([A-Za-z0-9_-]{10,})",
    )
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


def dropbox_direct_url(url: str) -> str | None:
    """Turn a Dropbox share link into a dl.dropboxusercontent.com link."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if not _match_host(host, DROPBOX_HOSTS):
        return None
    path = parts.path
    if not (path.startswith("/s/") or path.startswith("/scl/")):
        return None
    query = parse_qs(parts.query, keep_blank_values=True)
    query["dl"] = ["1"]
    return urlunsplit(("https", "dl.dropboxusercontent.com", path, urlencode(query, doseq=True), ""))


def extract_terabox_id(url: str) -> str:
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    for key in ("surl", "shorturl", "shareid"):
        if query.get(key):
            return query[key][0]
    match = re.search(r"/s/([A-Za-z0-9_-]+)", parts.path)
    if match:
        return match.group(1)
    return parts.path.strip("/").split("/")[-1]


def _cache_key(url: str, config: dict[str, Any] | None) -> str:
    cfg = config or {}
    secret_bits = str(cfg.get("access_token") or "") + "|" + str(cfg.get("cookie") or "")
    digest = hashlib.sha256(secret_bits.encode("utf-8")).hexdigest()[:16]
    return f"{url}#{digest}"


def cache_get(url: str, config: dict[str, Any] | None) -> Resolved | None:
    try:
        row = db.fetchone("SELECT * FROM remote_cache WHERE key = ?", (_cache_key(url, config),))
    except Exception:
        return None
    if row is None:
        return None
    if row["expires_at"] and row["expires_at"] < now():
        return None
    try:
        headers = json.loads(row["headers"] or "{}")
    except json.JSONDecodeError:
        headers = {}
    return Resolved(
        url=row["url"],
        headers=headers,
        provider=row["provider"] or "http",
        public=bool(row["public"]),
        size=row["size"],
        content_type=row["content_type"] or "",
        expires_at=row["expires_at"],
    )


def cache_put(url: str, config: dict[str, Any] | None, resolved: Resolved, ttl: int = CACHE_TTL) -> None:
    try:
        db.execute(
            "INSERT INTO remote_cache (key, url, headers, provider, public, size, content_type,"
            " expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET url = excluded.url, headers = excluded.headers,"
            " provider = excluded.provider, public = excluded.public, size = excluded.size,"
            " content_type = excluded.content_type, expires_at = excluded.expires_at,"
            " created_at = excluded.created_at",
            (
                _cache_key(url, config),
                resolved.url,
                json.dumps(resolved.headers, separators=(",", ":")),
                resolved.provider,
                1 if resolved.public else 0,
                resolved.size,
                resolved.content_type,
                resolved.expires_at or (now() + ttl),
                now(),
            ),
        )
    except Exception:
        pass


def _looks_like_html(content_type: str, prefix: bytes) -> bool:
    if "text/html" in content_type or "application/xhtml" in content_type:
        return True
    sample = prefix[:512].lstrip().lower()
    return sample.startswith(b"<!doctype html") or sample.startswith(b"<html")


async def _probe(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str] | None = None,
    *,
    allow_html: bool = False,
) -> Resolved:
    """GET the first byte of a URL, following redirects, to prove it is a file."""
    request_headers = {"User-Agent": USER_AGENT, "Range": "bytes=0-0", "Accept": "*/*"}
    if headers:
        request_headers.update(headers)
    try:
        response = await client.get(url, headers=request_headers)
    except httpx.HTTPError as exc:
        raise ResolveError("upstream did not respond", str(exc)) from exc
    content_type = (response.headers.get("content-type") or "").lower()
    prefix = response.content[:1024] if response.content else b""
    await response.aclose()
    if response.status_code >= 400:
        raise ResolveError(f"upstream returned HTTP {response.status_code}")
    if not allow_html and _looks_like_html(content_type, prefix):
        raise ResolveError("upstream returned an HTML page, not a file")
    final_url = str(response.url)
    size: int | None = None
    content_range = response.headers.get("content-range", "")
    match = re.search(r"/(\d+)$", content_range)
    if match:
        size = int(match.group(1))
    elif response.headers.get("content-length") and "content-encoding" not in response.headers:
        try:
            size = int(response.headers["content-length"])
        except ValueError:
            size = None
    return Resolved(
        url=final_url,
        headers=headers or {},
        provider="http",
        public=True,
        size=size,
        content_type=(response.headers.get("content-type") or "").split(";")[0],
    )


async def _resolve_dropbox(url: str, config: dict[str, Any], client: httpx.AsyncClient) -> Resolved:
    direct = dropbox_direct_url(url)
    if direct is None:
        raise ResolveError(
            "this Dropbox link is not a file share link",
            "Use a dropbox.com/s/... or dropbox.com/scl/fi/... link, or browse the folder with an access token.",
        )
    resolved = Resolved(url=direct, provider="dropbox", public=True)
    try:
        probed = await _probe(client, direct, allow_html=True)
        resolved.size = probed.size
        resolved.content_type = probed.content_type or "application/octet-stream"
        resolved.url = probed.url
    except ResolveError:
        # The rewrite is well known; keep it even if the probe was blocked.
        resolved.content_type = "application/octet-stream"
    return resolved


def _hidden_inputs(html: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for tag in re.findall(r"<input[^>]*>", html, flags=re.IGNORECASE):
        name = re.search(r'name="([^"]+)"', tag)
        value = re.search(r'value="([^"]*)"', tag)
        if name:
            fields[name.group(1)] = value.group(1) if value else ""
    return fields


async def _resolve_gdrive(url: str, config: dict[str, Any], client: httpx.AsyncClient) -> Resolved:
    file_id = extract_gdrive_id(url)
    if not file_id:
        raise ResolveError("could not find a Google Drive file id in that URL")
    token = (config or {}).get("access_token")
    if token:
        return Resolved(
            url=f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media&supportsAllDrives=true",
            headers={"Authorization": f"Bearer {token}"},
            provider="gdrive",
            public=False,
        )

    errors: list[str] = []
    candidates = (
        f"https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm=t",
        f"https://drive.google.com/uc?export=download&id={file_id}&confirm=t",
    )
    for candidate in candidates:
        try:
            resolved = await _probe(client, candidate)
            resolved.provider = "gdrive"
            resolved.content_type = resolved.content_type or "application/octet-stream"
            return resolved
        except ResolveError as exc:
            errors.append(str(exc))

    # Large public files: Google shows a confirmation form with a uuid token.
    try:
        response = await client.get(
            f"https://drive.google.com/uc?export=download&id={file_id}",
            headers={"User-Agent": USER_AGENT},
        )
        html = response.text
        action_match = re.search(r'<form[^>]+action="([^"]+)"', html, flags=re.IGNORECASE)
        if action_match:
            action = action_match.group(1).replace("&amp;", "&")
            fields = _hidden_inputs(html)
            fields.setdefault("id", file_id)
            fields.setdefault("export", "download")
            fields["confirm"] = "t"
            target = action + ("&" if "?" in action else "?") + urlencode(fields)
            resolved = await _probe(client, target)
            resolved.provider = "gdrive"
            resolved.content_type = resolved.content_type or "application/octet-stream"
            return resolved
    except (httpx.HTTPError, ResolveError) as exc:
        errors.append(str(exc))

    raise ResolveError(
        "Google Drive did not expose a direct download",
        "; ".join(errors[-3:]) or "the file may be private or over its download quota",
    )


def _terabox_cookie(config: dict[str, Any], response: httpx.Response | None = None) -> str:
    cfg_cookie = str((config or {}).get("cookie") or "").strip()
    if cfg_cookie:
        return cfg_cookie
    if response is not None:
        cookie = response.cookies.get("ndus") or response.cookies.get("BDUSS") or ""
        return cookie
    return ""


async def _resolve_terabox(url: str, config: dict[str, Any], client: httpx.AsyncClient) -> Resolved:
    template = str((config or {}).get("direct_url_template") or "").strip()
    if template:
        try:
            direct = template.format(url=quote(url, safe=""), id=extract_terabox_id(url))
        except (KeyError, IndexError, ValueError) as exc:
            raise ResolveError("invalid direct URL template", str(exc)) from exc
        headers: dict[str, str] = {"User-Agent": USER_AGENT}
        cookie = _terabox_cookie(config)
        if cookie:
            headers["Cookie"] = cookie
        return Resolved(url=direct, headers=headers, provider="terabox", content_type="application/octet-stream")

    try:
        return await _terabox_scrape(url, config, client)
    except ResolveError as scrape_error:
        try:
            return await resolve_with_ytdlp(url, config)
        except ResolveError as ytdlp_error:
            raise ResolveError(
                "TeraBox needs a little help",
                f"{scrape_error.detail or scrape_error}; yt-dlp: {ytdlp_error.detail or ytdlp_error}. "
                "Set a browser cookie (ndus) on the node, or provide a direct URL template.",
            ) from ytdlp_error


async def _terabox_scrape(url: str, config: dict[str, Any], client: httpx.AsyncClient) -> Resolved:
    parts = urlsplit(url)
    base = f"{parts.scheme or 'https'}://{parts.netloc}"
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": "https://www.google.com/",
    }
    cookie = _terabox_cookie(config)
    if cookie:
        headers["Cookie"] = cookie
    try:
        page = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise ResolveError("could not open the TeraBox share page", str(exc)) from exc
    html = page.text
    if page.status_code >= 400:
        raise ResolveError(f"TeraBox returned HTTP {page.status_code}")

    def grab(*patterns: str) -> str:
        for pattern in patterns:
            match = re.search(pattern, html)
            if match:
                return match.group(1)
        return ""

    shareid = grab(r'"shareid"\s*:\s*"?(\d+)', r'shareid["\']?\s*[:=]\s*["\']?(\d+)')
    uk = grab(r'"uk"\s*:\s*"?(\d+)', r'"uk"\s*:\s*(\d+)')
    sign = grab(r'"sign"\s*:\s*"([^"]+)"')
    timestamp = grab(r'"timestamp"\s*:\s*(\d+)')
    js_token = grab(r'"jsToken"\s*:\s*"([^"]+)"') or page.cookies.get("jsToken") or ""
    if not shareid or not uk:
        raise ResolveError(
            "TeraBox page did not expose its share ids",
            "The layout may have changed, the link may be private, or a cookie is required.",
        )

    resolved_cookie = cookie or _terabox_cookie(config, page)
    api_headers = {
        "User-Agent": USER_AGENT,
        "Referer": url,
        "Accept": "application/json, text/plain, */*",
    }
    cookies: dict[str, str] = {}
    if js_token:
        cookies["jsToken"] = js_token
    if resolved_cookie:
        for chunk in resolved_cookie.split(";"):
            if "=" in chunk:
                key, value = chunk.split("=", 1)
                cookies[key.strip()] = value.strip()

    params = {
        "app_id": "250528",
        "web": "1",
        "channel": "dubox",
        "clienttype": "0",
        "jsToken": js_token,
        "shorturl": extract_terabox_id(url),
        "root": "1",
        "page": "1",
        "num": "100",
    }
    if sign:
        params["sign"] = sign
    if timestamp:
        params["timestamp"] = timestamp
    if shareid:
        params["shareid"] = shareid
    if uk:
        params["uk"] = uk

    try:
        response = await client.get(
            f"{base}/share/list", params=params, headers=api_headers, cookies=cookies
        )
        data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise ResolveError("TeraBox file listing failed", str(exc)) from exc
    if not isinstance(data, dict):
        raise ResolveError("TeraBox returned an unexpected response")
    errno = data.get("errno")
    if errno not in (0, None):
        raise ResolveError(f"TeraBox API error {errno}", str(data.get("errmsg") or "")[:300])

    items = data.get("list") or []
    dlink = ""
    for item in items:
        if isinstance(item, dict) and item.get("dlink"):
            dlink = str(item["dlink"])
            break
    if not dlink:
        raise ResolveError(
            "TeraBox did not return a download link",
            "Sign in to TeraBox and paste the ndus cookie on the node, then try again.",
        )
    if dlink.startswith("/"):
        dlink = base + dlink
    out_headers = {
        "User-Agent": USER_AGENT,
        "Referer": url,
    }
    if resolved_cookie:
        out_headers["Cookie"] = resolved_cookie
    return Resolved(
        url=dlink,
        headers=out_headers,
        provider="terabox",
        public=False,
        content_type="application/octet-stream",
        expires_at=now() + 900,
    )


def _ytdlp_binary(config: dict[str, Any] | None = None) -> str:
    configured = str((config or {}).get("ytdlp_path") or "").strip()
    if configured:
        return configured
    try:
        from_settings = db.get_setting("ytdlp_path", "").strip()
    except Exception:
        from_settings = ""
    return from_settings or settings.ytdlp_path


async def resolve_with_ytdlp(url: str, config: dict[str, Any] | None = None) -> Resolved:
    try:
        if db.get_setting("ytdlp_enabled", "1") != "1":
            raise ResolveError(
                "yt-dlp is disabled",
                "Enable it under Settings → yt-dlp, or install the provider's native resolver.",
            )
    except ResolveError:
        raise
    except Exception:
        pass
    binary = _ytdlp_binary(config)
    command = [
        binary,
        "--no-warnings",
        "--no-playlist",
        "--socket-timeout",
        "20",
        "-J",
        url,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise ResolveError("yt-dlp is not installed", f"'{binary}' was not found on PATH") from exc
    except OSError as exc:
        raise ResolveError("could not start yt-dlp", str(exc)) from exc
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=90)
    except asyncio.TimeoutError as exc:
        proc.kill()
        raise ResolveError("yt-dlp timed out") from exc
    if proc.returncode != 0:
        message = (stderr or b"").decode("utf-8", "replace").strip().splitlines()
        raise ResolveError("yt-dlp could not resolve this URL", (message[-1] if message else "")[:300])
    try:
        info = json.loads(stdout.decode("utf-8", "replace"))
    except json.JSONDecodeError as exc:
        raise ResolveError("yt-dlp returned unreadable output") from exc

    direct = ""
    if isinstance(info, dict):
        if isinstance(info.get("url"), str):
            direct = info["url"]
        formats = info.get("requested_downloads") or info.get("formats") or []
        if not direct and isinstance(formats, list):
            for fmt in reversed(formats):
                if isinstance(fmt, dict) and isinstance(fmt.get("url"), str):
                    direct = fmt["url"]
                    break
    if not direct:
        raise ResolveError("yt-dlp found no direct media URL")
    headers = {}
    if isinstance(info, dict) and isinstance(info.get("http_headers"), dict):
        headers = {str(k): str(v) for k, v in info["http_headers"].items() if v}
    # yt-dlp headers are usually ordinary browser headers (Referer/UA), so the
    # resolved URL can safely be used for a public forward redirect as well.
    sensitive = any(k.lower() == "authorization" for k in headers)
    return Resolved(
        url=direct,
        headers=headers,
        provider="ytdlp",
        public=not sensitive,
        content_type="application/octet-stream",
        expires_at=now() + 900,
    )


async def resolve_direct(
    source_url: str,
    config: dict[str, Any] | None = None,
    *,
    client: httpx.AsyncClient | None = None,
    use_cache: bool = True,
) -> Resolved:
    """Resolve any supported share URL to a directly fetchable URL."""
    if not source_url:
        raise ResolveError("no source URL supplied")
    config = config or {}
    cached = cache_get(source_url, config) if use_cache else None
    if cached:
        return cached

    own_client = client is None
    client = client or httpx.AsyncClient(
        follow_redirects=True, timeout=httpx.Timeout(25.0, connect=15.0, read=30.0)
    )
    host = _host(source_url)
    try:
        if is_gdrive(source_url):
            resolved = await _resolve_gdrive(source_url, config, client)
        elif is_dropbox(source_url):
            resolved = await _resolve_dropbox(source_url, config, client)
        elif is_terabox(source_url):
            resolved = await _resolve_terabox(source_url, config, client)
        else:
            try:
                resolved = await _probe(client, source_url)
                resolved.provider = host or "http"
            except ResolveError:
                resolved = await resolve_with_ytdlp(source_url, config)
                resolved.provider = resolved.provider or "ytdlp"
    finally:
        if own_client:
            await client.aclose()

    resolved.expires_at = resolved.expires_at or (now() + CACHE_TTL)
    if use_cache:
        ttl = max(60, resolved.expires_at - now())
        cache_put(source_url, config, resolved, ttl=ttl)
    return resolved
