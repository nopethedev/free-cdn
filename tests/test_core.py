from __future__ import annotations

import asyncio
import json

import pytest

from cascade import db
from cascade.config import settings
from cascade.resolvers import (
    Resolved,
    _resolve_terabox,
    cache_get,
    cache_put,
    dropbox_direct_url,
    extract_gdrive_id,
    extract_terabox_id,
    resolve_with_ytdlp,
)
from cascade.utils import (
    RangeNotSatisfiable,
    content_disposition,
    normalize_path,
    parse_range,
    safe_join,
)


def test_normalize_path_rejects_traversal():
    assert normalize_path("a/b") == "/a/b"
    assert normalize_path("/a//b/") == "/a/b"
    assert normalize_path("") == "/"
    with pytest.raises(ValueError):
        normalize_path("/a/../b")
    assert normalize_path("a\\b") == "/a/b"


def test_safe_join_stays_inside_root(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    assert safe_join(root, "a/b.txt") == (root / "a/b.txt").resolve()
    with pytest.raises(ValueError):
        safe_join(root, "../escape.txt")


def test_parse_range_variants():
    assert parse_range(None, 100) is None
    assert parse_range("bytes=0-9", 100) == (0, 9)
    assert parse_range("bytes=90-", 100) == (90, 99)
    assert parse_range("bytes=-10", 100) == (90, 99)
    assert parse_range("bytes=90-200", 100) == (90, 99)
    assert parse_range("not-a-range", 100) is None
    with pytest.raises(RangeNotSatisfiable):
        parse_range("bytes=100-", 100)


def test_content_disposition_handles_unicode():
    header = content_disposition("héllo world.mp4")
    assert header.startswith("inline;")
    assert "filename*=UTF-8''h%C3%A9llo%20world.mp4" in header
    assert content_disposition("x.bin", inline=False).startswith("attachment;")


def test_dropbox_rewrite():
    url = "https://www.dropbox.com/scl/fi/abc123/movie.mp4?rlkey=key123&dl=0"
    direct = dropbox_direct_url(url)
    assert direct == "https://dl.dropboxusercontent.com/scl/fi/abc123/movie.mp4?rlkey=key123&dl=1"
    assert dropbox_direct_url("https://www.dropbox.com/s/xyz/file.zip?dl=0") == (
        "https://dl.dropboxusercontent.com/s/xyz/file.zip?dl=1"
    )
    assert dropbox_direct_url("https://www.dropbox.com/sh/folder") is None
    assert dropbox_direct_url("https://example.com/s/x") is None


def test_gdrive_id_extraction():
    assert extract_gdrive_id("https://drive.google.com/file/d/1AbCdEfGhIjK/view?usp=sharing") == "1AbCdEfGhIjK"
    assert extract_gdrive_id("https://drive.google.com/open?id=1ZzYyXxWwVv") == "1ZzYyXxWwVv"
    assert extract_gdrive_id("https://example.com/file/d/1AbCdEfGhIjK") is None


def test_terabox_template_and_id():
    assert extract_terabox_id("https://www.terabox.com/s/1abcDEF") == "1abcDEF"
    assert extract_terabox_id("https://www.terabox.com/sharing/link?surl=xyz789") == "xyz789"
    resolved = asyncio.run(
        _resolve_terabox(
            "https://www.terabox.com/s/1abcDEF",
            {"direct_url_template": "https://d.example/file/{id}"},
            None,
        )
    )
    assert resolved.url == "https://d.example/file/1abcDEF"
    assert resolved.provider == "terabox"


def test_ytdlp_bridge_parses_direct_url(monkeypatch):
    class FakeProcess:
        returncode = 0

        async def communicate(self):
            payload = {
                "url": "https://cdn.example/video.mp4",
                "http_headers": {"Referer": "https://example.com", "User-Agent": "yt"},
            }
            return json.dumps(payload).encode(), b""

    async def fake_exec(*args, **kwargs):
        assert args[0] == "yt-dlp"
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    resolved = asyncio.run(resolve_with_ytdlp("https://example.com/watch/1"))
    assert resolved.url == "https://cdn.example/video.mp4"
    assert resolved.headers["Referer"] == "https://example.com"
    assert resolved.provider == "ytdlp"


def test_ytdlp_missing_binary(monkeypatch):
    async def fake_exec(*args, **kwargs):
        raise FileNotFoundError("yt-dlp")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    with pytest.raises(Exception) as excinfo:
        asyncio.run(resolve_with_ytdlp("https://example.com/x"))
    assert "yt-dlp" in str(excinfo.value)


def test_remote_cache_roundtrip():
    resolved = Resolved(
        url="https://cdn.example/file.mp4?sig=1",
        headers={"Referer": "https://example.com"},
        provider="example",
        public=True,
        size=1234,
        content_type="video/mp4",
    )
    cache_put("https://example.com/share", {"access_token": "token"}, resolved, ttl=120)
    cached = cache_get("https://example.com/share", {"access_token": "token"})
    assert cached is not None
    assert cached.url == resolved.url
    assert cached.headers == resolved.headers
    assert cached.size == 1234
    assert cache_get("https://example.com/share", {"access_token": "other"}) is None
    db.execute("DELETE FROM remote_cache")


def test_secret_key_is_stable():
    assert settings.secret == settings.secret
    assert len(settings.secret) >= 16
