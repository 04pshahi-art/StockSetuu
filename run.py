#!/usr/bin/env python
"""Start the server. The banner, migrations and the encryption check live in app.main.

    python run.py                 # read HOST/PORT from .env
    python run.py --port 8080
    python run.py --reload        # while developing only

This is what NSSM points at on the Windows Server, so the paths stay relative to this
file rather than to whatever folder the service happens to start in.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
os.chdir(BASE_DIR)
sys.path.insert(0, str(BASE_DIR))

from app import db  # noqa: E402  (after chdir, so .env is found)
from app.config import settings  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the shop management server.")
    parser.add_argument("--host", default=settings.host, help=f"default {settings.host}")
    parser.add_argument("--port", type=int, default=settings.port, help=f"default {settings.port}")
    parser.add_argument(
        "--reload", action="store_true", help="restart on code changes (development only)"
    )
    args = parser.parse_args()

    # Check the storage before uvicorn takes over the terminal, so a missing DB_KEY is a
    # one-line message rather than a traceback buried in the reloader's output.
    try:
        db.preflight()
    except db.DatabaseNotConfigured as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if not settings.db_path.exists():
        print(f"error: no database at {settings.db_path}", file=sys.stderr)
        print("Create it first:  python manage.py init-db", file=sys.stderr)
        return 1

    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        # One worker on purpose. SQLite is a single file and the invoice counter is
        # protected by a transaction on one connection per thread; extra worker
        # processes would buy nothing for one shop and complicate both.
        workers=1,
        access_log=settings.debug,
        log_level="debug" if settings.debug else "info",
        proxy_headers=False,
        server_header=False,
        date_header=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
