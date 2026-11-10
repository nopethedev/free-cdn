"""Public delivery endpoints: /{id}, /s/{id}, /f/{id}."""

from __future__ import annotations

import re

from fastapi import APIRouter, Form, Request
from fastapi.responses import (
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)

from .. import __version__, db
from ..deps import client_ip, get_auth
from ..links import link_row, link_urls
from ..nodes import NodeError, Unsupported
from ..remote import RemoteError
from ..resolvers import ResolveError
from ..security import (
    RateLimiter,
    check_link_token,
    make_link_token,
    unlock_limiter,
    verify_password,
)
from ..serve import (
    consume_download,
    forward_target,
    link_block_message,
    link_state,
    open_link,
    record_delivery,
)
from ..utils import content_disposition, guess_mime
from ..web import render

router = APIRouter(tags=["public"])
LINK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{2,64}$")
SAFE_NEXT_PREFIXES = ("/s/", "/f/", "/u/")
_STREAM_LIMITERS: dict[int, RateLimiter] = {}


def _stream_limiter(limit: int) -> RateLimiter:
    limiter = _STREAM_LIMITERS.get(limit)
    if limiter is None:
        limiter = RateLimiter(limit, 60)
        _STREAM_LIMITERS[limit] = limiter
    return limiter


def _find(link_id: str):
    if not LINK_ID_RE.match(link_id or ""):
        return None
    return link_row(link_id)


def _authorized(link, request: Request) -> bool:
    if not link["password_hash"]:
        return True
    if check_link_token(request.query_params.get("t"), link["id"]):
        return True
    supplied = request.query_params.get("p")
    if supplied and verify_password(supplied, link["password_hash"]):
        return True
    return False


def _error(request: Request, title: str, message: str, status_code: int = 404):
    return render(
        request,
        "error.html",
        title=title,
        message=message,
        status_code=status_code,
        active="",
    )


def _landing(request: Request, link, *, locked: bool, status_code: int = 200, error: str = ""):
    token = request.query_params.get("t") or ""
    if not check_link_token(token, link["id"]):
        supplied = request.query_params.get("p")
        if supplied and link["password_hash"] and verify_password(supplied, link["password_hash"]):
            token = make_link_token(link["id"])
        else:
            token = ""
    return render(
        request,
        "link_landing.html",
        link=link,
        urls=link_urls(request, link),
        token=token,
        locked=locked,
        error=error,
        state=link_state(link),
        status_code=status_code,
        active="",
    )


def _check_link(request: Request, link):
    """Returns an error response when the link cannot be served."""
    if link is None:
        return _error(request, "Link not found", "This short link does not exist or was removed.", 404)
    message = link_block_message(link)
    if message:
        return _error(request, "Link unavailable", message, 410)
    return None


def _with_token(url: str, token: str) -> str:
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}t={token}"


@router.get("/healthz")
async def healthz():
    return {"ok": True, "name": "cascade", "version": __version__}


@router.get("/robots.txt")
async def robots():
    if db.get_setting("robots_noindex", "1") == "1":
        body = "User-agent: *\nDisallow: /\n"
    else:
        body = "User-agent: *\nAllow: /\n"
    return Response(body, media_type="text/plain")


@router.get("/favicon.ico")
async def favicon():
    return RedirectResponse("/static/favicon.svg", status_code=307)


@router.get("/")
async def home(request: Request):
    row = db.fetchone("SELECT COUNT(*) AS n FROM users")
    if not row or row["n"] == 0:
        return RedirectResponse("/setup", status_code=303)
    if get_auth(request):
        return RedirectResponse("/admin", status_code=303)
    return render(request, "home.html", active="")


@router.post("/api/public/links/{link_id}/unlock")
async def unlock_json(link_id: str, request: Request):
    link = _find(link_id)
    if link is None:
        return JSONResponse({"error": "link not found"}, status_code=404)
    blocked = link_block_message(link)
    if blocked:
        return JSONResponse({"error": blocked}, status_code=410)
    if not link["password_hash"]:
        return {"token": make_link_token(link_id), "urls": link_urls(request, link)}
    ip = client_ip(request)
    if not unlock_limiter.allow(f"unlock:{ip}:{link_id}"):
        return JSONResponse({"error": "too many attempts"}, status_code=429)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    password = str(payload.get("password") or request.query_params.get("password") or "")
    if not verify_password(password, link["password_hash"]):
        return JSONResponse({"error": "wrong password"}, status_code=401)
    token = make_link_token(link_id)
    urls = {key: _with_token(value, token) for key, value in link_urls(request, link).items()}
    db.audit(link["created_by"], "link.unlocked", {"id": link_id}, ip)
    return {"token": token, "urls": urls}


@router.post("/u/{link_id}")
async def unlock_form(
    link_id: str,
    request: Request,
    password: str = Form(""),
    next: str = Form(""),
):
    link = _find(link_id)
    blocked = _check_link(request, link)
    if blocked:
        return blocked
    ip = client_ip(request)
    if not unlock_limiter.allow(f"unlock:{ip}:{link_id}"):
        return _landing(request, link, locked=True, status_code=429, error="Too many attempts. Wait a minute.")
    if link["password_hash"] and not verify_password(password, link["password_hash"]):
        db.audit(link["created_by"], "link.unlock_failed", {"id": link_id}, ip)
        return _landing(request, link, locked=True, status_code=401, error="That password is not right.")
    token = make_link_token(link_id)
    target = next if next.startswith("/") and not next.startswith("//") else f"/s/{link_id}"
    if not target.startswith(SAFE_NEXT_PREFIXES):
        target = f"/s/{link_id}"
    db.audit(link["created_by"], "link.unlocked", {"id": link_id}, ip)
    return RedirectResponse(_with_token(target, token), status_code=303)


@router.get("/{link_id}")
async def short_link(link_id: str, request: Request):
    link = _find(link_id)
    blocked = _check_link(request, link)
    if blocked:
        return blocked
    authorized = _authorized(link, request)
    if not authorized:
        return _landing(request, link, locked=True)
    if link["landing"] or link["password_hash"]:
        return _landing(request, link, locked=False)
    if link["mode"] == "forward" and link["allow_forward"]:
        target = f"/f/{link_id}"
    elif link["allow_stream"]:
        target = f"/s/{link_id}"
    elif link["allow_forward"]:
        target = f"/f/{link_id}"
    else:
        return _error(request, "Link unavailable", "This link has no delivery mode enabled.", 410)
    if request.url.query:
        target = f"{target}?{request.url.query}"
    return RedirectResponse(target, status_code=302)


@router.head("/s/{link_id}")
async def stream_head(link_id: str, request: Request):
    link = _find(link_id)
    if link is None or link_block_message(link) or not link["allow_stream"]:
        return Response(status_code=404)
    if not _authorized(link, request):
        return Response(status_code=401)
    try:
        fetch = await open_link(link, "bytes=0-0")
        headers = dict(fetch.headers)
        await _close(fetch.body)
    except (NodeError, Unsupported, ResolveError, RemoteError):
        return Response(status_code=502)
    headers.pop("content-length", None)
    headers.pop("content-range", None)
    headers["accept-ranges"] = "bytes"
    headers["content-disposition"] = content_disposition(link["name"] or fetch.filename, inline=True)
    return Response(status_code=200, headers=headers)


async def _close(body) -> None:
    closer = getattr(body, "aclose", None)
    if closer:
        try:
            await closer()
        except Exception:
            pass


@router.get("/s/{link_id}")
async def stream_link(link_id: str, request: Request):
    link = _find(link_id)
    blocked = _check_link(request, link)
    if blocked:
        return blocked
    if not link["allow_stream"]:
        return _error(request, "Streaming disabled", "This link only allows forwarding.", 403)
    if not _authorized(link, request):
        return _landing(request, link, locked=True, status_code=401)
    ip = client_ip(request)
    limit = db.get_setting("link_rate_limit", "120")
    try:
        rate = int(limit or 0)
    except ValueError:
        rate = 0
    if rate > 0 and not _stream_limiter(rate).allow(f"stream:{ip}:{link_id}"):
        return _error(request, "Slow down", "Too many requests on this link. Try again shortly.", 429)

    range_header = request.headers.get("range")
    try:
        fetch = await open_link(link, range_header)
    except (NodeError, Unsupported, ResolveError, RemoteError) as exc:
        detail = getattr(exc, "detail", "") or str(exc)
        return _error(request, "Source unavailable", detail or "The file could not be opened.", 502)
    except Exception as exc:  # defensive: never leak a traceback to a visitor
        return _error(request, "Source unavailable", str(exc) or "The file could not be opened.", 502)

    if fetch.status == 416:
        await _close(fetch.body)
        headers = {
            "content-range": fetch.headers.get("content-range", "bytes */0"),
            "accept-ranges": "bytes",
            "cache-control": "no-store",
        }
        return Response(status_code=416, headers=headers)
    if fetch.status >= 400:
        await _close(fetch.body)
        return _error(
            request,
            "Source refused the request",
            f"The upstream host answered with HTTP {fetch.status}.",
            502,
        )

    counts = not range_header or range_header.strip().lower().startswith("bytes=0-")
    if counts and not consume_download(link_id):
        await _close(fetch.body)
        return _error(request, "Link unavailable", "This link reached its download limit.", 410)

    filename = link["name"] or fetch.filename or "download"
    download = request.query_params.get("download") == "1"
    headers = dict(fetch.headers)
    headers["content-disposition"] = content_disposition(filename, inline=not download)
    headers["cache-control"] = "no-store"
    headers["x-content-type-options"] = "nosniff"
    if "content-type" not in headers:
        headers["content-type"] = fetch.mime or guess_mime(filename)

    user_agent = request.headers.get("user-agent")

    async def body():
        sent = 0
        try:
            async for chunk in fetch.body:
                if chunk:
                    sent += len(chunk)
                    yield chunk
        finally:
            try:
                record_delivery(link_id, "stream", sent, ip, user_agent)
            except Exception:
                pass

    return StreamingResponse(body(), status_code=fetch.status, headers=headers)


@router.get("/f/{link_id}")
async def forward_link(link_id: str, request: Request):
    link = _find(link_id)
    blocked = _check_link(request, link)
    if blocked:
        return blocked
    if not link["allow_forward"]:
        return _error(request, "Forwarding disabled", "This link only allows streaming.", 403)
    if not _authorized(link, request):
        return _landing(request, link, locked=True, status_code=401)

    target, provider = await forward_target(link)
    if not target:
        # Local and FileBrowser sources have no public URL; streaming is the only
        # sensible hand-off, and it keeps both endpoints working for every link.
        query = request.url.query
        fallback = f"/s/{link_id}" + (f"?{query}" if query else "")
        return RedirectResponse(fallback, status_code=302)
    if not target.startswith(("http://", "https://")):
        return _error(request, "Forward failed", "The source returned an unsafe redirect target.", 502)

    if not consume_download(link_id):
        return _error(request, "Link unavailable", "This link reached its download limit.", 410)
    record_delivery(link_id, "forward", 0, client_ip(request), request.headers.get("user-agent"))
    return RedirectResponse(target, status_code=302, headers={"cache-control": "no-store"})
