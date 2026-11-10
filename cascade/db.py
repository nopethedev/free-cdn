"""SQLite storage for Cascade.

Connections are opened per operation (SQLite is fast at this), with WAL mode so
concurrent readers do not block the streaming proxy.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from typing import Any, Iterator

from .config import settings
from .security import hash_password
from .utils import now

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user',
    disabled INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    csrf TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    ip TEXT,
    ua TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    config TEXT NOT NULL DEFAULT '{}',
    enabled INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'unknown',
    latency_ms INTEGER,
    message TEXT,
    checked_at INTEGER,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS links (
    id TEXT PRIMARY KEY,
    node_id INTEGER REFERENCES nodes(id) ON DELETE SET NULL,
    path TEXT,
    url TEXT,
    name TEXT,
    size INTEGER,
    mime TEXT,
    mode TEXT NOT NULL DEFAULT 'stream',
    allow_stream INTEGER NOT NULL DEFAULT 1,
    allow_forward INTEGER NOT NULL DEFAULT 1,
    landing INTEGER NOT NULL DEFAULT 0,
    password_hash TEXT,
    note TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    expires_at INTEGER,
    max_downloads INTEGER,
    downloads INTEGER NOT NULL DEFAULT 0,
    bytes_served INTEGER NOT NULL DEFAULT 0,
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_links_created ON links(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_links_node ON links(node_id);

CREATE TABLE IF NOT EXISTS link_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    link_id TEXT NOT NULL REFERENCES links(id) ON DELETE CASCADE,
    ts INTEGER NOT NULL,
    ip TEXT,
    ua TEXT,
    kind TEXT,
    bytes INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_events_link ON link_events(link_id, ts DESC);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    total INTEGER NOT NULL DEFAULT 0,
    done INTEGER NOT NULL DEFAULT 0,
    message TEXT,
    payload TEXT NOT NULL,
    result TEXT,
    created_by INTEGER,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at DESC);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS remote_cache (
    key TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    headers TEXT,
    provider TEXT,
    public INTEGER NOT NULL DEFAULT 1,
    size INTEGER,
    content_type TEXT,
    expires_at INTEGER,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS api_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    prefix TEXT NOT NULL,
    key_hash TEXT NOT NULL UNIQUE,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    created_at INTEGER NOT NULL,
    last_used_at INTEGER,
    enabled INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    action TEXT NOT NULL,
    detail TEXT,
    ip TEXT,
    ts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(ts DESC);
"""

DEFAULT_SETTINGS: dict[str, str] = {
    "site_name": "Cascade",
    "tagline": "One router in front of every drive you own.",
    "public_signup": "0",
    "default_mode": "stream",
    "short_length": "8",
    "landing_default": "0",
    "max_upload_mb": "4096",
    "max_links_per_user": "200",
    "session_days": "7",
    "ytdlp_enabled": "1",
    "ytdlp_path": "",
    "public_base_url": "",
    "robots_noindex": "1",
    "accent": "#7c5cff",
    "link_rate_limit": "120",
}


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.db_path, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 20000")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextlib.contextmanager
def db() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def fetchall(sql: str, params: tuple | list = ()) -> list[sqlite3.Row]:
    with db() as conn:
        return conn.execute(sql, params).fetchall()


def fetchone(sql: str, params: tuple | list = ()) -> sqlite3.Row | None:
    with db() as conn:
        return conn.execute(sql, params).fetchone()


def execute(sql: str, params: tuple | list = ()) -> int:
    with db() as conn:
        cur = conn.execute(sql, params)
        return cur.lastrowid


def init_db() -> None:
    settings.ensure()
    with db() as conn:
        conn.executescript(SCHEMA)
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value))
        admin = conn.execute("SELECT COUNT(*) AS n FROM users WHERE role = 'admin'").fetchone()
        if (not admin or admin["n"] == 0) and settings.admin_email and settings.admin_password:
            existing = conn.execute(
                "SELECT id FROM users WHERE email = ?", (settings.admin_email.lower(),)
            ).fetchone()
            if not existing:
                conn.execute(
                    "INSERT INTO users (email, name, password_hash, role, created_at)"
                    " VALUES (?, ?, ?, 'admin', ?)",
                    (
                        settings.admin_email.lower(),
                        "Administrator",
                        hash_password(settings.admin_password),
                        now(),
                    ),
                )


def get_settings_map() -> dict[str, str]:
    values = dict(DEFAULT_SETTINGS)
    try:
        for row in fetchall("SELECT key, value FROM settings"):
            values[row["key"]] = row["value"]
    except sqlite3.Error:
        pass
    return values


def get_setting(key: str, default: str = "") -> str:
    row = fetchone("SELECT value FROM settings WHERE key = ?", (key,))
    if row is None:
        return DEFAULT_SETTINGS.get(key, default)
    return row["value"]


def set_setting(key: str, value: str) -> None:
    execute(
        "INSERT INTO settings (key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def audit(user_id: int | None, action: str, detail: Any = "", ip: str | None = None) -> None:
    if isinstance(detail, (dict, list)):
        detail = json.dumps(detail, separators=(",", ":"), sort_keys=True)
    execute(
        "INSERT INTO audit (user_id, action, detail, ip, ts) VALUES (?, ?, ?, ?, ?)",
        (user_id, action, str(detail or "")[:2000], ip, now()),
    )
