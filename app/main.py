"""Application assembly.

Run with:  uvicorn app.main:app --host 0.0.0.0 --port 8000
or simply: python run.py
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse

from . import db, migrations, repo, security
from .config import settings
from .deps import render
from .routers import (
    api,
    auth,
    dashboard,
    dealers,
    products,
    purchases,
    reports,
    sales,
    serials,
    services,
    shop,
    tally_import,
)
from .session import SessionMiddleware

log = logging.getLogger("pcs")

STATIC_DIR = Path(__file__).resolve().parent / "static"

BANNER = r"""
  SHOP MANAGEMENT SYSTEM — shop management
  ------------------------------------------------------------------
  Database : {db_path}
  Storage  : {storage}
  Schema   : v{version}
  Listening: http://{host}:{port}   (reach it over Tailscale)
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(
        level=logging.DEBUG if settings.debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
    )
    settings.ensure_dirs()

    # Refuse to start on an unencrypted database unless that was an explicit choice —
    # silently falling back would leave the shop's books readable to anyone with the file.
    db.preflight()

    applied = migrations.migrate()
    if applied:
        log.info("Applied migrations: %s", ", ".join(f"v{v}" for v in applied))

    status = repo.storage_status()
    print(
        BANNER.format(
            db_path=settings.db_path,
            storage=status.get("detail", ""),
            version=migrations.LATEST_VERSION,
            host=settings.host,
            port=settings.port,
        )
    )
    if not status.get("encrypted"):
        log.warning(
            "Database is NOT encrypted. Install sqlcipher3-wheels and set DB_KEY, "
            "then re-create the database to encrypt it."
        )

    if int(db.scalar("SELECT count(*) FROM users", default=0) or 0) == 0:
        log.warning(
            "No user accounts exist yet. Create one with:  python manage.py create-user"
        )
    log.info("Password hashing backend: %s", security.preferred_backend())

    try:
        yield
    finally:
        db.close_thread_connection()


app = FastAPI(
    title="Shop Management System",
    description="Inventory, GST billing, warranty and service management for a single shop.",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/api/docs" if settings.debug else None,
    redoc_url=None,
    openapi_url="/api/openapi.json" if settings.debug else None,
)

# Outermost: every request gets session resolution and CSRF enforcement.
# resolve_secret_key() is called here, at import time, so a generated key is written to
# data/secret_key once rather than a fresh one appearing per worker or per request.
app.add_middleware(SessionMiddleware, secret=settings.resolve_secret_key())

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

app.include_router(dashboard.router)
app.include_router(auth.router)
app.include_router(shop.router)
app.include_router(products.router)
app.include_router(dealers.router)
app.include_router(purchases.router)
app.include_router(sales.router)
app.include_router(serials.router)
app.include_router(services.router)
app.include_router(reports.router)
app.include_router(tally_import.router)
app.include_router(api.router)


def _wants_json(request: Request) -> bool:
    return request.url.path.startswith("/api/") or "application/json" in request.headers.get(
        "accept", ""
    )


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    if _wants_json(request):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
    if exc.status_code == 401:
        from .deps import redirect

        return redirect("/login", error="Please sign in to continue.")
    return render(
        request,
        "error.html",
        status_code=exc.status_code,
        message=str(exc.detail),
    )


@app.exception_handler(Exception)
async def unhandled_error(request: Request, exc: Exception):
    log.exception("Unhandled error on %s %s", request.method, request.url.path)
    if _wants_json(request):
        return JSONResponse({"error": "Internal error"}, status_code=500)
    return render(
        request,
        "error.html",
        status_code=500,
        message=(
            str(exc)
            if settings.debug
            else "Something went wrong. Nothing was saved. Check the server log for details."
        ),
    )


@app.exception_handler(db.DatabaseNotConfigured)
async def db_error(request: Request, exc: db.DatabaseNotConfigured):
    return JSONResponse({"error": str(exc)}, status_code=503)


@app.exception_handler(HTTPException)
async def fastapi_http_error(request: Request, exc: HTTPException):
    return await http_error(request, exc)
