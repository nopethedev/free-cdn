"""Turn a link row into a stream or a forward target."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit

from . import db
from .nodes import Connector, NodeError, Unsupported, build_connector
from .remote import Fetch, open_remote
from .resolvers import ResolveError, resolve_direct
from .utils import base_name, guess_mime, now


def load_node(node_id: int | None):
    if not node_id:
        return None
    return db.fetchone("SELECT * FROM nodes WHERE id = ?", (node_id,))


def node_config(node) -> dict[str, Any]:
    if node is None:
        return {}
    raw = node["config"]
    if isinstance(raw, dict):
        return raw
    try:
        data = json.loads(raw or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def link_state(link) -> str:
    if not link["enabled"]:
        return "disabled"
    if link["expires_at"] and link["expires_at"] < now():
        return "expired"
    if link["max_downloads"] and link["downloads"] >= link["max_downloads"]:
        return "exhausted"
    return "active"


def link_block_message(link) -> str:
    state = link_state(link)
    if state == "disabled":
        return "This link has been switched off."
    if state == "expired":
        return "This link has expired."
    if state == "exhausted":
        return "This link reached its download limit."
    return ""


def consume_download(link_id: str) -> bool:
    """Atomically reserve one download, honouring limits and expiry."""
    with db.db() as conn:
        cursor = conn.execute(
            "UPDATE links SET downloads = downloads + 1 WHERE id = ? AND enabled = 1"
            " AND (expires_at IS NULL OR expires_at > ?)"
            " AND (max_downloads IS NULL OR downloads < max_downloads)",
            (link_id, now()),
        )
        return cursor.rowcount > 0


def record_delivery(
    link_id: str, kind: str, nbytes: int, ip: str | None, ua: str | None
) -> None:
    if nbytes < 0:
        nbytes = 0
    with db.db() as conn:
        if nbytes:
            conn.execute(
                "UPDATE links SET bytes_served = bytes_served + ? WHERE id = ?",
                (nbytes, link_id),
            )
        conn.execute(
            "INSERT INTO link_events (link_id, ts, ip, ua, kind, bytes) VALUES (?, ?, ?, ?, ?, ?)",
            (link_id, now(), ip, (ua or "")[:300], kind, nbytes),
        )


async def open_link(link, range_header: str | None = None) -> Fetch:
    """Open the link's source as a byte stream with range passthrough."""
    node = load_node(link["node_id"])
    if node is not None and not node["enabled"]:
        raise NodeError("that node is disabled")
    if link["url"]:
        config = node_config(node)
        resolved = await resolve_direct(link["url"], config)
        filename = link["name"] or base_name(urlsplit(resolved.url).path) or "download"
        mime = link["mime"] or resolved.content_type or guess_mime(filename)
        return await open_remote(
            resolved.url,
            headers=resolved.headers,
            range_header=range_header,
            filename=filename,
            mime=mime,
        )
    if node is None:
        raise NodeError("this link has no source node")
    connector = build_connector(node)
    if "read" in connector.capabilities:
        return await connector.fetch(link["path"] or "/", range_header)
    resolved = await connector.direct(link["path"] or "/")
    if resolved is None:
        raise Unsupported(f"{connector.kind} nodes cannot stream files")
    filename = link["name"] or base_name(link["path"] or "") or "download"
    return await open_remote(
        resolved.url,
        headers=resolved.headers,
        range_header=range_header,
        filename=filename,
        mime=link["mime"] or resolved.content_type or guess_mime(filename),
    )


async def forward_target(link) -> tuple[str | None, str]:
    """Return (url, provider). url is None when only proxying is possible."""
    node = load_node(link["node_id"])
    if node is not None and not node["enabled"]:
        return None, "disabled"
    if link["url"]:
        try:
            resolved = await resolve_direct(link["url"], node_config(node))
        except ResolveError:
            # Falling back to the original share URL still forwards the visitor.
            return link["url"], "raw"
        if resolved.public:
            return resolved.url, resolved.provider
        return None, resolved.provider
    if node is None:
        return None, ""
    try:
        connector = build_connector(node)
        if "direct" not in connector.capabilities:
            return None, connector.kind
        resolved = await connector.direct(link["path"] or "/")
    except (NodeError, Unsupported):
        return None, ""
    if resolved and resolved.public:
        return resolved.url, resolved.provider
    return None, resolved.provider if resolved else ""


def connector_for_link(link) -> Connector | None:
    node = load_node(link["node_id"])
    if node is None:
        return None
    return build_connector(node)
