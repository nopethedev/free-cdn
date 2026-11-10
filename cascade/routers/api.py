"""JSON API used by the admin panel."""

from __future__ import annotations

import json
import re
from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import JSONResponse, StreamingResponse

from .. import browser_login, db, jobs
from ..deps import Auth, client_ip, require_admin, require_auth
from ..links import LinkError, create_link, link_row, link_summary, link_urls
from ..nodes import (
    NODE_KINDS,
    NodeError,
    build_connector,
    test_node,
)
from ..security import (
    api_limiter,
    hash_api_key,
    hash_password,
    new_api_key,
)
from ..utils import (
    base_name,
    content_disposition,
    guess_mime,
    join_path,
    normalize_path,
    now,
    parent_path,
)

router = APIRouter(prefix="/api", tags=["api"])

SECRET_KEYS = {"access_token", "refresh_token", "password", "cookie", "token", "secret_key", "session_token"}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _node_config(node) -> dict[str, Any]:
    raw = node["config"] if node is not None else "{}"
    if isinstance(raw, dict):
        return raw
    try:
        data = json.loads(raw or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _node_json(node) -> dict[str, Any]:
    cfg = _node_config(node)
    caps: list[str] = []
    try:
        caps = build_connector(node).capabilities_list()
    except Exception:
        caps = []
    return {
        "id": node["id"],
        "name": node["name"],
        "kind": node["kind"],
        "kind_label": NODE_KINDS.get(node["kind"], {}).get("label", node["kind"]),
        "enabled": bool(node["enabled"]),
        "status": node["status"],
        "latency_ms": node["latency_ms"],
        "message": node["message"],
        "checked_at": node["checked_at"],
        "capabilities": caps,
        "config": {key: value for key, value in cfg.items() if key not in SECRET_KEYS},
        "secrets": {key: bool(cfg.get(key)) for key in SECRET_KEYS},
    }


def _validate_config(kind: str, config: Any, *, require_fields: bool = True) -> dict[str, Any]:
    if not isinstance(config, dict):
        raise HTTPException(400, "config must be an object")
    meta = NODE_KINDS.get(kind)
    if meta is None:
        raise HTTPException(400, f"unknown node kind '{kind}'")
    clean: dict[str, Any] = {}
    for field in meta["fields"]:
        key = field["key"]
        if key not in config:
            continue
        value = config[key]
        if value is None:
            continue
        if field.get("type") == "textarea" and isinstance(value, str):
            value = value.strip()
        elif isinstance(value, str):
            value = value.strip()
        clean[key] = value
    if require_fields:
        for field in meta["fields"]:
            if field.get("required") and not str(clean.get(field["key"]) or "").strip():
                raise HTTPException(400, f"{field['label']} is required")
    if kind in ("filebrowser", "http", "webdav"):
        base = str(clean.get("base_url") or "")
        if base and not base.startswith(("http://", "https://")):
            raise HTTPException(400, "base URL must start with http:// or https://")
    if kind == "s3":
        endpoint = str(clean.get("endpoint") or "")
        if endpoint and not endpoint.startswith(("http://", "https://")):
            raise HTTPException(400, "S3 endpoint must start with http:// or https://")
        prefix = str(clean.get("prefix") or "")
        if ".." in prefix or "\\" in prefix:
            raise HTTPException(400, "S3 prefix must be a relative path")
        clean["prefix"] = prefix.strip("/")
    if kind == "local":
        root = str(clean.get("root") or "")
        if ".." in root or "\\" in root:
            raise HTTPException(400, "subfolder must be a relative path")
        clean["root"] = root.strip("/")
    if kind == "http" and isinstance(clean.get("headers"), str) and clean["headers"]:
        try:
            parsed = json.loads(clean["headers"])
        except json.JSONDecodeError:
            raise HTTPException(400, "extra headers must be valid JSON")
        if not isinstance(parsed, dict):
            raise HTTPException(400, "extra headers must be a JSON object")
        clean["headers"] = {str(k): str(v) for k, v in parsed.items()}
    for key, value in clean.items():
        if isinstance(value, str) and len(value) > 4000:
            raise HTTPException(400, f"{key} is too long")
    return clean


def _get_node(node_id: int):
    node = db.fetchone("SELECT * FROM nodes WHERE id = ?", (node_id,))
    if node is None:
        raise HTTPException(404, "node not found")
    return node


def _connector_for(node_id: int):
    node = _get_node(node_id)
    try:
        return node, build_connector(node)
    except NodeError as exc:
        raise HTTPException(400, str(exc)) from exc


def _job_json(row) -> dict[str, Any]:
    def maybe_json(value):
        if not value:
            return None
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return value

    return {
        "id": row["id"],
        "kind": row["kind"],
        "status": row["status"],
        "total": row["total"],
        "done": row["done"],
        "message": row["message"],
        "payload": maybe_json(row["payload"]),
        "result": maybe_json(row["result"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _json_body(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise HTTPException(400, "expected a JSON object")
    return payload


async def _read_json(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(400, "invalid JSON body")
    return _json_body(payload)


def _int(value: Any, field: str, *, minimum: int | None = None) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise HTTPException(400, f"{field} must be a number")
    if minimum is not None and number < minimum:
        raise HTTPException(400, f"{field} must be at least {minimum}")
    return number


# ---------------------------------------------------------------------------
# nodes
# ---------------------------------------------------------------------------


@router.get("/nodes")
async def list_nodes(auth: Auth = Depends(require_admin)):
    rows = db.fetchall("SELECT * FROM nodes ORDER BY created_at ASC")
    return {"nodes": [_node_json(row) for row in rows]}


@router.post("/nodes")
async def create_node(request: Request, auth: Auth = Depends(require_admin)):
    payload = await _read_json(request)
    name = str(payload.get("name") or "").strip()
    kind = str(payload.get("kind") or "").strip()
    if not name:
        raise HTTPException(400, "give the node a name")
    if len(name) > 80:
        raise HTTPException(400, "name is too long")
    config = _validate_config(kind, payload.get("config") or {})
    node_id = db.execute(
        "INSERT INTO nodes (name, kind, config, enabled, status, created_at) VALUES (?, ?, ?, 1, 'unknown', ?)",
        (name, kind, json.dumps(config, separators=(",", ":")), now()),
    )
    db.audit(auth.user["id"], "node.created", {"id": node_id, "name": name, "kind": kind}, client_ip(request))
    node = _get_node(node_id)
    return {"node": _node_json(node)}


@router.post("/nodes/browser/start")
async def browser_login_start(request: Request, auth: Auth = Depends(require_admin)):
    payload = await _read_json(request)
    kind = str(payload.get("kind") or "").strip()
    raw_node = payload.get("node_id")
    node_id = None
    if raw_node not in (None, "", 0):
        node_id = _int(raw_node, "node_id", minimum=1)
        node = _get_node(node_id)
        if node["kind"] != kind:
            raise HTTPException(400, "browser sign-in kind does not match the node")
    name = str(payload.get("name") or "").strip()
    if len(name) > 80:
        raise HTTPException(400, "name is too long")
    try:
        session = browser_login.start(
            kind,
            auth.user["id"],
            node_id=node_id,
            name=name,
            ip=client_ip(request),
        )
    except browser_login.BrowserLoginError as exc:
        raise HTTPException(409, str(exc)) from exc
    db.audit(
        auth.user["id"],
        "node.browser_login_started",
        {"kind": kind, "node_id": node_id},
        client_ip(request),
    )
    return {"session": session}


@router.get("/nodes/browser/{session_id}")
async def browser_login_status(session_id: str, auth: Auth = Depends(require_admin)):
    try:
        session = browser_login.status(session_id, auth.user["id"])
    except browser_login.BrowserLoginNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"session": session}


@router.post("/nodes/browser/{session_id}/cancel")
async def browser_login_cancel(
    session_id: str, request: Request, auth: Auth = Depends(require_admin)
):
    try:
        session = browser_login.cancel(session_id, auth.user["id"])
    except browser_login.BrowserLoginNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    db.audit(
        auth.user["id"],
        "node.browser_login_cancelled",
        {"kind": session["kind"], "node_id": session.get("node_id")},
        client_ip(request),
    )
    return {"session": session}


@router.patch("/nodes/{node_id}")
async def update_node(node_id: int, request: Request, auth: Auth = Depends(require_admin)):
    node = _get_node(node_id)
    payload = await _read_json(request)
    name = node["name"]
    if "name" in payload:
        name = str(payload.get("name") or "").strip()
        if not name or len(name) > 80:
            raise HTTPException(400, "name must be 1-80 characters")
    kind = node["kind"]
    old_config = _node_config(node)
    if "config" in payload:
        incoming = _validate_config(kind, payload.get("config") or {}, require_fields=False)
        merged = dict(old_config)
        for key, value in incoming.items():
            if value is None:
                merged.pop(key, None)
            elif value == "" and key in SECRET_KEYS:
                continue
            else:
                merged[key] = value
        for key in SECRET_KEYS:
            if isinstance(payload.get("config"), dict) and key in payload["config"] and payload["config"][key] is None:
                merged.pop(key, None)
        config = json.dumps(merged, separators=(",", ":"))
    else:
        config = node["config"]
    enabled = int(node["enabled"])
    if "enabled" in payload:
        enabled = 1 if payload.get("enabled") else 0
    db.execute(
        "UPDATE nodes SET name = ?, config = ?, enabled = ? WHERE id = ?",
        (name, config, enabled, node_id),
    )
    db.audit(auth.user["id"], "node.updated", {"id": node_id, "name": name}, client_ip(request))
    return {"node": _node_json(_get_node(node_id))}


@router.delete("/nodes/{node_id}")
async def delete_node(node_id: int, request: Request, auth: Auth = Depends(require_admin)):
    node = _get_node(node_id)
    db.execute("DELETE FROM nodes WHERE id = ?", (node_id,))
    db.audit(auth.user["id"], "node.deleted", {"id": node_id, "name": node["name"]}, client_ip(request))
    return {"ok": True}


@router.post("/nodes/{node_id}/test")
async def node_test(node_id: int, auth: Auth = Depends(require_admin)):
    node = _get_node(node_id)
    result = await test_node(node)
    status = "online" if result["ok"] else "offline"
    db.execute(
        "UPDATE nodes SET status = ?, latency_ms = ?, message = ?, checked_at = ? WHERE id = ?",
        (status, result["latency_ms"], result["message"], now(), node_id),
    )
    result["status"] = status
    return result


# ---------------------------------------------------------------------------
# files
# ---------------------------------------------------------------------------


@router.get("/files")
async def list_files(node: int, path: str = "/", auth: Auth = Depends(require_auth)):
    node_row, connector = _connector_for(node)
    if not node_row["enabled"]:
        raise HTTPException(409, "this node is disabled")
    if "list" not in connector.capabilities:
        raise HTTPException(400, "this node cannot be browsed")
    try:
        clean_path = normalize_path(path)
        items = await connector.list(clean_path)
    except NodeError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {
        "node": {"id": node_row["id"], "name": node_row["name"], "kind": node_row["kind"]},
        "path": clean_path,
        "parent": parent_path(clean_path),
        "items": [item.as_dict() for item in items],
        "capabilities": connector.capabilities_list(),
    }


@router.get("/files/raw")
async def raw_file(
    node: int,
    path: str,
    range_header: str | None = Header(default=None, alias="Range"),
    auth: Auth = Depends(require_auth),
):
    _, connector = _connector_for(node)
    if "read" not in connector.capabilities:
        raise HTTPException(400, "this node cannot be read")
    try:
        fetch = await connector.fetch(normalize_path(path), range_header)
    except NodeError as exc:
        raise HTTPException(400, str(exc)) from exc
    filename = fetch.filename or base_name(path)
    headers = dict(fetch.headers)
    headers["content-disposition"] = content_disposition(filename, inline=False)
    headers["cache-control"] = "private, max-age=0, no-store"
    if "content-type" not in headers:
        headers["content-type"] = fetch.mime or guess_mime(filename)
    return StreamingResponse(fetch.body, status_code=fetch.status, headers=headers)


@router.post("/files/upload")
async def upload_files(
    request: Request,
    node: int = Form(...),
    path: str = Form("/"),
    files: list[UploadFile] = File(...),
    auth: Auth = Depends(require_auth),
):
    if not auth.is_admin:
        raise HTTPException(403, "only administrators can upload")
    _, connector = _connector_for(node)
    if "write" not in connector.capabilities:
        raise HTTPException(400, "this node is read-only")
    try:
        directory = normalize_path(path)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    try:
        max_mb = int(db.get_setting("max_upload_mb", "4096") or 4096)
    except ValueError:
        max_mb = 4096
    max_bytes = max_mb * 1024 * 1024

    uploaded = []
    for upload in files:
        name = base_name(upload.filename or "")
        if not name or name.startswith("."):
            raise HTTPException(400, "invalid file name")
        target = join_path(directory, name)
        if upload.size and upload.size > max_bytes:
            raise HTTPException(413, f"{name} is larger than the {max_mb} MB upload limit")

        async def body(upload=upload, name=name):
            total = 0
            while True:
                chunk = await upload.read(256 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise HTTPException(413, f"{name} exceeded the upload limit")
                yield chunk

        try:
            written = await connector.upload(target, body(), size=upload.size, mime=guess_mime(name))
        except NodeError as exc:
            raise HTTPException(400, f"{name}: {exc}") from exc
        finally:
            await upload.close()

        uploaded.append({"name": name, "path": target, "bytes": written})
    db.audit(
        auth.user["id"],
        "file.uploaded",
        {"node": node, "path": directory, "names": [item["name"] for item in uploaded]},
        client_ip(request),
    )
    return {"uploaded": uploaded, "count": len(uploaded)}


@router.post("/files/mkdir")
async def make_folder(request: Request, auth: Auth = Depends(require_admin)):
    payload = await _read_json(request)
    node_id = _int(payload.get("node"), "node")
    _, connector = _connector_for(node_id)
    try:
        directory = normalize_path(payload.get("path") or "/")
        name = str(payload.get("name") or "").strip().strip("/")
        if not name or "/" in name or ".." in name:
            raise HTTPException(400, "folder name is invalid")
        target = join_path(directory, name)
        await connector.mkdir(target)
    except NodeError as exc:
        raise HTTPException(400, str(exc)) from exc
    db.audit(auth.user["id"], "file.mkdir", {"node": node_id, "path": target}, client_ip(request))
    return {"ok": True, "path": target}


@router.post("/files/rename")
async def rename_file(request: Request, auth: Auth = Depends(require_admin)):
    payload = await _read_json(request)
    node_id = _int(payload.get("node"), "node")
    _, connector = _connector_for(node_id)
    path = normalize_path(payload.get("path") or "/")
    new_name = str(payload.get("new_name") or "").strip().strip("/")
    if not new_name or "/" in new_name or ".." in new_name:
        raise HTTPException(400, "new name is invalid")
    if path == "/":
        raise HTTPException(400, "cannot rename the root")
    try:
        new_path = join_path(parent_path(path), new_name)
        await connector.rename(path, new_path)
    except NodeError as exc:
        raise HTTPException(400, str(exc)) from exc
    db.audit(auth.user["id"], "file.renamed", {"node": node_id, "from": path, "to": new_path}, client_ip(request))
    return {"ok": True, "path": new_path}


@router.post("/files/delete")
async def delete_file(request: Request, auth: Auth = Depends(require_admin)):
    payload = await _read_json(request)
    node_id = _int(payload.get("node"), "node")
    _, connector = _connector_for(node_id)
    path = normalize_path(payload.get("path") or "/")
    if path == "/":
        raise HTTPException(400, "refusing to delete the node root")
    try:
        await connector.delete(path)
    except NodeError as exc:
        raise HTTPException(400, str(exc)) from exc
    db.audit(auth.user["id"], "file.deleted", {"node": node_id, "path": path}, client_ip(request))
    return {"ok": True}


@router.post("/files/replicate")
async def replicate_file(request: Request, auth: Auth = Depends(require_admin)):
    payload = await _read_json(request)
    node_id = _int(payload.get("node"), "node")
    source_node = _get_node(node_id)
    source_connector = build_connector(source_node)
    if "read" not in source_connector.capabilities:
        raise HTTPException(400, "source node cannot be read")
    path = normalize_path(payload.get("path") or "/")
    if path == "/":
        raise HTTPException(400, "choose a file to replicate")
    raw_targets = payload.get("targets") or []
    if not isinstance(raw_targets, list) or not raw_targets:
        raise HTTPException(400, "choose at least one target")
    targets = []
    for target in raw_targets[:20]:
        if not isinstance(target, dict):
            continue
        target_id = _int(target.get("node"), "target node")
        target_node = db.fetchone("SELECT * FROM nodes WHERE id = ?", (target_id,))
        if target_node is None:
            raise HTTPException(400, f"target node {target_id} not found")
        try:
            target_connector = build_connector(target_node)
        except NodeError as exc:
            raise HTTPException(400, str(exc)) from exc
        if "write" not in target_connector.capabilities:
            raise HTTPException(400, f"{target_node['name']} is read-only")
        target_path = normalize_path(target.get("path") or f"/{base_name(path)}")
        if target_path == "/":
            target_path = normalize_path(f"/{base_name(path)}")
        targets.append({"node_id": target_id, "path": target_path})
    job_id = jobs.create_job(
        "replicate",
        {"source": {"node_id": node_id, "path": path}, "targets": targets},
        auth.user["id"],
    )
    await jobs.enqueue(job_id)
    db.audit(
        auth.user["id"],
        "file.replicate",
        {"node": node_id, "path": path, "targets": targets, "job": job_id},
        client_ip(request),
    )
    return {"job_id": job_id}


@router.get("/jobs")
async def list_jobs(auth: Auth = Depends(require_auth)):
    if auth.is_admin:
        rows = db.fetchall("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 50")
    else:
        rows = db.fetchall(
            "SELECT * FROM jobs WHERE created_by = ? ORDER BY created_at DESC LIMIT 50",
            (auth.user["id"],),
        )
    return {"jobs": [_job_json(row) for row in rows]}


@router.get("/jobs/{job_id}")
async def job_detail(job_id: str, auth: Auth = Depends(require_auth)):
    row = db.fetchone("SELECT * FROM jobs WHERE id = ?", (job_id,))
    if row is None:
        raise HTTPException(404, "job not found")
    if not auth.is_admin and row["created_by"] != auth.user["id"]:
        raise HTTPException(403, "not your job")
    return {"job": _job_json(row)}


# ---------------------------------------------------------------------------
# links
# ---------------------------------------------------------------------------


def _can_manage(auth: Auth, link) -> bool:
    return auth.is_admin or link["created_by"] == auth.user["id"]


@router.get("/links")
async def list_links(request: Request, auth: Auth = Depends(require_auth)):
    if auth.is_admin:
        rows = db.fetchall(
            "SELECT l.*, n.name AS node_name, u.email AS owner_email FROM links l"
            " LEFT JOIN nodes n ON n.id = l.node_id LEFT JOIN users u ON u.id = l.created_by"
            " ORDER BY l.created_at DESC"
        )
    else:
        rows = db.fetchall(
            "SELECT l.*, n.name AS node_name, u.email AS owner_email FROM links l"
            " LEFT JOIN nodes n ON n.id = l.node_id LEFT JOIN users u ON u.id = l.created_by"
            " WHERE l.created_by = ? ORDER BY l.created_at DESC",
            (auth.user["id"],),
        )
    return {
        "links": [
            {
                **link_summary(row),
                "node_name": row["node_name"],
                "owner_email": row["owner_email"],
                "urls": link_urls(request, row),
            }
            for row in rows
        ]
    }


@router.post("/links")
async def create_link_endpoint(request: Request, auth: Auth = Depends(require_auth)):
    payload = await _read_json(request)
    try:
        with db.db() as conn:
            link_id = create_link(
                conn,
                user_id=auth.user["id"],
                node_id=payload.get("node_id"),
                path=payload.get("path"),
                url=payload.get("url"),
                name=payload.get("name"),
                size=payload.get("size"),
                mime=payload.get("mime"),
                mode=payload.get("mode") or db.get_setting("default_mode", "stream"),
                allow_stream=payload.get("allow_stream", True),
                allow_forward=payload.get("allow_forward", True),
                landing=payload.get("landing", db.get_setting("landing_default", "0") == "1"),
                password=payload.get("password") or None,
                expires_hours=payload.get("expires_hours"),
                max_downloads=payload.get("max_downloads"),
                note=payload.get("note"),
            )
            link = conn.execute("SELECT * FROM links WHERE id = ?", (link_id,)).fetchone()
    except LinkError as exc:
        raise HTTPException(400, str(exc)) from exc
    db.audit(
        auth.user["id"],
        "link.created",
        {"id": link_id, "node": link["node_id"], "path": link["path"], "url": link["url"]},
        client_ip(request),
    )
    return {
        "link": {**link_summary(link), "urls": link_urls(request, link)},
    }


@router.patch("/links/{link_id}")
async def update_link(link_id: str, request: Request, auth: Auth = Depends(require_auth)):
    link = link_row(link_id)
    if link is None:
        raise HTTPException(404, "link not found")
    if not _can_manage(auth, link):
        raise HTTPException(403, "not your link")
    payload = await _read_json(request)
    updates: dict[str, Any] = {}
    if "name" in payload:
        name = str(payload.get("name") or "").strip()
        updates["name"] = name[:300] or base_name(link["path"] or "") or link_id
    if "mode" in payload:
        mode = str(payload.get("mode") or "").strip().lower()
        if mode not in ("stream", "forward"):
            raise HTTPException(400, "mode must be stream or forward")
        updates["mode"] = mode
    for flag in ("allow_stream", "allow_forward", "landing", "enabled"):
        if flag in payload:
            updates[flag] = 1 if payload.get(flag) else 0
    if not updates.get("allow_stream", link["allow_stream"]) and not updates.get(
        "allow_forward", link["allow_forward"]
    ):
        raise HTTPException(400, "keep at least one delivery mode enabled")
    if "note" in payload:
        updates["note"] = (str(payload.get("note") or "").strip() or None)
    if payload.get("clear_password"):
        updates["password_hash"] = None
    elif payload.get("password"):
        updates["password_hash"] = hash_password(str(payload["password"]))
    if "expires_hours" in payload:
        value = payload.get("expires_hours")
        if value in (None, "", 0, "0"):
            updates["expires_at"] = None
        else:
            try:
                hours = float(value)
            except (TypeError, ValueError):
                raise HTTPException(400, "expiry must be a number of hours")
            if hours <= 0:
                updates["expires_at"] = None
            else:
                updates["expires_at"] = now() + int(hours * 3600)
    if "max_downloads" in payload:
        value = payload.get("max_downloads")
        if value in (None, "", 0, "0"):
            updates["max_downloads"] = None
        else:
            updates["max_downloads"] = _int(value, "max_downloads", minimum=1)
    if updates:
        columns = ", ".join(f"{key} = ?" for key in updates)
        db.execute(f"UPDATE links SET {columns} WHERE id = ?", list(updates.values()) + [link_id])
    db.audit(auth.user["id"], "link.updated", {"id": link_id, "fields": list(updates)}, client_ip(request))
    fresh = link_row(link_id)
    return {"link": {**link_summary(fresh), "urls": link_urls(request, fresh)}}


@router.delete("/links/{link_id}")
async def delete_link(link_id: str, request: Request, auth: Auth = Depends(require_auth)):
    link = link_row(link_id)
    if link is None:
        raise HTTPException(404, "link not found")
    if not _can_manage(auth, link):
        raise HTTPException(403, "not your link")
    db.execute("DELETE FROM links WHERE id = ?", (link_id,))
    db.audit(auth.user["id"], "link.deleted", {"id": link_id, "name": link["name"]}, client_ip(request))
    return {"ok": True}


# ---------------------------------------------------------------------------
# users
# ---------------------------------------------------------------------------


def _admin_count(exclude: int | None = None) -> int:
    if exclude:
        row = db.fetchone(
            "SELECT COUNT(*) AS n FROM users WHERE role = 'admin' AND disabled = 0 AND id != ?",
            (exclude,),
        )
    else:
        row = db.fetchone("SELECT COUNT(*) AS n FROM users WHERE role = 'admin' AND disabled = 0")
    return int(row["n"]) if row else 0


@router.get("/users")
async def list_users(auth: Auth = Depends(require_admin)):
    rows = db.fetchall(
        "SELECT u.*, (SELECT COUNT(*) FROM links WHERE created_by = u.id) AS link_count"
        " FROM users u ORDER BY u.created_at ASC"
    )
    return {
        "users": [
            {
                "id": row["id"],
                "email": row["email"],
                "name": row["name"],
                "role": row["role"],
                "disabled": bool(row["disabled"]),
                "created_at": row["created_at"],
                "link_count": row["link_count"],
            }
            for row in rows
        ]
    }


@router.post("/users")
async def create_user(request: Request, auth: Auth = Depends(require_admin)):
    payload = await _read_json(request)
    email = str(payload.get("email") or "").strip().lower()
    name = str(payload.get("name") or "").strip() or email.split("@")[0]
    password = str(payload.get("password") or "")
    role = str(payload.get("role") or "user").strip().lower()
    if not EMAIL_RE.match(email):
        raise HTTPException(400, "enter a valid email")
    if len(password) < 8:
        raise HTTPException(400, "password must be at least 8 characters")
    if role not in ("admin", "user"):
        raise HTTPException(400, "role must be admin or user")
    if db.fetchone("SELECT id FROM users WHERE email = ?", (email,)):
        raise HTTPException(409, "that email already exists")
    user_id = db.execute(
        "INSERT INTO users (email, name, password_hash, role, created_at) VALUES (?, ?, ?, ?, ?)",
        (email, name[:80], hash_password(password), role, now()),
    )
    db.audit(auth.user["id"], "user.created", {"id": user_id, "email": email, "role": role}, client_ip(request))
    return {"id": user_id, "email": email, "name": name, "role": role}


@router.patch("/users/{user_id}")
async def update_user(user_id: int, request: Request, auth: Auth = Depends(require_admin)):
    user = db.fetchone("SELECT * FROM users WHERE id = ?", (user_id,))
    if user is None:
        raise HTTPException(404, "user not found")
    payload = await _read_json(request)
    updates: dict[str, Any] = {}
    if "name" in payload:
        name = str(payload.get("name") or "").strip()
        if not name:
            raise HTTPException(400, "name cannot be empty")
        updates["name"] = name[:80]
    if "role" in payload:
        role = str(payload.get("role") or "").strip().lower()
        if role not in ("admin", "user"):
            raise HTTPException(400, "role must be admin or user")
        if role != "admin" and user["role"] == "admin" and _admin_count(exclude=user_id) == 0:
            raise HTTPException(400, "keep at least one active administrator")
        updates["role"] = role
    if "disabled" in payload:
        disabled = 1 if payload.get("disabled") else 0
        if disabled and user["role"] == "admin" and _admin_count(exclude=user_id) == 0:
            raise HTTPException(400, "cannot disable the last administrator")
        if disabled and user["id"] == auth.user["id"]:
            raise HTTPException(400, "you cannot disable your own account")
        updates["disabled"] = disabled
    if payload.get("password"):
        password = str(payload.get("password"))
        if len(password) < 8:
            raise HTTPException(400, "password must be at least 8 characters")
        updates["password_hash"] = hash_password(password)
        db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    if updates:
        columns = ", ".join(f"{key} = ?" for key in updates)
        db.execute(f"UPDATE users SET {columns} WHERE id = ?", list(updates.values()) + [user_id])
    db.audit(auth.user["id"], "user.updated", {"id": user_id, "fields": list(updates)}, client_ip(request))
    return {"ok": True}


@router.delete("/users/{user_id}")
async def delete_user(user_id: int, request: Request, auth: Auth = Depends(require_admin)):
    if user_id == auth.user["id"]:
        raise HTTPException(400, "you cannot delete your own account")
    user = db.fetchone("SELECT * FROM users WHERE id = ?", (user_id,))
    if user is None:
        raise HTTPException(404, "user not found")
    if user["role"] == "admin" and _admin_count(exclude=user_id) == 0:
        raise HTTPException(400, "keep at least one active administrator")
    db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    db.audit(auth.user["id"], "user.deleted", {"id": user_id, "email": user["email"]}, client_ip(request))
    return {"ok": True}


# ---------------------------------------------------------------------------
# settings & API keys
# ---------------------------------------------------------------------------


SETTING_KEYS = {
    "site_name",
    "tagline",
    "public_signup",
    "default_mode",
    "short_length",
    "landing_default",
    "max_upload_mb",
    "max_links_per_user",
    "session_days",
    "ytdlp_enabled",
    "ytdlp_path",
    "robots_noindex",
    "accent",
    "link_rate_limit",
    "public_base_url",
}


@router.get("/settings")
async def get_settings_endpoint(auth: Auth = Depends(require_admin)):
    return {"settings": db.get_settings_map()}


@router.post("/settings")
async def save_settings(request: Request, auth: Auth = Depends(require_admin)):
    payload = await _read_json(request)
    changes = {}
    for key, value in payload.items():
        if key not in SETTING_KEYS:
            continue
        text = str(value if value is not None else "").strip()
        if key in ("short_length", "max_upload_mb", "max_links_per_user", "session_days", "link_rate_limit"):
            try:
                number = int(text or "0")
            except ValueError:
                raise HTTPException(400, f"{key} must be a number")
            limits = {
                "short_length": (4, 24),
                "max_upload_mb": (1, 1_048_576),
                "max_links_per_user": (0, 1_000_000),
                "session_days": (1, 365),
                "link_rate_limit": (0, 1_000_000),
            }
            low, high = limits[key]
            if number < low or number > high:
                raise HTTPException(400, f"{key} must be between {low} and {high}")
            text = str(number)
        elif key in ("public_signup", "landing_default", "ytdlp_enabled", "robots_noindex"):
            text = "1" if text.lower() in ("1", "true", "yes", "on") else "0"
        elif key == "default_mode":
            if text not in ("stream", "forward"):
                raise HTTPException(400, "default mode must be stream or forward")
        elif key == "accent":
            if not re.match(r"^#[0-9a-fA-F]{6}$", text):
                raise HTTPException(400, "accent must be a hex colour like #7c5cff")
        elif key == "public_base_url":
            if text and not text.startswith(("http://", "https://")):
                raise HTTPException(400, "public base URL must start with http:// or https://")
            text = text.rstrip("/")
        elif key in ("site_name", "tagline", "ytdlp_path"):
            text = text[:300]
        changes[key] = text
    for key, value in changes.items():
        db.set_setting(key, value)
    db.audit(auth.user["id"], "settings.updated", changes, client_ip(request))
    return {"settings": db.get_settings_map()}


@router.get("/keys")
async def list_keys(auth: Auth = Depends(require_auth)):
    rows = db.fetchall(
        "SELECT * FROM api_keys WHERE user_id = ? ORDER BY created_at DESC", (auth.user["id"],)
    )
    return {
        "keys": [
            {
                "id": row["id"],
                "name": row["name"],
                "prefix": row["prefix"],
                "created_at": row["created_at"],
                "last_used_at": row["last_used_at"],
                "enabled": bool(row["enabled"]),
            }
            for row in rows
        ]
    }


@router.post("/keys")
async def create_key(request: Request, auth: Auth = Depends(require_auth)):
    payload = await _read_json(request)
    name = str(payload.get("name") or "API key").strip()[:80] or "API key"
    count = db.fetchone("SELECT COUNT(*) AS n FROM api_keys WHERE user_id = ?", (auth.user["id"],))
    if count and count["n"] >= 20:
        raise HTTPException(400, "key limit reached (20)")
    key, prefix, key_hash = new_api_key()
    key_id = db.execute(
        "INSERT INTO api_keys (name, prefix, key_hash, user_id, created_at) VALUES (?, ?, ?, ?, ?)",
        (name, prefix, key_hash, auth.user["id"], now()),
    )
    db.audit(auth.user["id"], "apikey.created", {"id": key_id, "name": name}, client_ip(request))
    return {"id": key_id, "name": name, "key": key, "prefix": prefix}


@router.delete("/keys/{key_id}")
async def delete_key(key_id: int, request: Request, auth: Auth = Depends(require_auth)):
    row = db.fetchone("SELECT * FROM api_keys WHERE id = ?", (key_id,))
    if row is None:
        raise HTTPException(404, "key not found")
    if row["user_id"] != auth.user["id"] and not auth.is_admin:
        raise HTTPException(403, "not your key")
    db.execute("DELETE FROM api_keys WHERE id = ?", (key_id,))
    db.audit(auth.user["id"], "apikey.deleted", {"id": key_id}, client_ip(request))
    return {"ok": True}


# ---------------------------------------------------------------------------
# public JSON API (API key auth)
# ---------------------------------------------------------------------------


def _api_key_user(request: Request):
    header = request.headers.get("authorization") or ""
    if not header.lower().startswith("bearer "):
        return None, "missing bearer token"
    key = header.split(None, 1)[1].strip()
    if not key:
        return None, "missing bearer token"
    row = db.fetchone(
        "SELECT k.*, u.disabled AS user_disabled, u.role AS user_role FROM api_keys k"
        " JOIN users u ON u.id = k.user_id WHERE k.key_hash = ?",
        (hash_api_key(key),),
    )
    if row is None:
        return None, "invalid API key"
    if not row["enabled"] or row["user_disabled"]:
        return None, "API key disabled"
    return row, None


@router.post("/v1/links")
async def api_create_link(request: Request):
    key_row, error = _api_key_user(request)
    if key_row is None:
        return JSONResponse({"error": error or "unauthorized"}, status_code=401)
    limiter_key = f"key:{key_row['id']}"
    if not api_limiter.allow(limiter_key):
        return JSONResponse({"error": "rate limit exceeded"}, status_code=429)
    try:
        payload = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"error": "expected a JSON object"}, status_code=400)
    db.execute("UPDATE api_keys SET last_used_at = ? WHERE id = ?", (now(), key_row["id"]))
    try:
        with db.db() as conn:
            link_id = create_link(
                conn,
                user_id=key_row["user_id"],
                node_id=payload.get("node_id"),
                path=payload.get("path"),
                url=payload.get("url"),
                name=payload.get("name"),
                size=payload.get("size"),
                mime=payload.get("mime"),
                mode=payload.get("mode") or db.get_setting("default_mode", "stream"),
                allow_stream=payload.get("allow_stream", True),
                allow_forward=payload.get("allow_forward", True),
                landing=payload.get("landing", False),
                password=payload.get("password") or None,
                expires_hours=payload.get("expires_hours"),
                max_downloads=payload.get("max_downloads"),
                note=payload.get("note"),
            )
            link = conn.execute("SELECT * FROM links WHERE id = ?", (link_id,)).fetchone()
    except LinkError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    db.audit(key_row["user_id"], "link.created.api", {"id": link_id}, client_ip(request))
    return {
        "id": link_id,
        "name": link["name"],
        "mode": link["mode"],
        "urls": link_urls(request, link),
        "expires_at": link["expires_at"],
        "max_downloads": link["max_downloads"],
    }
