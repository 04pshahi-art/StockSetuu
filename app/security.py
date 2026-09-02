"""Password hashing, signed session cookies and CSRF tokens.

Password hashes are stored in a self-describing ``algo$params$salt$digest`` format so the
scheme can change later without invalidating existing accounts. argon2id and bcrypt are
preferred when installed; otherwise the app uses ``hashlib.scrypt`` from the standard
library, which is memory-hard and needs no third-party wheel on Windows.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

# -- optional stronger backends ---------------------------------------------

try:  # pragma: no cover - presence depends on the host
    from argon2 import PasswordHasher as _Argon2Hasher
    from argon2.exceptions import VerifyMismatchError as _Argon2Mismatch

    _argon2 = _Argon2Hasher()
except Exception:  # noqa: BLE001
    _argon2 = None
    _Argon2Mismatch = Exception  # type: ignore[assignment]

try:  # pragma: no cover
    import bcrypt as _bcrypt
except Exception:  # noqa: BLE001
    _bcrypt = None

# scrypt cost parameters: 128 * N * r = 32 MiB of memory per hash, which is plenty for a
# login form and still instant on a small server.
_SCRYPT_N = 2**15
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32

# OpenSSL caps scrypt's memory at 32 MiB unless told otherwise, and the parameters above
# need that exactly — leaving this at the default makes every hash raise
# "memory limit exceeded". Give it room for the working buffers on top.
_SCRYPT_MAXMEM = 128 * _SCRYPT_N * _SCRYPT_R * 2


def _scrypt_maxmem(n: int, r: int, p: int) -> int:
    """Memory ceiling for a given cost, with headroom, capped so a corrupt stored hash
    cannot ask the process to allocate something absurd."""
    return min(max(128 * n * r * 2, _SCRYPT_MAXMEM), 512 * 1024 * 1024)

MIN_PASSWORD_LENGTH = 10


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def preferred_backend() -> str:
    if _argon2 is not None:
        return "argon2id"
    if _bcrypt is not None:
        return "bcrypt"
    return "scrypt"


def hash_password(password: str) -> str:
    """Hash a password with the strongest backend available on this machine."""
    if not password:
        raise ValueError("password cannot be empty")
    backend = preferred_backend()
    if backend == "argon2id":
        return "argon2$" + _argon2.hash(password)  # type: ignore[union-attr]
    if backend == "bcrypt":
        digest = _bcrypt.hashpw(password.encode("utf-8"), _bcrypt.gensalt(rounds=12))  # type: ignore[union-attr]
        return "bcrypt$" + digest.decode("ascii")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
        maxmem=_SCRYPT_MAXMEM,
    )
    return f"scrypt${_SCRYPT_N}:{_SCRYPT_R}:{_SCRYPT_P}${_b64e(salt)}${_b64e(digest)}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time verification against any of the supported hash formats."""
    if not password or not stored or "$" not in stored:
        return False
    algo, _, rest = stored.partition("$")
    try:
        if algo == "argon2":
            if _argon2 is None:
                return False
            try:
                return bool(_argon2.verify(rest, password))
            except _Argon2Mismatch:
                return False
        if algo == "bcrypt":
            if _bcrypt is None:
                return False
            return bool(_bcrypt.checkpw(password.encode("utf-8"), rest.encode("ascii")))
        if algo == "scrypt":
            params, salt_b64, digest_b64 = rest.split("$")
            n_str, r_str, p_str = params.split(":")
            n, r, p = int(n_str), int(r_str), int(p_str)
            computed = hashlib.scrypt(
                password.encode("utf-8"),
                salt=_b64d(salt_b64),
                n=n,
                r=r,
                p=p,
                dklen=len(_b64d(digest_b64)),
                maxmem=_scrypt_maxmem(n, r, p),
            )
            return hmac.compare_digest(computed, _b64d(digest_b64))
    except Exception:  # noqa: BLE001 - a malformed hash is simply a failed login
        return False
    return False


def password_problems(password: str, confirm: str | None = None) -> list[str]:
    """Human-readable strength complaints, or an empty list when acceptable."""
    problems: list[str] = []
    if confirm is not None and password != confirm:
        problems.append("The two passwords do not match.")
    if len(password) < MIN_PASSWORD_LENGTH:
        problems.append(f"Use at least {MIN_PASSWORD_LENGTH} characters.")
    lowered = password.lower()
    if lowered in {"password", "12345678901", "adminadmin", "changeme123"}:
        problems.append("That password is too easy to guess.")
    if password and password.isdigit():
        problems.append("Use more than only digits.")
    return problems


# -- signed payloads (session cookie) ---------------------------------------


class BadSignature(Exception):
    """Cookie was missing, tampered with, or expired."""


def sign(payload: dict[str, Any], secret: str) -> str:
    """Serialise and HMAC-sign a small dict into a cookie-safe string."""
    body = _b64e(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    mac = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{_b64e(mac)}"


def unsign(token: str, secret: str) -> dict[str, Any]:
    """Verify and decode a token produced by :func:`sign`."""
    if not token or "." not in token:
        raise BadSignature("malformed token")
    body, _, mac_b64 = token.rpartition(".")
    expected = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    try:
        provided = _b64d(mac_b64)
    except Exception as exc:  # noqa: BLE001
        raise BadSignature("malformed signature") from exc
    if not hmac.compare_digest(expected, provided):
        raise BadSignature("signature mismatch")
    try:
        data = json.loads(_b64d(body))
    except Exception as exc:  # noqa: BLE001
        raise BadSignature("malformed payload") from exc
    if not isinstance(data, dict):
        raise BadSignature("payload is not an object")
    return data


def now_ts() -> int:
    return int(time.time())


# -- CSRF --------------------------------------------------------------------


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def csrf_matches(expected: str | None, provided: str | None) -> bool:
    if not expected or not provided:
        return False
    return hmac.compare_digest(expected, provided)
