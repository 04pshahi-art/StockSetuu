"""Login, logout and password change."""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Form, Request
from starlette.responses import RedirectResponse

from .. import db, repo, security, session
from ..config import settings
from ..deps import redirect, render, require_user

router = APIRouter()


def _lockout_remaining(locked_until: str | None) -> int:
    """Minutes left on a login lockout, or 0 when not locked."""
    if not locked_until:
        return 0
    try:
        until = dt.datetime.fromisoformat(locked_until)
    except ValueError:
        return 0
    remaining = (until - dt.datetime.now()).total_seconds()
    return max(0, int(remaining // 60) + 1) if remaining > 0 else 0


@router.get("/login")
def login_form(request: Request):
    if getattr(request.state, "user", None) is not None:
        return RedirectResponse("/", status_code=303)
    has_users = int(db.scalar("SELECT count(*) FROM users", default=0)) > 0
    return render(
        request,
        "login.html",
        next_url=request.query_params.get("next", "/"),
        timed_out=request.query_params.get("timeout") == "1",
        has_users=has_users,
        error="",
    )


@router.post("/login")
def login_submit(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    next_url: str = Form("/"),
):
    username = username.strip()
    row = db.query_one(
        "SELECT id, username, display_name, password_hash, is_active, failed_attempts, locked_until "
        "FROM users WHERE username = ? COLLATE NOCASE",
        (username,),
    )

    def failure(message: str):
        return render(
            request,
            "login.html",
            next_url=next_url,
            timed_out=False,
            has_users=True,
            error=message,
            username=username,
        )

    if row is None or not row["is_active"]:
        # Same message either way so an attacker cannot enumerate usernames.
        return failure("Wrong username or password.")

    remaining = _lockout_remaining(row["locked_until"])
    if remaining:
        return failure(f"Too many failed attempts. Try again in {remaining} minute(s).")

    if not security.verify_password(password, row["password_hash"]):
        attempts = int(row["failed_attempts"]) + 1
        locked_until = None
        if attempts >= settings.login_max_attempts:
            locked_until = (
                dt.datetime.now() + dt.timedelta(minutes=settings.login_lockout_minutes)
            ).isoformat(timespec="seconds")
            attempts = 0
        with db.transaction():
            db.execute(
                "UPDATE users SET failed_attempts = ?, locked_until = ? WHERE id = ?",
                (attempts, locked_until, int(row["id"])),
            )
        if locked_until:
            return failure(
                f"Too many failed attempts. Locked for {settings.login_lockout_minutes} minutes."
            )
        left = settings.login_max_attempts - attempts
        return failure(f"Wrong username or password. {left} attempt(s) left before lockout.")

    with db.transaction():
        db.execute(
            "UPDATE users SET failed_attempts = 0, locked_until = NULL, "
            "last_login_at = datetime('now', 'localtime') WHERE id = ?",
            (int(row["id"]),),
        )
        repo.audit("auth.login", entity="user", entity_id=int(row["id"]), user_id=int(row["id"]))

    target = next_url if next_url.startswith("/") and not next_url.startswith("//") else "/"
    response = RedirectResponse(target, status_code=303)
    session.start_session(response, int(row["id"]), settings.resolve_secret_key())
    # The middleware would otherwise overwrite this fresh cookie using the stale
    # request-scoped token.
    request.state.session_ended = True
    return response


@router.post("/logout")
def logout(request: Request):
    user = getattr(request.state, "user", None)
    if user is not None:
        with db.transaction():
            repo.audit("auth.logout", entity="user", entity_id=user.id, user_id=user.id)
    response = RedirectResponse("/login?ok=Signed+out", status_code=303)
    session.end_session(response)
    request.state.session_ended = True
    return response


@router.get("/account")
def account(request: Request):
    user = require_user(request)
    row = db.query_one("SELECT * FROM users WHERE id = ?", (user.id,))
    return render(
        request,
        "account.html",
        account=row,
        hash_backend=security.preferred_backend(),
        idle_minutes=settings.session_idle_minutes,
    )


@router.post("/account/password")
def change_password(
    request: Request,
    current_password: str = Form(""),
    new_password: str = Form(""),
    confirm_password: str = Form(""),
):
    user = require_user(request)
    row = db.query_one("SELECT password_hash FROM users WHERE id = ?", (user.id,))
    if row is None or not security.verify_password(current_password, row["password_hash"]):
        return redirect("/account", error="Your current password is wrong.")

    problems = security.password_problems(new_password, confirm_password)
    if problems:
        return redirect("/account", error=" ".join(problems))

    with db.transaction():
        # Bumping session_epoch invalidates every cookie signed for this user, so a
        # password change also signs out any other device.
        db.execute(
            "UPDATE users SET password_hash = ?, session_epoch = session_epoch + 1 WHERE id = ?",
            (security.hash_password(new_password), user.id),
        )
        repo.audit("auth.password_change", entity="user", entity_id=user.id, user_id=user.id)

    response = RedirectResponse("/login?ok=Password+changed.+Please+sign+in+again.", status_code=303)
    session.end_session(response)
    request.state.session_ended = True
    return response


@router.post("/account/display-name")
def change_display_name(request: Request, display_name: str = Form("")):
    user = require_user(request)
    with db.transaction():
        db.execute("UPDATE users SET display_name = ? WHERE id = ?", (display_name.strip(), user.id))
    return redirect("/account", ok="Name updated.")
