"""OAuth sign-in routes for cloud-drive nodes."""

from __future__ import annotations

import json
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from .. import db
from ..deps import Auth, client_ip, request_base, require_admin
from ..oauth import (
    PROVIDERS,
    OAuthError,
    authorization_url,
    exchange_code,
    is_configured,
    sign_state,
    token_expiry,
    verify_state,
)
from ..utils import now

router = APIRouter(prefix="/oauth", tags=["oauth"])


def _redirect(status: str, message: str = "", provider: str = "") -> RedirectResponse:
    target = f"/admin/nodes?oauth={quote(status)}"
    if message:
        target += f"&message={quote(message[:300])}"
    if provider:
        target += f"&provider={quote(provider[:80])}"
    return RedirectResponse(target, status_code=303)


def _redirect_uri(request: Request, kind: str) -> str:
    return f"{request_base(request)}/oauth/{kind}/callback"


def _unique_name(label: str) -> str:
    rows = db.fetchall("SELECT name FROM nodes")
    used = {str(row["name"]).lower() for row in rows}
    if label.lower() not in used:
        return label
    for index in range(2, 100):
        candidate = f"{label} {index}"
        if candidate.lower() not in used:
            return candidate
    return f"{label} {now()}"


@router.get("/{kind}/start")
async def oauth_start(
    request: Request,
    kind: str,
    name: str = "",
    auth: Auth = Depends(require_admin),
):
    meta = PROVIDERS.get(kind)
    if meta is None:
        return _redirect("error", "Unknown cloud-drive provider")
    if not is_configured(kind):
        return _redirect(
            "error",
            f"{meta['label']} sign-in is not configured. Add its OAuth client ID and secret to .env.",
            str(meta["label"]),
        )
    suggested = (name or "").strip()[:80] or str(meta["label"])
    state = sign_state(auth.user["id"], kind, suggested)
    try:
        target = authorization_url(kind, _redirect_uri(request, kind), state)
    except OAuthError as exc:
        return _redirect("error", str(exc), str(meta["label"]))
    return RedirectResponse(target, status_code=303)


@router.get("/{kind}/callback")
async def oauth_callback(
    request: Request,
    kind: str,
    code: str = "",
    state: str = "",
    error: str = "",
    error_description: str = "",
    auth: Auth = Depends(require_admin),
):
    meta = PROVIDERS.get(kind)
    if meta is None:
        return _redirect("error", "Unknown cloud-drive provider")
    if error:
        return _redirect(
            "error",
            f"{meta['label']} sign-in was cancelled: {error_description or error}",
            str(meta["label"]),
        )
    if not code:
        return _redirect("error", "The provider did not return an authorization code", str(meta["label"]))
    try:
        payload = verify_state(state)
        if payload.get("kind") != kind:
            raise OAuthError("the sign-in response was for a different provider")
        if int(payload.get("uid") or 0) != int(auth.user["id"]):
            raise OAuthError("this sign-in request belongs to a different administrator")
    except OAuthError as exc:
        return _redirect("error", str(exc), str(meta["label"]))

    try:
        token = await exchange_code(kind, code, _redirect_uri(request, kind))
    except OAuthError as exc:
        return _redirect("error", str(exc), str(meta["label"]))

    access_token = str(token.get("access_token") or "")
    if not access_token:
        return _redirect("error", "The provider did not return an access token", str(meta["label"]))
    refresh_token = str(token.get("refresh_token") or "")
    label = str(payload.get("name") or meta["label"])[:80].strip() or str(meta["label"])
    name = _unique_name(label)
    config = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_expires_at": token_expiry(token),
        "oauth": True,
    }
    node_id = db.execute(
        "INSERT INTO nodes (name, kind, config, enabled, status, created_at)"
        " VALUES (?, ?, ?, 1, 'unknown', ?)",
        (name, kind, json.dumps(config, separators=(",", ":")), now()),
    )
    db.audit(
        auth.user["id"],
        "node.oauth_connected",
        {"id": node_id, "name": name, "kind": kind},
        client_ip(request),
    )
    return _redirect(
        "connected",
        f"{name} is connected. Press Test on its card to check access.",
        str(meta["label"]),
    )
