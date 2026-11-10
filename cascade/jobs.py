"""Background jobs. Currently replication between storage nodes."""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from typing import Any, AsyncIterator

from . import db
from .nodes import build_connector
from .utils import base_name, normalize_path, now

QUEUE: asyncio.Queue[str] = asyncio.Queue()
_worker: asyncio.Task | None = None


def create_job(kind: str, payload: dict[str, Any], user_id: int | None) -> str:
    job_id = secrets.token_hex(8)
    db.execute(
        "INSERT INTO jobs (id, kind, status, payload, created_by, created_at, updated_at)"
        " VALUES (?, ?, 'queued', ?, ?, ?, ?)",
        (job_id, kind, json.dumps(payload, separators=(",", ":")), user_id, now(), now()),
    )
    return job_id


def _update(job_id: str, **fields: Any) -> None:
    if not fields:
        return
    columns = ", ".join(f"{key} = ?" for key in fields)
    values = list(fields.values()) + [job_id]
    db.execute(f"UPDATE jobs SET {columns} WHERE id = ?", values)


def _fail(job_id: str, message: str) -> None:
    _update(job_id, status="error", message=message[:500], updated_at=now())


async def _close(body: AsyncIterator[bytes]) -> None:
    closer = getattr(body, "aclose", None)
    if closer:
        try:
            await closer()
        except Exception:
            pass


async def _replicate(job_id: str, payload: dict[str, Any]) -> None:
    source = payload.get("source") or {}
    targets = payload.get("targets") or []
    if not targets:
        _fail(job_id, "no replication targets supplied")
        return
    source_node = db.fetchone("SELECT * FROM nodes WHERE id = ?", (source.get("node_id"),))
    if source_node is None:
        _fail(job_id, "source node no longer exists")
        return
    try:
        source_conn = build_connector(source_node)
    except Exception as exc:
        _fail(job_id, f"source node: {exc}")
        return
    if "read" not in source_conn.capabilities:
        _fail(job_id, "source node is not readable")
        return
    source_path = normalize_path(str(source.get("path") or "/"))
    if source_path == "/":
        _fail(job_id, "choose a file to replicate, not the root folder")
        return

    _update(job_id, status="running", message="starting", updated_at=now())
    copied: list[dict[str, Any]] = []
    errors: list[str] = []
    total_bytes = 0
    copied_bytes = 0
    progress_at = 0.0

    for target in targets:
        target_node = db.fetchone("SELECT * FROM nodes WHERE id = ?", (target.get("node_id"),))
        if target_node is None:
            errors.append("target node no longer exists")
            continue
        if not target_node["enabled"]:
            errors.append(f"{target_node['name']}: node is disabled")
            continue
        try:
            target_conn = build_connector(target_node)
        except Exception as exc:
            errors.append(f"{target_node['name']}: {exc}")
            continue
        if "write" not in target_conn.capabilities:
            errors.append(f"{target_node['name']}: node is read-only")
            continue
        target_path = normalize_path(str(target.get("path") or f"/{base_name(source_path)}"))
        if target_path == "/":
            target_path = normalize_path(f"/{base_name(source_path)}")

        try:
            fetch = await source_conn.fetch(source_path)
        except Exception as exc:
            errors.append(f"{source_conn.name}: {exc}")
            continue
        if fetch.size:
            total_bytes += fetch.size * max(1, len(targets) - len(errors))
            _update(job_id, total=total_bytes, updated_at=now())
        _update(
            job_id,
            message=f"copying to {target_node['name']}",
            updated_at=now(),
        )

        async def counted(body: AsyncIterator[bytes], fetch_size: int | None = fetch.size) -> AsyncIterator[bytes]:
            nonlocal copied_bytes, progress_at
            async for chunk in body:
                if not chunk:
                    continue
                copied_bytes += len(chunk)
                if time.time() - progress_at > 1.0:
                    progress_at = time.time()
                    _update(job_id, done=copied_bytes, updated_at=now())
                yield chunk

        try:
            written = await target_conn.upload(
                target_path, counted(fetch.body), size=fetch.size, mime=fetch.mime
            )
            copied.append(
                {
                    "node_id": target_node["id"],
                    "node": target_node["name"],
                    "path": target_path,
                    "bytes": written,
                }
            )
        except Exception as exc:
            errors.append(f"{target_node['name']}: {exc}")
        finally:
            await _close(fetch.body)
        _update(job_id, done=copied_bytes, updated_at=now())

    result = {"copied": copied, "errors": errors}
    if errors and not copied:
        _update(
            job_id,
            status="error",
            done=copied_bytes,
            total=total_bytes or copied_bytes,
            message="; ".join(errors)[:500],
            result=json.dumps(result),
            updated_at=now(),
        )
    else:
        message = f"copied to {len(copied)} node(s)"
        if errors:
            message += " — " + "; ".join(errors)
        _update(
            job_id,
            status="done",
            done=copied_bytes,
            total=total_bytes or copied_bytes,
            message=message[:500],
            result=json.dumps(result),
            updated_at=now(),
        )


async def _run(job_id: str) -> None:
    row = db.fetchone("SELECT * FROM jobs WHERE id = ?", (job_id,))
    if row is None or row["status"] not in ("queued", "running"):
        return
    _update(job_id, status="running", updated_at=now())
    try:
        payload = json.loads(row["payload"] or "{}")
    except json.JSONDecodeError:
        _fail(job_id, "job payload is corrupt")
        return
    if row["kind"] == "replicate":
        await _replicate(job_id, payload)
    else:
        _fail(job_id, f"unknown job kind '{row['kind']}'")


async def _worker_loop() -> None:
    while True:
        job_id = await QUEUE.get()
        try:
            await _run(job_id)
        except asyncio.CancelledError:
            _update(job_id, status="error", message="worker stopped", updated_at=now())
            raise
        except Exception as exc:  # never let one job kill the worker
            _fail(job_id, str(exc) or exc.__class__.__name__)
        finally:
            QUEUE.task_done()


def start() -> None:
    global _worker
    # Anything left mid-flight by a restart goes back in the queue.
    for row in db.fetchall("SELECT id FROM jobs WHERE status IN ('queued', 'running')"):
        db.execute("UPDATE jobs SET status = 'queued' WHERE id = ?", (row["id"],))
        QUEUE.put_nowait(row["id"])
    _worker = asyncio.create_task(_worker_loop(), name="cascade-jobs")


async def stop() -> None:
    global _worker
    if _worker is not None:
        _worker.cancel()
        try:
            await _worker
        except (asyncio.CancelledError, Exception):
            pass
        _worker = None


async def enqueue(job_id: str) -> None:
    await QUEUE.put(job_id)
