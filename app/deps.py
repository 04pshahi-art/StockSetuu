"""Template environment, rendering helper and the auth dependency."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import HTTPException, Request
from fastapi.templating import Jinja2Templates
from starlette.responses import RedirectResponse

from . import gst, money, repo
from .session import SessionUser

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
templates.env.trim_blocks = True
templates.env.lstrip_blocks = True

templates.env.filters["money"] = money.fmt_money
templates.env.filters["rate"] = money.fmt_rate
templates.env.filters["rupees"] = money.rupees
templates.env.filters["words"] = money.amount_in_words
templates.env.filters["state"] = gst.state_label


def _plus_gst(amount_paise: int, rate_bp: int) -> int:
    """Gross-up for display only, rounded the same way the ledger rounds."""
    return int(amount_paise) + money.mul_div_round(int(amount_paise), int(rate_bp), 10_000)


templates.env.filters["plus_gst"] = _plus_gst

STATIC_DIR = Path(__file__).resolve().parent / "static"


def _asset_version() -> str:
    """Cache-buster for the stylesheet and script.

    The shop is updated by copying files onto the server, and browsers happily keep a
    stale ``/static/app.js`` for the rest of the session. That is how a fixed bug comes
    back to the counter, so the URL carries the newest mtime of the static folder and
    changes the moment anything in it is edited.
    """
    try:
        newest = max(p.stat().st_mtime for p in STATIC_DIR.iterdir() if p.is_file())
    except (OSError, ValueError):
        return "0"
    return str(int(newest))


templates.env.globals.update(
    {
        "STATES": gst.INDIAN_STATES,
        "GST_SLABS_BP": money.GST_SLABS_BP,
        "SLAB_LABELS": money.SLAB_LABELS,
        "PAYMENT_MODES": ("Cash", "UPI", "Card", "Bank Transfer", "Cheque", "Credit"),
        "asset_version": _asset_version,
    }
)


def human_date(value: str | dt.date | None) -> str:
    """ISO date -> ``28 Aug 2026``. Anything unparseable is passed through."""
    if not value:
        return "—"
    if isinstance(value, dt.date):
        parsed = value
    else:
        text = str(value)[:10]
        try:
            parsed = dt.date.fromisoformat(text)
        except ValueError:
            return str(value)
    return parsed.strftime("%d %b %Y")


def human_datetime(value: str | None) -> str:
    if not value:
        return "—"
    text = str(value).replace("T", " ")
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return text
    return parsed.strftime("%d %b %Y, %I:%M %p")


templates.env.filters["date"] = human_date
templates.env.filters["datetime"] = human_datetime


def today_iso() -> str:
    return dt.date.today().isoformat()


def current_user(request: Request) -> SessionUser | None:
    return getattr(request.state, "user", None)


def require_user(request: Request) -> SessionUser:
    """Dependency for routes that must have a signed-in user.

    The session middleware already redirects anonymous browsers, so reaching this without
    a user means a misconfiguration rather than a normal logged-out visit.
    """
    user = current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def render(request: Request, template: str, /, status_code: int = 200, **context: Any):
    """Render a template with the shared context every page needs.

    The shop/storage lookups are guarded because this same helper renders the error page,
    and the most likely reason for an error page is that the database is unreachable.
    """
    try:
        shop = repo.get_shop_settings()
        db_status = repo.storage_status()
    except Exception:  # noqa: BLE001 - an error page must still render
        shop, db_status = {}, {}
    base: dict[str, Any] = {
        "request": request,
        "user": current_user(request),
        "csrf_token": getattr(request.state, "csrf_token", ""),
        "shop": shop,
        "today": today_iso(),
        "path": request.url.path,
        "flash_ok": request.query_params.get("ok", ""),
        "flash_error": request.query_params.get("error", ""),
        "db_status": db_status,
    }
    base.update(context)
    return templates.TemplateResponse(request, template, base, status_code=status_code)


def redirect(path: str, *, ok: str = "", error: str = "") -> RedirectResponse:
    """303 redirect carrying a one-off status message in the query string."""
    params = []
    if ok:
        params.append("ok=" + quote(ok))
    if error:
        params.append("error=" + quote(error))
    if params:
        path += ("&" if "?" in path else "?") + "&".join(params)
    return RedirectResponse(path, status_code=303)
