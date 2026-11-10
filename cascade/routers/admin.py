"""Admin panel pages."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from .. import db
from ..deps import Auth, require_admin, require_auth
from ..links import link_row, link_urls
from ..nodes import NODE_KINDS, build_connector
from ..oauth import configured_providers
from ..serve import link_state
from ..utils import now
from ..web import render

router = APIRouter(prefix="/admin", tags=["pages"])


SECRET_KEYS = {"access_token", "refresh_token", "password", "cookie", "token", "secret_key", "session_token"}


def _node_config(row) -> dict:
    try:
        data = json.loads(row["config"] or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _nodes_with_caps():
    rows = db.fetchall("SELECT * FROM nodes ORDER BY created_at ASC")
    result = []
    for row in rows:
        caps: list[str] = []
        try:
            caps = build_connector(row).capabilities_list()
        except Exception:
            caps = []
        config = _node_config(row)
        result.append(
            {
                "node": row,
                "caps": caps,
                "kind_label": NODE_KINDS.get(row["kind"], {}).get("label", row["kind"]),
                "config": {key: value for key, value in config.items() if key not in SECRET_KEYS},
                "secrets": {key: bool(config.get(key)) for key in SECRET_KEYS},
            }
        )
    return result


@router.get("")
async def dashboard(request: Request, auth: Auth = Depends(require_auth)):
    node_rows = _nodes_with_caps()
    online = sum(1 for item in node_rows if item["node"]["status"] == "online")
    if auth.is_admin:
        stats = db.fetchone(
            "SELECT COUNT(*) AS total, COALESCE(SUM(downloads), 0) AS downloads,"
            " COALESCE(SUM(bytes_served), 0) AS served FROM links"
        )
        recent_links = db.fetchall(
            "SELECT l.*, n.name AS node_name FROM links l LEFT JOIN nodes n ON n.id = l.node_id"
            " ORDER BY l.created_at DESC LIMIT 6"
        )
        events = db.fetchall(
            "SELECT e.*, l.name AS link_name FROM link_events e LEFT JOIN links l ON l.id = e.link_id"
            " ORDER BY e.ts DESC LIMIT 8"
        )
        jobs = db.fetchall("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 5")
        mode_rows = db.fetchall("SELECT mode, COUNT(*) AS n FROM links GROUP BY mode")
    else:
        stats = db.fetchone(
            "SELECT COUNT(*) AS total, COALESCE(SUM(downloads), 0) AS downloads,"
            " COALESCE(SUM(bytes_served), 0) AS served FROM links WHERE created_by = ?",
            (auth.user["id"],),
        )
        recent_links = db.fetchall(
            "SELECT l.*, n.name AS node_name FROM links l LEFT JOIN nodes n ON n.id = l.node_id"
            " WHERE l.created_by = ? ORDER BY l.created_at DESC LIMIT 6",
            (auth.user["id"],),
        )
        events = db.fetchall(
            "SELECT e.*, l.name AS link_name FROM link_events e JOIN links l ON l.id = e.link_id"
            " WHERE l.created_by = ? ORDER BY e.ts DESC LIMIT 8",
            (auth.user["id"],),
        )
        jobs = db.fetchall(
            "SELECT * FROM jobs WHERE created_by = ? ORDER BY created_at DESC LIMIT 5",
            (auth.user["id"],),
        )
        mode_rows = db.fetchall(
            "SELECT mode, COUNT(*) AS n FROM links WHERE created_by = ? GROUP BY mode",
            (auth.user["id"],),
        )
    modes = {row["mode"]: row["n"] for row in mode_rows}
    urls = {link["id"]: link_urls(request, link) for link in recent_links}
    return render(
        request,
        "dashboard.html",
        auth=auth,
        active="dashboard",
        stats=stats,
        nodes=node_rows,
        online=online,
        recent_links=recent_links,
        urls=urls,
        events=events,
        jobs=jobs,
        modes=modes,
    )


@router.get("/files")
async def files_page(request: Request, auth: Auth = Depends(require_auth)):
    node_rows = _nodes_with_caps()
    browsable = []
    for item in node_rows:
        node = item["node"]
        if not node["enabled"]:
            continue
        browsable.append(
            {
                "id": node["id"],
                "name": node["name"],
                "kind": node["kind"],
                "kind_label": item["kind_label"],
                "caps": item["caps"],
            }
        )
    can_write = auth.is_admin
    bootstrap = {
        "nodes": browsable,
        "canWrite": can_write,
        "canReplicate": can_write,
        "initialNode": browsable[0]["id"] if browsable else None,
    }
    return render(
        request,
        "files.html",
        auth=auth,
        active="files",
        nodes=browsable,
        can_write=can_write,
        bootstrap=bootstrap,
    )


def _links_query(user_id: int | None):
    base = (
        "SELECT l.*, n.name AS node_name, u.email AS owner_email FROM links l"
        " LEFT JOIN nodes n ON n.id = l.node_id LEFT JOIN users u ON u.id = l.created_by"
    )
    if user_id is None:
        return db.fetchall(base + " ORDER BY l.created_at DESC"), None
    return (
        db.fetchall(base + " WHERE l.created_by = ? ORDER BY l.created_at DESC", (user_id,)),
        user_id,
    )


@router.get("/links")
async def links_page(request: Request, auth: Auth = Depends(require_auth)):
    rows, _ = _links_query(None if auth.is_admin else auth.user["id"])
    urls = {row["id"]: link_urls(request, row) for row in rows}
    nodes = db.fetchall("SELECT id, name, kind FROM nodes WHERE enabled = 1 ORDER BY name")
    bootstrap = {
        "nodes": [
            {"id": row["id"], "name": row["name"], "kind": row["kind"]}
            for row in nodes
        ],
        "defaultMode": db.get_setting("default_mode", "stream"),
        "landingDefault": db.get_setting("landing_default", "0") == "1",
    }
    return render(
        request,
        "links.html",
        auth=auth,
        active="links",
        links=rows,
        urls=urls,
        bootstrap=bootstrap,
    )


@router.get("/nodes")
async def nodes_page(
    request: Request,
    auth: Auth = Depends(require_admin),
    oauth: str = "",
    message: str = "",
    provider: str = "",
):
    node_rows = _nodes_with_caps()
    kinds = [
        {
            "kind": kind,
            "label": meta["label"],
            "group": meta.get("group", "Other"),
            "blurb": meta["blurb"],
            "fields": meta["fields"],
        }
        for kind, meta in NODE_KINDS.items()
    ]
    bootstrap = {
        "kinds": kinds,
        "nodes": [
            {
                "id": item["node"]["id"],
                "name": item["node"]["name"],
                "kind": item["node"]["kind"],
                "enabled": bool(item["node"]["enabled"]),
                "config": item["config"],
                "secrets": item["secrets"],
            }
            for item in node_rows
        ],
    }
    notice = None
    notice_kind = "info"
    if oauth == "connected":
        notice = message or f"{provider or 'Cloud drive'} connected. Press Test on its card."
        notice_kind = "good"
    elif oauth == "error":
        notice = message or "Cloud-drive sign-in could not be completed."
        notice_kind = "bad"
    return render(
        request,
        "nodes.html",
        auth=auth,
        active="nodes",
        nodes=node_rows,
        kinds=kinds,
        oauth_providers=configured_providers(),
        oauth_notice=notice,
        oauth_notice_kind=notice_kind,
        bootstrap=bootstrap,
    )


@router.get("/users")
async def users_page(request: Request, auth: Auth = Depends(require_admin)):
    users = db.fetchall(
        "SELECT u.*, (SELECT COUNT(*) FROM links WHERE created_by = u.id) AS link_count,"
        " (SELECT COUNT(*) FROM sessions WHERE user_id = u.id AND expires_at > ?) AS session_count"
        " FROM users u ORDER BY u.created_at ASC",
        (now(),),
    )
    return render(request, "users.html", auth=auth, active="users", users=users)


@router.get("/settings")
async def settings_page(request: Request, auth: Auth = Depends(require_admin)):
    keys = db.fetchall(
        "SELECT * FROM api_keys WHERE user_id = ? ORDER BY created_at DESC", (auth.user["id"],)
    )
    return render(
        request,
        "settings.html",
        auth=auth,
        active="settings",
        values=db.get_settings_map(),
        api_keys=keys,
    )


@router.get("/activity")
async def activity_page(request: Request, auth: Auth = Depends(require_admin)):
    audit_rows = db.fetchall(
        "SELECT a.*, u.email AS user_email FROM audit a LEFT JOIN users u ON u.id = a.user_id"
        " ORDER BY a.ts DESC LIMIT 200"
    )
    events = db.fetchall(
        "SELECT e.*, l.name AS link_name, l.id AS short_id FROM link_events e"
        " LEFT JOIN links l ON l.id = e.link_id ORDER BY e.ts DESC LIMIT 200"
    )
    return render(
        request,
        "activity.html",
        auth=auth,
        active="activity",
        audit_rows=audit_rows,
        events=events,
    )


@router.get("/links/{link_id}")
async def link_detail(request: Request, link_id: str, auth: Auth = Depends(require_auth)):
    link = link_row(link_id)
    if link is None:
        return RedirectResponse("/admin/links", status_code=303)
    if not auth.is_admin and link["created_by"] != auth.user["id"]:
        return RedirectResponse("/admin/links", status_code=303)
    urls = link_urls(request, link)
    events = db.fetchall(
        "SELECT * FROM link_events WHERE link_id = ? ORDER BY ts DESC LIMIT 100", (link_id,)
    )
    node_name = None
    if link["node_id"]:
        node = db.fetchone("SELECT name FROM nodes WHERE id = ?", (link["node_id"],))
        node_name = node["name"] if node else None
    return render(
        request,
        "link_detail.html",
        auth=auth,
        active="links",
        link=link,
        urls=urls,
        events=events,
        node_name=node_name,
        state=link_state(link),
        now_ts=now(),
    )
