"""Cookie session handling, inactivity logout and CSRF enforcement.

Implemented directly on top of an HMAC-signed cookie rather than a session store: with a
single user there is nothing to gain from server-side session rows, and revocation is
handled by bumping ``users.session_epoch``.
"""

from __future__ import annotations

from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from . import db, security
from .config import settings

SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}

# Paths reachable without a session. Everything else requires login.
PUBLIC_PATHS = {"/login", "/logout", "/healthz", "/favicon.ico"}
PUBLIC_PREFIXES = ("/static/",)

# Logging in and out are not state changes worth a CSRF token, and demanding one on
# /logout only produces a confusing error when the session has already timed out.
CSRF_EXEMPT_PATHS = {"/login", "/logout"}

# Upper bound on a request body we are willing to buffer in order to read its CSRF
# field. Comfortably above any Tally stock export; a larger body is rejected outright.
MAX_BUFFERED_BODY = 25 * 1024 * 1024


class SessionUser:
    """The signed-in user, attached to ``request.state.user``."""

    __slots__ = ("id", "username", "display_name")

    def __init__(self, user_id: int, username: str, display_name: str) -> None:
        self.id = user_id
        self.username = username
        self.display_name = display_name or username

    def __bool__(self) -> bool:  # pragma: no cover - convenience in templates
        return True


def _is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


def _wants_json(request: Request) -> bool:
    if request.url.path.startswith("/api/"):
        return True
    accept = request.headers.get("accept", "")
    return "application/json" in accept and "text/html" not in accept


class SessionMiddleware(BaseHTTPMiddleware):
    """Validates the session cookie, refreshes it, and blocks CSRF on writes."""

    def __init__(self, app: Any, secret: str) -> None:
        super().__init__(app)
        self.secret = secret

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        payload = self._read_cookie(request)
        user, expiry_reason = self._resolve_user(payload)

        request.state.user = user
        request.state.csrf_token = (payload or {}).get("csrf") or security.new_csrf_token()
        # Preserve the original issue time so the absolute lifetime actually expires
        # instead of sliding forward on every request along with `seen`.
        request.state.session_iat = int((payload or {}).get("iat") or security.now_ts())
        request.state.session_ended = False

        path = request.url.path
        if user is None and not _is_public(path):
            return self._redirect_to_login(request, expiry_reason)

        if request.method not in SAFE_METHODS and path not in CSRF_EXEMPT_PATHS:
            failure = await self._check_csrf(request)
            if failure is not None:
                return failure

        response = await call_next(request)

        if user is not None and not request.state.session_ended:
            self._write_cookie(response, user.id, request.state.session_iat, request.state.csrf_token)
        return response

    # -- helpers ------------------------------------------------------------

    def _redirect_to_login(self, request: Request, expiry_reason: str) -> Response:
        if _wants_json(request):
            return JSONResponse({"error": "not authenticated"}, status_code=401)
        target = "/login"
        params = []
        if request.method in SAFE_METHODS and request.url.path != "/":
            params.append("next=" + request.url.path)
        if expiry_reason == "idle":
            params.append("timeout=1")
        if params:
            target += "?" + "&".join(params)
        response = RedirectResponse(target, status_code=303)
        response.delete_cookie(settings.session_cookie, path="/")
        return response

    def _read_cookie(self, request: Request) -> dict[str, Any] | None:
        raw = request.cookies.get(settings.session_cookie)
        if not raw:
            return None
        try:
            return security.unsign(raw, self.secret)
        except security.BadSignature:
            return None

    def _resolve_user(self, payload: dict[str, Any] | None) -> tuple[SessionUser | None, str]:
        if not payload:
            return None, ""
        now = security.now_ts()
        try:
            user_id = int(payload["uid"])
            issued_at = int(payload["iat"])
            seen_at = int(payload["seen"])
            epoch = int(payload["epoch"])
        except (KeyError, TypeError, ValueError):
            return None, ""

        if now - seen_at > settings.session_idle_minutes * 60:
            return None, "idle"
        if now - issued_at > settings.session_absolute_hours * 3600:
            return None, "absolute"

        row = db.query_one(
            "SELECT id, username, display_name, is_active, session_epoch FROM users WHERE id = ?",
            (user_id,),
        )
        if row is None or not row["is_active"] or int(row["session_epoch"]) != epoch:
            return None, "revoked"
        return SessionUser(int(row["id"]), row["username"], row["display_name"]), ""

    def _write_cookie(self, response: Response, user_id: int, issued_at: int, csrf: str) -> None:
        epoch_row = db.query_one("SELECT session_epoch FROM users WHERE id = ?", (user_id,))
        payload = {
            "uid": user_id,
            "iat": issued_at,
            "seen": security.now_ts(),
            "epoch": int(epoch_row["session_epoch"]) if epoch_row else 1,
            "csrf": csrf,
        }
        _set_cookie(response, security.sign(payload, self.secret))

    async def _check_csrf(self, request: Request) -> Response | None:
        expected = getattr(request.state, "csrf_token", None)
        provided = request.headers.get("x-csrf-token")

        if not provided and _looks_like_form(request):
            try:
                length = int(request.headers.get("content-length") or 0)
            except ValueError:
                length = 0
            if length > MAX_BUFFERED_BODY:
                return _csrf_failure(request, "The uploaded file is too large.")
            # Buffer the body before parsing: that populates Request._body, which is what
            # lets Starlette replay the same bytes to the endpoint downstream. Parsing a
            # multipart stream without buffering first would leave the endpoint with
            # nothing to read.
            await request.body()
            try:
                form = await request.form()
            except Exception:  # noqa: BLE001 - unparseable body, treat as no token
                form = None
            if form is not None:
                value = form.get("csrf_token")
                provided = value if isinstance(value, str) else None

        if security.csrf_matches(expected, provided):
            return None
        return _csrf_failure(request, "Your session moved on — please try that again.")


def _looks_like_form(request: Request) -> bool:
    content_type = request.headers.get("content-type", "")
    return content_type.startswith(("application/x-www-form-urlencoded", "multipart/form-data"))


def _csrf_failure(request: Request, message: str) -> Response:
    if _wants_json(request):
        return JSONResponse({"error": message}, status_code=403)
    from urllib.parse import quote

    return RedirectResponse(f"/?error={quote(message)}", status_code=303)


def _set_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        settings.session_cookie,
        token,
        max_age=settings.session_idle_minutes * 60,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )


def start_session(response: Response, user_id: int, secret: str) -> str:
    """Issue a brand-new session cookie after a successful login."""
    now = security.now_ts()
    csrf = security.new_csrf_token()
    epoch_row = db.query_one("SELECT session_epoch FROM users WHERE id = ?", (user_id,))
    payload = {
        "uid": user_id,
        "iat": now,
        "seen": now,
        "epoch": int(epoch_row["session_epoch"]) if epoch_row else 1,
        "csrf": csrf,
    }
    _set_cookie(response, security.sign(payload, secret))
    return csrf


def end_session(response: Response) -> None:
    response.delete_cookie(settings.session_cookie, path="/")
