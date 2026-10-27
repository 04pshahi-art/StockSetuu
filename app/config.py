"""Runtime configuration, read from environment (optionally seeded from a .env file)."""

from __future__ import annotations

import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader. Real environment variables always win."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv(BASE_DIR / ".env")


def _bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


class Settings:
    """Process-wide settings.

    ``DB_KEY`` is the SQLCipher passphrase. It is the only thing standing between a
    stolen ``.db`` file and the shop's data, so the app refuses to start without it
    unless encryption has been explicitly disabled.
    """

    def __init__(self) -> None:
        self.data_dir = Path(os.environ.get("DATA_DIR") or (BASE_DIR / "data")).resolve()
        self.db_path = Path(os.environ.get("DB_PATH") or (self.data_dir / "shop.db")).resolve()
        self.backup_dir = Path(os.environ.get("BACKUP_DIR") or (self.data_dir / "backups")).resolve()

        self.db_key = os.environ.get("DB_KEY", "").strip()
        # Escape hatch for local development on a machine where SQLCipher cannot be
        # installed. Never set this on the shop server.
        self.allow_unencrypted_db = _bool("ALLOW_UNENCRYPTED_DB", False)

        self.secret_key = os.environ.get("SECRET_KEY", "").strip()
        self.session_cookie = os.environ.get("SESSION_COOKIE", "pcs_session")
        self.session_idle_minutes = _int("SESSION_IDLE_MINUTES", 60)
        self.session_absolute_hours = _int("SESSION_ABSOLUTE_HOURS", 24 * 7)
        self.cookie_secure = _bool("COOKIE_SECURE", False)  # plain HTTP over Tailscale

        self.host = os.environ.get("HOST", "0.0.0.0")
        self.port = _int("PORT", 8000)
        self.debug = _bool("DEBUG", False)

        self.login_max_attempts = _int("LOGIN_MAX_ATTEMPTS", 8)
        self.login_lockout_minutes = _int("LOGIN_LOCKOUT_MINUTES", 15)

    # -- derived / validated ------------------------------------------------

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.backup_dir.mkdir(parents=True, exist_ok=True)

    def resolve_secret_key(self) -> str:
        """Return the cookie-signing key, persisting a generated one if absent.

        A rotated key logs everyone out, which is safe but annoying, so the generated
        key is written to ``data/secret_key`` rather than regenerated per boot.
        """
        if self.secret_key:
            return self.secret_key
        self.ensure_dirs()
        key_file = self.data_dir / "secret_key"
        if key_file.is_file():
            existing = key_file.read_text(encoding="utf-8").strip()
            if existing:
                return existing
        generated = secrets.token_urlsafe(48)
        key_file.write_text(generated, encoding="utf-8")
        try:
            key_file.chmod(0o600)
        except OSError:
            pass  # Windows ACLs; not fatal
        return generated


settings = Settings()
