"""Runtime configuration for Cascade."""

from __future__ import annotations

import os
import secrets
import threading
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = BASE_DIR / "data"


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader so `python -m cascade` needs no extra package."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        entry = line.strip()
        if not entry or entry.startswith("#") or "=" not in entry:
            continue
        key, value = entry.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv(BASE_DIR / ".env")


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class Settings:
    """Environment driven settings. One instance is shared by the whole app."""

    def __init__(self) -> None:
        self.data_dir = Path(os.environ.get("CASCADE_DATA_DIR", str(DEFAULT_DATA_DIR))).expanduser()
        self.host = os.environ.get("CASCADE_HOST", "0.0.0.0")
        try:
            self.port = int(os.environ.get("CASCADE_PORT", "8000"))
        except ValueError:
            self.port = 8000
        self.admin_email = (os.environ.get("CASCADE_ADMIN_EMAIL") or "").strip() or None
        self.admin_password = os.environ.get("CASCADE_ADMIN_PASSWORD") or None
        self.trust_proxy = _env_bool("CASCADE_TRUST_PROXY", False)
        self.cookie_secure = _env_bool("CASCADE_COOKIE_SECURE", False)
        self.ytdlp_path = os.environ.get("CASCADE_YTDLP", "yt-dlp")
        self.base_dir = BASE_DIR
        self.package_dir = Path(__file__).resolve().parent
        self.static_dir = self.package_dir / "static"
        self.template_dir = self.package_dir / "templates"
        self._secret: str | None = None
        self._secret_lock = threading.Lock()

    @property
    def db_path(self) -> Path:
        return self.data_dir / "cascade.db"

    @property
    def files_dir(self) -> Path:
        return self.data_dir / "files"

    def ensure(self) -> None:
        """Create runtime directories. Safe to call repeatedly."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.files_dir.mkdir(parents=True, exist_ok=True)

    @property
    def secret(self) -> str:
        """Signing key: CASCADE_SECRET, or a generated file kept in the data dir."""
        if self._secret:
            return self._secret
        with self._secret_lock:
            if self._secret:
                return self._secret
            env_secret = os.environ.get("CASCADE_SECRET")
            if env_secret:
                self._secret = env_secret
                return env_secret
            self.ensure()
            key_file = self.data_dir / "secret.key"
            if key_file.exists():
                value = key_file.read_text(encoding="utf-8").strip()
                if not value:
                    value = secrets.token_urlsafe(48)
                    key_file.write_text(value, encoding="utf-8")
            else:
                value = secrets.token_urlsafe(48)
                key_file.write_text(value, encoding="utf-8")
                try:
                    os.chmod(key_file, 0o600)
                except OSError:
                    pass
            self._secret = value
            return value


settings = Settings()
