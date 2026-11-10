"""Password hashing, sessions, signed tokens and rate limiting."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import threading
import time
from collections import deque

from .config import settings
from .utils import now

SESSION_COOKIE = "cascade_session"
LINK_TOKEN_TTL = 12 * 3600
PBKDF2_ITERATIONS = 240_000
DUMMY_HASH = (
    "pbkdf2_sha256$240000$"
    "00000000000000000000000000000000$"
    "0000000000000000000000000000000000000000000000000000000000000000"
)


def hash_password(password: str) -> str:
    if not password:
        raise ValueError("password must not be empty")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    if not password or not stored:
        return False
    try:
        algorithm, iterations, salt_hex, digest_hex = stored.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def random_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
RESERVED_IDS = {
    "admin",
    "api",
    "static",
    "login",
    "logout",
    "setup",
    "signup",
    "healthz",
    "favicon.ico",
    "robots.txt",
    "s",
    "f",
    "assets",
    "public",
}


def random_short_id(length: int = 8) -> str:
    length = max(4, min(32, int(length)))
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


def unique_short_id(conn: sqlite3.Connection, length: int = 8) -> str:
    for _ in range(40):
        candidate = random_short_id(length)
        if candidate.lower() in RESERVED_IDS:
            continue
        exists = conn.execute("SELECT 1 FROM links WHERE id = ?", (candidate,)).fetchone()
        if not exists:
            return candidate
    # Extremely unlikely, but never hand back a collision.
    while True:
        candidate = secrets.token_urlsafe(12).replace("-", "x").replace("_", "y")[:16]
        if candidate.lower() not in RESERVED_IDS and not conn.execute(
            "SELECT 1 FROM links WHERE id = ?", (candidate,)
        ).fetchone():
            return candidate


def create_session(
    conn: sqlite3.Connection,
    user_id: int,
    ip: str | None,
    ua: str | None,
    days: int = 7,
) -> tuple[str, str]:
    token = random_token(32)
    csrf = random_token(24)
    created = now()
    expires = created + max(1, days) * 86400
    conn.execute(
        "INSERT INTO sessions (token, user_id, csrf, created_at, expires_at, ip, ua)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (token, user_id, csrf, created, expires, ip, (ua or "")[:400]),
    )
    conn.execute("DELETE FROM sessions WHERE expires_at < ?", (created,))
    return token, csrf


def load_session(conn: sqlite3.Connection, token: str | None):
    if not token:
        return None
    row = conn.execute(
        "SELECT s.token AS session_token, s.csrf AS session_csrf, s.expires_at AS session_expires,"
        " u.* FROM sessions s JOIN users u ON u.id = s.user_id"
        " WHERE s.token = ? AND s.expires_at > ?",
        (token, now()),
    ).fetchone()
    if row is None:
        return None
    if row["disabled"]:
        return None
    return row


def delete_session(token: str | None) -> None:
    if token:
        from .db import execute

        execute("DELETE FROM sessions WHERE token = ?", (token,))


def _sign(payload: str) -> str:
    return hmac.new(settings.secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def sign_value(value: str) -> str:
    """Return `value:timestamp:signature` using the app secret."""
    payload = f"{value}:{now()}"
    return f"{payload}:{_sign(payload)}"


def unsign_value(token: str, max_age: int = LINK_TOKEN_TTL) -> str | None:
    if not token:
        return None
    try:
        value, timestamp, signature = token.rsplit(":", 2)
        issued = int(timestamp)
    except (ValueError, AttributeError):
        return None
    if time.time() - issued > max_age or issued > time.time() + 300:
        return None
    if not hmac.compare_digest(signature, _sign(f"{value}:{timestamp}")):
        return None
    return value


def make_link_token(link_id: str) -> str:
    return sign_value(f"link:{link_id}")


def check_link_token(token: str | None, link_id: str) -> bool:
    if not token:
        return False
    value = unsign_value(token)
    return value == f"link:{link_id}"


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def new_api_key() -> tuple[str, str, str]:
    """Returns (full_key, prefix, hash)."""
    secret = secrets.token_urlsafe(32)
    key = f"csd_{secret}"
    return key, key[:12], hash_api_key(key)


class RateLimiter:
    """Tiny in-memory sliding window limiter, keyed per IP or API key."""

    def __init__(self, limit: int, window_seconds: int = 60) -> None:
        self.limit = max(1, int(limit))
        self.window = max(1, int(window_seconds))
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, cost: int = 1) -> bool:
        if self.limit <= 0:
            return True
        cutoff = time.time() - self.window
        with self._lock:
            bucket = self._hits.get(key)
            if bucket is None:
                bucket = deque()
                self._hits[key] = bucket
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) + cost > self.limit:
                return False
            for _ in range(cost):
                bucket.append(time.time())
            if len(self._hits) > 20_000:
                self._prune(cutoff)
            return True

    def _prune(self, cutoff: float) -> None:
        stale = [k for k, v in self._hits.items() if not v or v[-1] < cutoff]
        for key in stale:
            self._hits.pop(key, None)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


login_limiter = RateLimiter(10, 300)
unlock_limiter = RateLimiter(20, 300)
api_limiter = RateLimiter(240, 60)
