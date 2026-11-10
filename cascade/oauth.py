"""OAuth sign-in for writable cloud-drive nodes."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import httpx

from .config import settings
from .utils import now

OAUTH_STATE_TTL = 10 * 60


class OAuthError(Exception):
    """OAuth setup or token exchange failed."""


PROVIDERS: dict[str, dict[str, Any]] = {
    "dropbox": {
        "label": "Dropbox",
        "id_env": "CASCADE_DROPBOX_CLIENT_ID",
        "secret_env": "CASCADE_DROPBOX_CLIENT_SECRET",
        "authorize_url": "https://www.dropbox.com/oauth2/authorize",
        "token_url": "https://api.dropboxapi.com/oauth2/token",
        "scope": (
            "account_info.read files.metadata.read files.metadata.write "
            "files.content.read files.content.write"
        ),
        "authorize_extra": {"token_access_type": "offline"},
    },
    "gdrive": {
        "label": "Google Drive",
        "id_env": "CASCADE_GOOGLE_CLIENT_ID",
        "secret_env": "CASCADE_GOOGLE_CLIENT_SECRET",
        "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "scope": "https://www.googleapis.com/auth/drive",
        "authorize_extra": {"access_type": "offline", "prompt": "consent"},
    },
    "onedrive": {
        "label": "Microsoft OneDrive",
        "id_env": "CASCADE_MICROSOFT_CLIENT_ID",
        "secret_env": "CASCADE_MICROSOFT_CLIENT_SECRET",
        "authorize_url": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "token_url": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        "scope": "offline_access Files.ReadWrite.All",
        "authorize_extra": {"response_mode": "query", "prompt": "consent"},
    },
}


def _client_credentials(kind: str) -> tuple[str, str]:
    meta = PROVIDERS.get(kind)
    if meta is None:
        raise OAuthError(f"unknown OAuth provider '{kind}'")
    client_id = (os.environ.get(str(meta["id_env"])) or "").strip()
    client_secret = (os.environ.get(str(meta["secret_env"])) or "").strip()
    return client_id, client_secret


def is_configured(kind: str) -> bool:
    try:
        client_id, client_secret = _client_credentials(kind)
    except OAuthError:
        return False
    return bool(client_id and client_secret)


def configured_providers() -> list[dict[str, Any]]:
    return [
        {
            "kind": kind,
            "label": meta["label"],
            "configured": is_configured(kind),
        }
        for kind, meta in PROVIDERS.items()
    ]


def authorization_url(kind: str, redirect_uri: str, state: str) -> str:
    meta = PROVIDERS.get(kind)
    if meta is None:
        raise OAuthError(f"unknown OAuth provider '{kind}'")
    client_id, client_secret = _client_credentials(kind)
    if not client_id or not client_secret:
        raise OAuthError(
            f"{meta['label']} sign-in needs {meta['id_env']} and {meta['secret_env']}"
        )
    params: dict[str, str] = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "state": state,
        "scope": str(meta["scope"]),
    }
    params.update({str(key): str(value) for key, value in meta["authorize_extra"].items()})
    return f"{meta['authorize_url']}?{urlencode(params)}"


def _token_payload(kind: str) -> tuple[dict[str, str], dict[str, Any]]:
    meta = PROVIDERS.get(kind)
    if meta is None:
        raise OAuthError(f"unknown OAuth provider '{kind}'")
    client_id, client_secret = _client_credentials(kind)
    if not client_id or not client_secret:
        raise OAuthError(f"{meta['label']} OAuth is not configured")
    return (
        {"client_id": client_id, "client_secret": client_secret},
        meta,
    )


async def _token_request(kind: str, data: dict[str, str]) -> dict[str, Any]:
    _, meta = _token_payload(kind)
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                str(meta["token_url"]),
                data=data,
                headers={"Accept": "application/json"},
            )
    except httpx.HTTPError as exc:
        raise OAuthError(f"{meta['label']} token request failed: {exc}") from exc
    try:
        payload = response.json()
    except ValueError as exc:
        raise OAuthError(
            f"{meta['label']} returned an invalid token response"
        ) from exc
    if response.status_code >= 400 or not isinstance(payload, dict):
        message = (
            payload.get("error_description")
            or payload.get("error")
            or f"HTTP {response.status_code}"
        )
        raise OAuthError(f"{meta['label']} sign-in failed: {str(message)[:300]}")
    if not payload.get("access_token"):
        raise OAuthError(f"{meta['label']} did not return an access token")
    return payload


async def exchange_code(kind: str, code: str, redirect_uri: str) -> dict[str, Any]:
    base, _ = _token_payload(kind)
    data = {
        **base,
        "code": code,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }
    if kind == "onedrive":
        data["scope"] = "offline_access Files.ReadWrite.All"
    return await _token_request(kind, data)


async def refresh_access_token(kind: str, refresh_token: str) -> dict[str, Any]:
    base, _ = _token_payload(kind)
    data = {
        **base,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }
    if kind == "onedrive":
        data["scope"] = "offline_access Files.ReadWrite.All"
    return await _token_request(kind, data)


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _signature(body: str) -> str:
    return hmac.new(
        settings.secret.encode("utf-8"),
        body.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()


def sign_state(user_id: int, kind: str, name: str) -> str:
    payload = {
        "uid": int(user_id),
        "kind": kind,
        "name": name[:80],
        "nonce": secrets.token_urlsafe(8),
        "exp": int(time.time()) + OAUTH_STATE_TTL,
    }
    body = _b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    return f"{body}.{_signature(body)}"


def verify_state(state: str) -> dict[str, Any]:
    try:
        body, signature = state.split(".", 1)
    except ValueError as exc:
        raise OAuthError("invalid sign-in state") from exc
    if not body or not hmac.compare_digest(_signature(body), signature):
        raise OAuthError("invalid sign-in state")
    try:
        payload = json.loads(_b64decode(body).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise OAuthError("invalid sign-in state") from exc
    if not isinstance(payload, dict):
        raise OAuthError("invalid sign-in state")
    if int(payload.get("exp") or 0) < int(time.time()):
        raise OAuthError("the sign-in request expired; start again")
    return payload


def token_expiry(payload: dict[str, Any]) -> int:
    try:
        expires_in = int(payload.get("expires_in") or 3600)
    except (TypeError, ValueError):
        expires_in = 3600
    return now() + max(60, expires_in)
