"""Request authentication helpers."""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from urllib.parse import urlsplit

from fastapi import Request

from . import db
from .config import settings
from .security import SESSION_COOKIE, load_session


class LoginRequired(Exception):
    def __init__(self, next_url: str = "/admin") -> None:
        self.next_url = next_url
        super().__init__("login required")


class PermissionDenied(Exception):
    pass


@dataclass
class Auth:
    user: object
    csrf: str
    token: str

    @property
    def is_admin(self) -> bool:
        return self.user["role"] == "admin"


def client_ip(request: Request) -> str:
    if settings.trust_proxy:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


def get_auth(request: Request) -> Auth | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    try:
        with db.db() as conn:
            row = load_session(conn, token)
    except Exception:
        return None
    if row is None:
        return None
    return Auth(user=row, csrf=row["session_csrf"], token=token)


def require_auth(request: Request) -> Auth:
    auth = getattr(request.state, "auth", None)
    if auth is None:
        auth = get_auth(request)
        request.state.auth = auth
    if auth is None:
        raise LoginRequired(request.url.path)
    return auth


def require_admin(request: Request) -> Auth:
    auth = require_auth(request)
    if not auth.is_admin:
        raise PermissionDenied("admin access required")
    return auth


def request_base(request: Request) -> str:
    """Public base URL used for generated links."""
    configured = (db.get_setting("public_base_url") or "").strip().rstrip("/")
    if configured:
        return configured
    if settings.trust_proxy:
        forwarded_host = (request.headers.get("x-forwarded-host") or "").split(",")[0].strip()
        if forwarded_host:
            proto = (request.headers.get("x-forwarded-proto") or request.url.scheme)
            proto = proto.split(",")[0].strip() or "http"
            return f"{proto}://{forwarded_host}".rstrip("/")
    return str(request.base_url).rstrip("/")


def same_origin(request: Request) -> bool:
    source = request.headers.get("origin") or request.headers.get("referer")
    if not source:
        return True
    try:
        host = (urlsplit(source).hostname or "").lower()
    except ValueError:
        return False
    request_host = (request.url.hostname or "").lower()
    return bool(host) and hmac.compare_digest(host, request_host)


SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
CSRF_EXEMPT_PREFIXES = ("/api/public/", "/api/v1/")


def csrf_valid(request: Request, auth: Auth | None) -> bool:
    if auth is None:
        return False
    provided = request.headers.get("x-csrf-token") or ""
    return bool(provided) and hmac.compare_digest(provided, auth.csrf)
