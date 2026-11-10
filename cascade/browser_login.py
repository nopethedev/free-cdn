"""Browser-assisted sign-in for cookie-based providers.

Some providers do not expose an OAuth app flow.  Their private share APIs use a
session cookie instead.  A web page cannot read an HttpOnly cookie set on
another origin, so Cascade launches a short-lived Chromium window on the
machine that runs the server.  The helper process watches the browser context,
writes the cookie straight into the node's SQLite config, and never sends the
cookie back to the admin page.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import db
from .config import settings
from .utils import now

ROOT_NAME = "browser-login"
COOKIE_LIMIT = 4096


class BrowserLoginError(Exception):
    """A browser sign-in cannot be started or completed."""


class BrowserLoginNotFound(BrowserLoginError):
    """The sign-in session does not exist or belongs to another user."""


BROWSER_LOGINS: dict[str, dict[str, Any]] = {
    "terabox": {
        "label": "TeraBox",
        "node_name": "TeraBox",
        "login_url": "https://www.terabox.com/",
        "cookie_domains": (
            "terabox.com",
            "terabox.app",
            "teraboxapp.com",
            "freeterabox.com",
            "1024tera.com",
        ),
        "cookie_names": ("ndus", "BDUSS"),
    }
}


@dataclass
class _Session:
    id: str
    kind: str
    user_id: int
    node_id: int | None
    name: str
    ip: str
    created_at: int
    expires_at: int
    process: subprocess.Popen
    result_path: Path
    request_path: Path
    state: str = "waiting"
    message: str = ""
    saved_node_id: int | None = None


_sessions: dict[str, _Session] = {}
_lock = threading.RLock()


def provider(kind: str) -> dict[str, Any]:
    meta = BROWSER_LOGINS.get(str(kind))
    if meta is None:
        raise BrowserLoginError("browser sign-in is not available for this provider")
    return meta


def playwright_available() -> bool:
    return importlib.util.find_spec("playwright") is not None


def _root() -> Path:
    path = settings.data_dir / ROOT_NAME
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def _write_private_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    try:
        os.chmod(temp, 0o600)
    except OSError:
        pass
    os.replace(temp, path)


def _read_private_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def cookie_header(cookies: list[dict[str, Any]], domains: tuple[str, ...], names: tuple[str, ...]) -> str:
    """Return only the provider's session cookies as a Cookie header value."""
    roots = tuple(str(domain).lstrip(".").lower() for domain in domains)
    wanted = {str(name) for name in names}
    found: dict[str, str] = {}
    for cookie in cookies:
        name = str(cookie.get("name") or "")
        if name not in wanted or name in found:
            continue
        domain = str(cookie.get("domain") or "").lstrip(".").lower()
        if not any(domain == root or domain.endswith("." + root) for root in roots):
            continue
        value = str(cookie.get("value") or "")
        if not value or any(char in value for char in ";\r\n"):
            continue
        found[name] = value
    header = "; ".join(f"{name}={value}" for name, value in found.items())
    return header[:COOKIE_LIMIT]


def _public(session: _Session) -> dict[str, Any]:
    return {
        "id": session.id,
        "kind": session.kind,
        "label": BROWSER_LOGINS.get(session.kind, {}).get("label", session.kind),
        "state": session.state,
        "message": session.message,
        "expires_at": session.expires_at,
        "seconds_left": max(0, session.expires_at - int(time.time())),
        "node_id": session.saved_node_id or session.node_id,
    }


def _terminate(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        else:
            process.terminate()
    except (OSError, ProcessLookupError):
        try:
            process.terminate()
        except OSError:
            pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            if os.name == "posix":
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            else:
                process.kill()
        except (OSError, ProcessLookupError):
            pass


def _remove_artifacts(session: _Session) -> None:
    for path in (session.result_path, session.request_path):
        try:
            path.unlink()
        except OSError:
            pass


def _refresh_locked(session: _Session) -> None:
    if session.state != "waiting":
        return
    if session.result_path.exists():
        data = _read_private_json(session.result_path)
        try:
            session.result_path.unlink()
        except OSError:
            pass
        if data.get("ok") and data.get("node_id"):
            try:
                session.saved_node_id = int(data["node_id"])
            except (TypeError, ValueError):
                session.saved_node_id = None
            if session.saved_node_id:
                session.state = "success"
                session.message = str(data.get("message") or "Browser sign-in saved.")
                return
        session.state = "error"
        session.message = str(
            data.get("error") or "The browser closed before the cookie could be saved."
        )[:500]
        return
    if session.process.poll() is not None:
        session.state = "error"
        session.message = "The sign-in browser closed before Cascade could save the cookie."
        return
    if int(time.time()) >= session.expires_at:
        _terminate(session.process)
        session.state = "expired"
        session.message = "The browser sign-in window expired. Start again and finish signing in."


def _cleanup_locked() -> None:
    now_ts = int(time.time())
    for session_id, session in list(_sessions.items()):
        _refresh_locked(session)
        if session.state in {"success", "error", "expired", "cancelled"}:
            _remove_artifacts(session)
            if now_ts > session.expires_at + 3600:
                _sessions.pop(session_id, None)


def start(
    kind: str,
    user_id: int,
    *,
    node_id: int | None = None,
    name: str = "",
    ip: str = "",
) -> dict[str, Any]:
    """Launch a temporary headed Chromium and return its public session state."""
    meta = provider(kind)
    if not settings.browser_login_enabled:
        raise BrowserLoginError(
            "Browser sign-in is disabled. Set CASCADE_BROWSER_LOGIN=1 to enable it."
        )
    if not playwright_available():
        raise BrowserLoginError(
            "Browser sign-in needs the optional helper. Run "
            "'pip install -r requirements-browser.txt' and 'python -m playwright install chromium'."
        )
    with _lock:
        _cleanup_locked()
        session_id = secrets.token_urlsafe(18)
        root = _root()
        request_path = root / f"{session_id}.request.json"
        result_path = root / f"{session_id}.result.json"
        timeout = int(settings.browser_login_timeout)
        expires_at = int(time.time()) + timeout
        clean_name = str(name or meta["node_name"]).strip()[:80]
        request = {
            "session_id": session_id,
            "kind": kind,
            "user_id": int(user_id),
            "node_id": int(node_id) if node_id else 0,
            "name": clean_name,
            "ip": str(ip or "")[:120],
            "timeout_seconds": timeout,
            "result_path": str(result_path),
        }
        _write_private_json(request_path, request)
        command = [sys.executable, "-m", "cascade.browser_login", session_id, str(request_path)]
        try:
            process = subprocess.Popen(
                command,
                cwd=str(settings.base_dir),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=os.name == "posix",
            )
        except OSError as exc:
            for path in (request_path, result_path):
                try:
                    path.unlink()
                except OSError:
                    pass
            raise BrowserLoginError(f"Could not start the browser helper: {exc}") from exc
        session = _Session(
            id=session_id,
            kind=kind,
            user_id=int(user_id),
            node_id=int(node_id) if node_id else None,
            name=clean_name,
            ip=str(ip or "")[:120],
            created_at=int(time.time()),
            expires_at=expires_at,
            process=process,
            result_path=result_path,
            request_path=request_path,
        )
        _sessions[session_id] = session
        return _public(session)


def status(session_id: str, user_id: int) -> dict[str, Any]:
    with _lock:
        _cleanup_locked()
        session = _sessions.get(str(session_id))
        if session is None or session.user_id != int(user_id):
            raise BrowserLoginNotFound("browser sign-in session not found")
        _refresh_locked(session)
        if session.state in {"success", "error", "expired", "cancelled"}:
            _remove_artifacts(session)
        return _public(session)


def cancel(session_id: str, user_id: int) -> dict[str, Any]:
    with _lock:
        _cleanup_locked()
        session = _sessions.get(str(session_id))
        if session is None or session.user_id != int(user_id):
            raise BrowserLoginNotFound("browser sign-in session not found")
        if session.state == "waiting":
            _terminate(session.process)
            session.state = "cancelled"
            session.message = "Browser sign-in cancelled."
            _remove_artifacts(session)
        return _public(session)


def shutdown() -> None:
    """Stop any browser windows that are still open when Cascade exits."""
    with _lock:
        for session in _sessions.values():
            if session.state == "waiting":
                _terminate(session.process)
                session.state = "cancelled"
                session.message = "Cascade stopped."
                _remove_artifacts(session)


def _node_config(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    try:
        data = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _unique_name(conn, requested: str, fallback: str) -> str:
    base = (requested or fallback).strip()[:80] or fallback
    candidate = base
    index = 2
    while conn.execute("SELECT 1 FROM nodes WHERE name = ?", (candidate,)).fetchone():
        suffix = f" ({index})"
        candidate = base[: max(1, 80 - len(suffix))] + suffix
        index += 1
        if index > 99:
            break
    return candidate


def _save_cookie_node(request: dict[str, Any], header: str) -> tuple[int, str]:
    kind = str(request.get("kind") or "")
    meta = provider(kind)
    user_id = int(request.get("user_id") or 0)
    target = int(request.get("node_id") or 0) or None
    ip = str(request.get("ip") or "")[:120]
    if not header:
        raise BrowserLoginError("No session cookie was found.")
    with db.db() as conn:
        if target:
            row = conn.execute("SELECT * FROM nodes WHERE id = ?", (target,)).fetchone()
            if row is None:
                raise BrowserLoginError("That node no longer exists.")
            if str(row["kind"]) != kind:
                raise BrowserLoginError("That node is not a browser-sign-in provider.")
            config = _node_config(row["config"])
            config["cookie"] = header
            conn.execute(
                "UPDATE nodes SET config = ?, status = 'unknown',"
                " message = 'Browser sign-in saved a new cookie.', checked_at = NULL"
                " WHERE id = ?",
                (json.dumps(config, separators=(",", ":")), target),
            )
            node_id = target
            message = f"{meta['label']} sign-in saved. Press Test on the node."
        else:
            name = _unique_name(conn, str(request.get("name") or ""), meta["node_name"])
            config = {"cookie": header}
            cursor = conn.execute(
                "INSERT INTO nodes (name, kind, config, enabled, status, created_at)"
                " VALUES (?, ?, ?, 1, 'unknown', ?)",
                (name, kind, json.dumps(config, separators=(",", ":")), now()),
            )
            node_id = int(cursor.lastrowid)
            message = f"{meta['label']} connected. Press Test on the node."
        conn.execute(
            "INSERT INTO audit (user_id, action, detail, ip, ts) VALUES (?, ?, ?, ?, ?)",
            (
                user_id,
                "node.browser_login",
                json.dumps({"kind": kind, "node_id": node_id}, separators=(",", ":")),
                ip,
                now(),
            ),
        )
    return node_id, message


def _cookie_wait(context, page, meta: dict[str, Any], timeout: int) -> str:
    deadline = time.monotonic() + timeout
    while True:
        try:
            header = cookie_header(
                context.cookies(),
                tuple(meta["cookie_domains"]),
                tuple(meta["cookie_names"]),
            )
        except Exception as exc:
            raise BrowserLoginError("The sign-in browser closed before the cookie was saved.") from exc
        if header:
            return header
        if page.is_closed():
            raise BrowserLoginError("The sign-in browser was closed before login finished.")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BrowserLoginError(
                f"Browser sign-in expired before a {meta['label']} session cookie appeared."
            )
        try:
            page.wait_for_timeout(min(1000, max(100, int(remaining * 1000))))
        except Exception as exc:
            try:
                header = cookie_header(
                    context.cookies(),
                    tuple(meta["cookie_domains"]),
                    tuple(meta["cookie_names"]),
                )
            except Exception:
                header = ""
            if header:
                return header
            raise BrowserLoginError("The sign-in browser closed before login finished.") from exc


def _run_browser_login(request: dict[str, Any]) -> dict[str, Any]:
    meta = provider(str(request.get("kind") or ""))
    timeout = max(30, min(int(request.get("timeout_seconds") or 600), 1800))
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise BrowserLoginError(
            "Browser sign-in needs the optional helper. Run "
            "'pip install -r requirements-browser.txt' and 'python -m playwright install chromium'."
        ) from exc

    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=False)
        except Exception as exc:
            detail = str(exc)
            lowered = detail.lower()
            if "executable doesn't exist" in lowered or "playwright install" in lowered:
                raise BrowserLoginError(
                    "Chromium is not installed for Playwright. Run "
                    "'python -m playwright install chromium'."
                ) from exc
            if "xserver" in lowered or "x server" in lowered or "display" in lowered:
                raise BrowserLoginError(
                    "Browser sign-in needs a desktop session on the machine running Cascade. "
                    "Run Cascade locally, or use the manual cookie field."
                ) from exc
            if (
                "missing dependencies" in lowered
                or "error while loading shared libraries" in lowered
                or "cannot open shared object file" in lowered
            ):
                raise BrowserLoginError(
                    "Chromium is missing system libraries. On Linux run "
                    "'python -m playwright install --with-deps chromium'."
                ) from exc
            raise BrowserLoginError(f"Could not open Chromium: {detail[:300]}") from exc
        context = None
        try:
            context = browser.new_context()
            page = context.new_page()
            try:
                page.goto(meta["login_url"], wait_until="domcontentloaded", timeout=60000)
            except Exception as exc:
                raise BrowserLoginError(
                    f"Could not open {meta['label']}: {str(exc)[:300]}"
                ) from exc
            try:
                page.bring_to_front()
            except Exception:
                pass
            header = _cookie_wait(context, page, meta, timeout)
            node_id, message = _save_cookie_node(request, header)
            return {"ok": True, "node_id": node_id, "message": message}
        finally:
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass
            try:
                browser.close()
            except Exception:
                pass


def _write_result(path: Path, payload: dict[str, Any]) -> None:
    try:
        _write_private_json(path, payload)
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Cascade browser sign-in helper")
    parser.add_argument("session_id")
    parser.add_argument("request_path")
    args = parser.parse_args(argv)
    request_path = Path(args.request_path)
    request = _read_private_json(request_path)
    result_path = Path(str(request.get("result_path") or ""))
    try:
        if not request:
            return 2
        db.init_db()
        result = _run_browser_login(request)
    except BrowserLoginError as exc:
        if str(result_path):
            _write_result(result_path, {"ok": False, "error": str(exc)[:500]})
        return 1
    except Exception as exc:  # defensive: never leak a traceback or cookie to the page
        if str(result_path):
            _write_result(result_path, {"ok": False, "error": f"Browser sign-in failed: {str(exc)[:400]}"})
        return 1
    else:
        _write_result(result_path, result)
        return 0
    finally:
        try:
            request_path.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
