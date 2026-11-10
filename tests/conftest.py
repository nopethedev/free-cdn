from __future__ import annotations

import functools
import http.server
import os
import re
import shutil
import threading
from pathlib import Path

TEST_DATA = Path(__file__).resolve().parent / ".data"
shutil.rmtree(TEST_DATA, ignore_errors=True)
TEST_DATA.mkdir(parents=True, exist_ok=True)

os.environ["CASCADE_DATA_DIR"] = str(TEST_DATA)
os.environ["CASCADE_SECRET"] = "test-secret-key-with-enough-entropy-123456"
os.environ["CASCADE_ADMIN_EMAIL"] = "admin@example.com"
os.environ["CASCADE_ADMIN_PASSWORD"] = "admin-password"
os.environ["CASCADE_YTDLP"] = "yt-dlp"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


class Panel:
    """Admin client that always sends the CSRF token."""

    def __init__(self, client: TestClient, csrf: str) -> None:
        self.client = client
        self.csrf = csrf

    def _headers(self, extra=None):
        headers = {"X-CSRF-Token": self.csrf}
        if extra:
            headers.update(extra)
        return headers

    def get(self, url, **kwargs):
        return self.client.get(url, **kwargs)

    def post(self, url, **kwargs):
        kwargs["headers"] = self._headers(kwargs.get("headers"))
        return self.client.post(url, **kwargs)

    def patch(self, url, **kwargs):
        kwargs["headers"] = self._headers(kwargs.get("headers"))
        return self.client.patch(url, **kwargs)

    def delete(self, url, **kwargs):
        kwargs["headers"] = self._headers(kwargs.get("headers"))
        return self.client.delete(url, **kwargs)


def extract_csrf(html: str) -> str:
    match = re.search(r'<meta name="csrf-token" content="([^"]+)"', html)
    assert match, "csrf meta tag missing"
    return match.group(1)


@pytest.fixture(scope="session")
def client():
    from cascade.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def clean_state(client):
    from cascade import db
    from cascade.security import api_limiter, login_limiter, unlock_limiter

    with db.db() as conn:
        for table in (
            "link_events",
            "links",
            "jobs",
            "nodes",
            "api_keys",
            "remote_cache",
            "sessions",
        ):
            conn.execute(f"DELETE FROM {table}")
        conn.execute("DELETE FROM users WHERE email != ?", ("admin@example.com",))
    login_limiter.reset()
    unlock_limiter.reset()
    api_limiter.reset()
    shutil.rmtree(TEST_DATA / "files", ignore_errors=True)
    (TEST_DATA / "files").mkdir(parents=True, exist_ok=True)
    yield


@pytest.fixture
def panel(client, clean_state):
    from cascade.security import login_limiter

    login_limiter.reset()
    response = client.post(
        "/login",
        data={"email": "admin@example.com", "password": "admin-password"},
        follow_redirects=True,
    )
    assert response.status_code == 200, response.text[:500]
    return Panel(client, extract_csrf(response.text))


@pytest.fixture
def local_file(clean_state):
    from cascade.config import settings

    data = bytes(range(256)) * 4
    (settings.files_dir / "movie.bin").write_bytes(data)
    return data


@pytest.fixture
def make_node(panel):
    def _make(name="Local disk", kind="local", config=None):
        response = panel.post(
            "/api/nodes",
            json={"name": name, "kind": kind, "config": config or {}},
        )
        assert response.status_code == 200, response.text
        return response.json()["node"]["id"]

    return _make


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args, **kwargs):  # silence test server logs
        pass


@pytest.fixture
def file_server(tmp_path):
    directory = tmp_path / "served"
    directory.mkdir()
    (directory / "clip.txt").write_text("direct source contents", encoding="utf-8")
    (directory / "movie.bin").write_bytes(bytes(range(256)) * 8)
    handler = functools.partial(QuietHandler, directory=str(directory))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
