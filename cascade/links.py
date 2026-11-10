"""Link creation and presentation helpers."""

from __future__ import annotations

import sqlite3
from typing import Any
from urllib.parse import urlsplit

from . import db
from .deps import request_base
from .security import hash_password, unique_short_id
from .utils import base_name, normalize_path, now

MODES = ("stream", "forward")


class LinkError(Exception):
    pass


def link_urls(request, link) -> dict[str, str]:
    base = request_base(request)
    link_id = link["id"]
    return {
        "short": f"{base}/{link_id}",
        "stream": f"{base}/s/{link_id}",
        "forward": f"{base}/f/{link_id}",
    }


def link_row(link_id: str):
    return db.fetchone("SELECT * FROM links WHERE id = ?", (link_id,))


def create_link(
    conn: sqlite3.Connection,
    *,
    user_id: int | None,
    node_id: int | None = None,
    path: str | None = None,
    url: str | None = None,
    name: str | None = None,
    size: int | None = None,
    mime: str | None = None,
    mode: str = "stream",
    allow_stream: bool = True,
    allow_forward: bool = True,
    landing: bool = False,
    password: str | None = None,
    expires_hours: float | None = None,
    max_downloads: int | None = None,
    note: str | None = None,
) -> str:
    mode = (mode or "stream").strip().lower()
    if mode not in MODES:
        raise LinkError("mode must be 'stream' or 'forward'")
    if not allow_stream and not allow_forward:
        raise LinkError("enable at least one delivery mode")

    url = (url or "").strip()
    path = (path or "").strip() or None
    node_id = int(node_id) if node_id else None

    if url:
        if not url.startswith(("http://", "https://")):
            raise LinkError("source URL must start with http:// or https://")
        if len(url) > 4000:
            raise LinkError("source URL is too long")
        node_id = None
        path = None
        if not name:
            name = base_name(urlsplit(url).path) or "shared file"
    else:
        if not node_id:
            raise LinkError("choose a node or paste a URL")
        node = conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
        if node is None:
            raise LinkError("node not found")
        if not node["enabled"]:
            raise LinkError("that node is disabled")
        if not path:
            raise LinkError("choose a file path on the node")
        path = normalize_path(path)
        if not name:
            name = base_name(path) or "shared file"

    max_links = 0
    try:
        max_links = int(db.get_setting("max_links_per_user", "200"))
    except ValueError:
        max_links = 200
    if user_id and max_links > 0:
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM links WHERE created_by = ?", (user_id,)
        ).fetchone()
        if count and count["n"] >= max_links:
            raise LinkError(f"link limit reached ({max_links} per user)")

    length = 8
    try:
        length = int(db.get_setting("short_length", "8"))
    except ValueError:
        length = 8
    link_id = unique_short_id(conn, length)

    expires_at = None
    if expires_hours:
        try:
            hours = float(expires_hours)
        except (TypeError, ValueError):
            if str(expires_hours).strip():
                raise LinkError("expiry must be a number of hours")
            hours = 0
        if hours > 0:
            expires_at = now() + int(hours * 3600)

    max_dl = None
    if max_downloads:
        try:
            max_dl = int(max_downloads)
        except (TypeError, ValueError):
            raise LinkError("download limit must be a number")
        if max_dl <= 0:
            max_dl = None

    password_hash = hash_password(password) if password else None

    conn.execute(
        "INSERT INTO links (id, node_id, path, url, name, size, mime, mode, allow_stream,"
        " allow_forward, landing, password_hash, note, enabled, expires_at, max_downloads,"
        " created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)",
        (
            link_id,
            node_id,
            path,
            url or None,
            (name or "")[:300] or None,
            int(size) if size not in (None, "") else None,
            (mime or "")[:150] or None,
            mode,
            1 if allow_stream else 0,
            1 if allow_forward else 0,
            1 if landing else 0,
            password_hash,
            (note or "")[:500] or None,
            expires_at,
            max_dl,
            user_id,
            now(),
        ),
    )
    return link_id


def link_summary(link) -> dict[str, Any]:
    from .serve import link_state

    return {
        "id": link["id"],
        "name": link["name"] or link["id"],
        "path": link["path"],
        "url": link["url"],
        "mode": link["mode"],
        "node_id": link["node_id"],
        "allow_stream": bool(link["allow_stream"]),
        "allow_forward": bool(link["allow_forward"]),
        "landing": bool(link["landing"]),
        "password": bool(link["password_hash"]),
        "enabled": bool(link["enabled"]),
        "state": link_state(link),
        "downloads": link["downloads"],
        "bytes_served": link["bytes_served"],
        "max_downloads": link["max_downloads"],
        "expires_at": link["expires_at"],
        "created_at": link["created_at"],
        "note": link["note"],
    }
