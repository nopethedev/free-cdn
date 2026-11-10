"""Small, dependency-free helpers used across Cascade."""

from __future__ import annotations

import mimetypes
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

EXTRA_MIME = {
    ".mkv": "video/x-matroska",
    ".m4a": "audio/mp4",
    ".flac": "audio/flac",
    ".opus": "audio/opus",
    ".webm": "video/webm",
    ".avif": "image/avif",
    ".heic": "image/heic",
    ".7z": "application/x-7z-compressed",
    ".rar": "application/vnd.rar",
    ".zst": "application/zstd",
    ".parquet": "application/vnd.apache.parquet",
}


def now() -> int:
    return int(time.time())


def human_size(num: int | float | None) -> str:
    if num is None:
        return "—"
    try:
        value = float(num)
    except (TypeError, ValueError):
        return "—"
    if value < 0:
        return "—"
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    idx = 0
    while value >= 1024 and idx < len(units) - 1:
        value /= 1024.0
        idx += 1
    if idx == 0:
        return f"{int(value)} B"
    return f"{value:.1f} {units[idx]}".replace(".0 ", " ")


def fmt_time(ts: int | None) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def iso_time(ts: int | None) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()


def rel_time(ts: int | None) -> str:
    if not ts:
        return "never"
    delta = now() - int(ts)
    if delta < 0:
        future = -delta
        if future < 3600:
            return f"in {max(1, future // 60)}m"
        if future < 86400:
            return f"in {future // 3600}h"
        return f"in {future // 86400}d"
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    if delta < 86400 * 30:
        return f"{delta // 86400}d ago"
    if delta < 86400 * 365:
        return f"{delta // (86400 * 30)}mo ago"
    return f"{delta // (86400 * 365)}y ago"


def guess_mime(name: str) -> str:
    ext = os.path.splitext(name or "")[1].lower()
    if ext in EXTRA_MIME:
        return EXTRA_MIME[ext]
    guessed, _ = mimetypes.guess_type(name or "")
    return guessed or "application/octet-stream"


def normalize_path(path: str | None) -> str:
    """Normalise a virtual path to '/a/b' form, rejecting traversal."""
    raw = (path or "").replace("\\", "/")
    if "\x00" in raw:
        raise ValueError("invalid path")
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise ValueError("invalid path")
    return "/" + "/".join(parts)


def parent_path(path: str) -> str:
    path = normalize_path(path)
    if path == "/":
        return "/"
    parts = path.strip("/").split("/")[:-1]
    return "/" + "/".join(parts) if parts else "/"


def base_name(path: str) -> str:
    path = (path or "").rstrip("/")
    return path.rsplit("/", 1)[-1] if path else ""


def join_path(directory: str, name: str) -> str:
    directory = normalize_path(directory)
    name = (name or "").replace("\\", "/").strip("/")
    if not name:
        return directory
    return normalize_path(f"{directory}/{name}")


def safe_join(root: Path, rel: str) -> Path:
    """Resolve `rel` under `root`, refusing anything that escapes it."""
    raw = (rel or "").replace("\\", "/")
    if "\x00" in raw:
        raise ValueError("invalid path")
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise ValueError("invalid path")
    root_resolved = Path(root).resolve()
    target = root_resolved.joinpath(*parts)
    target_resolved = target.resolve(strict=False)
    if target_resolved != root_resolved and root_resolved not in target_resolved.parents:
        raise ValueError("path escapes storage root")
    return target_resolved


class RangeNotSatisfiable(Exception):
    def __init__(self, size: int) -> None:
        self.size = size
        super().__init__("range not satisfiable")


def parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """Parse a single byte range. Returns inclusive (start, end) or None.

    Multiple ranges are collapsed to the first one; a malformed header is
    ignored so the caller can serve the whole file.
    """
    if not header:
        return None
    value = header.strip()
    if not value.lower().startswith("bytes="):
        return None
    spec = value[6:].split(",")[0].strip()
    if "-" not in spec:
        return None
    start_raw, end_raw = spec.split("-", 1)
    try:
        if start_raw == "":
            suffix = int(end_raw)
            if suffix <= 0:
                return None
            start = max(0, size - suffix)
            end = size - 1
        else:
            start = int(start_raw)
            end = int(end_raw) if end_raw else size - 1
    except ValueError:
        return None
    if start < 0:
        return None
    if size == 0:
        raise RangeNotSatisfiable(0)
    if start >= size:
        raise RangeNotSatisfiable(size)
    end = min(end, size - 1)
    if end < start:
        return None
    return start, end


def split_name(path: str) -> tuple[str, str]:
    path = normalize_path(path)
    if path == "/":
        return "/", ""
    return parent_path(path), base_name(path)


def truncate(value: str | None, limit: int = 120) -> str:
    text = (value or "").replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def content_disposition(filename: str, inline: bool = True) -> str:
    """A header value that survives non-ASCII filenames."""
    name = (filename or "download").replace("\\", "_").replace('"', "'").replace("\r", "").replace("\n", "")
    ascii_name = re.sub(r"[^\x20-\x7e]", "_", name)[:180] or "download"
    encoded = quote(name, safe="")[:240]
    kind = "inline" if inline else "attachment"
    return f'{kind}; filename="{ascii_name}"; filename*=UTF-8\'\'{encoded}'
