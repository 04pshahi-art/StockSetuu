#!/usr/bin/env python
"""Admin CLI — the only way accounts are created, because the app has no sign-up page.

Run from the project folder with the virtualenv active:

    python manage.py init-db
    python manage.py create-user owner --name "Your Name"
    python manage.py set-password owner
    python manage.py seed-shop
    python manage.py backup
    python manage.py status

Every command reads .env for DB_KEY and DB_PATH, exactly like the server does, so the CLI
and the app always talk to the same database with the same key.
"""

from __future__ import annotations

import argparse
import datetime as dt
import getpass
import os
import shutil
import sys
from pathlib import Path

# Importing app.config loads .env before anything touches the database.
from app import db, migrations, repo, security
from app.config import settings

EXIT_OK = 0
EXIT_FAIL = 1

# Details from the GST registration certificate. Nothing here is real — these are
# placeholders so a fresh clone runs immediately. Used only by seed-shop, only when the
# field is still blank, and every one of them is editable later at /settings.
SHOP_SEED = {
    "legal_name": "YOUR LEGAL NAME AS ON THE GST CERTIFICATE",
    "trade_name": "YOUR SHOP NAME",
    "gstin": "",
    "registration_type": "Regular",
    "state_code": "27",
    "invoice_prefix": "INV",
    "invoice_terms": (
        "Goods once sold are not returnable. Warranty as per brand terms and conditions. "
        "Interest at 18% per annum on overdue payments. Subject to your state's jurisdiction."
    ),
}


def _out(message: str = "") -> None:
    print(message)


def _fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return EXIT_FAIL


# -- storage -----------------------------------------------------------------


def _open_db() -> int:
    """Open the database the same way the server does, with the same refusals."""
    try:
        db.preflight()
    except db.DatabaseNotConfigured as exc:
        return _fail(str(exc))
    try:
        db.get_connection()
    except db.DatabaseNotConfigured as exc:
        return _fail(str(exc))
    return EXIT_OK


def cmd_init_db(args: argparse.Namespace) -> int:
    if (rc := _open_db()) != EXIT_OK:
        return rc
    applied = migrations.migrate()
    status = db.encryption_status()
    _out(f"Database: {settings.db_path}")
    if applied:
        _out(f"Applied migration{'' if len(applied) == 1 else 's'}: {', '.join(map(str, applied))}")
    else:
        _out("Already up to date.")
    _out(f"Storage:  {status['detail']}")
    if not status["encrypted"]:
        _out()
        _out("WARNING: this database is NOT encrypted. On the shop server, install")
        _out("         sqlcipher3-wheels and set DB_KEY before entering real data.")
    users = int(db.scalar("SELECT count(*) FROM users", default=0))
    if not users:
        _out()
        _out("No accounts yet. Create the owner's login with:")
        _out("    python manage.py create-user owner")
    return EXIT_OK


def cmd_status(args: argparse.Namespace) -> int:
    if (rc := _open_db()) != EXIT_OK:
        return rc
    conn = db.get_connection()
    status = db.encryption_status()
    shop = repo.get_shop_settings()
    _out(f"Database file    {settings.db_path}")
    size = settings.db_path.stat().st_size if settings.db_path.exists() else 0
    _out(f"Size             {size / 1024:.0f} KiB")
    _out(
        f"Schema version   {migrations.current_version(conn)} "
        f"(code expects {migrations.LATEST_VERSION})"
    )
    _out(f"Encryption       {status['detail']}")
    _out(f"Password hashing {security.preferred_backend()}")
    _out(f"Backups          {settings.backup_dir}")
    _out(f"Shop             {shop.get('trade_name') or '(not set)'} · GSTIN {shop.get('gstin') or '(not set)'}")
    _out()
    counts = [
        ("users", "SELECT count(*) FROM users"),
        ("products", "SELECT count(*) FROM products WHERE is_active = 1"),
        ("dealers", "SELECT count(*) FROM dealers"),
        ("purchases", "SELECT count(*) FROM purchases"),
        ("invoices", "SELECT count(*) FROM sales"),
        ("service jobs", "SELECT count(*) FROM service_jobs"),
        ("serials", "SELECT count(*) FROM serials"),
    ]
    for label, sql in counts:
        _out(f"{label:>14}   {int(db.scalar(sql, default=0))}")
    return EXIT_OK


# -- accounts ----------------------------------------------------------------


def _read_new_password(username: str) -> str | None:
    """Prompt twice, off the echo, and enforce the same rules as the web form."""
    for _ in range(3):
        first = getpass.getpass(f"New password for {username}: ")
        second = getpass.getpass("Repeat: ")
        problems = security.password_problems(first, second)
        if not problems:
            return first
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
    return None


def cmd_create_user(args: argparse.Namespace) -> int:
    if (rc := _open_db()) != EXIT_OK:
        return rc
    migrations.migrate()
    username = args.username.strip()
    if not username:
        return _fail("username cannot be blank")

    existing = db.query_one("SELECT id FROM users WHERE username = ?", (username,))
    if existing is not None:
        return _fail(f"'{username}' already exists — use set-password to change its password")

    password = args.password or _read_new_password(username)
    if password is None:
        return _fail("password not set")
    problems = security.password_problems(password)
    if problems:
        return _fail("; ".join(problems))

    with db.transaction():
        user_id = db.insert(
            "INSERT INTO users (username, display_name, password_hash) VALUES (?, ?, ?)",
            (username, args.name or username, security.hash_password(password)),
        )
        repo.audit("user.create", entity="user", entity_id=user_id, detail=username)
    _out(f"Created '{username}' (id {user_id}), password hashed with {security.preferred_backend()}.")
    _out("There is no sign-up page — this CLI is the only way an account appears.")
    return EXIT_OK


def cmd_set_password(args: argparse.Namespace) -> int:
    if (rc := _open_db()) != EXIT_OK:
        return rc
    username = args.username.strip()
    row = db.query_one("SELECT id, username FROM users WHERE username = ?", (username,))
    if row is None:
        return _fail(f"no such user: {username}")

    password = args.password or _read_new_password(username)
    if password is None:
        return _fail("password not changed")
    problems = security.password_problems(password)
    if problems:
        return _fail("; ".join(problems))

    with db.transaction():
        # Bumping session_epoch invalidates every cookie already issued to this user,
        # which is the point of resetting a password you think someone else has seen.
        db.execute(
            """
            UPDATE users
               SET password_hash = ?, session_epoch = session_epoch + 1,
                   failed_attempts = 0, locked_until = NULL
             WHERE id = ?
            """,
            (security.hash_password(password), int(row["id"])),
        )
        repo.audit(
            "auth.password_reset", entity="user", entity_id=int(row["id"]), detail="via manage.py"
        )
    _out(f"Password changed for '{row['username']}'. Every signed-in device was signed out.")
    return EXIT_OK


def cmd_users(args: argparse.Namespace) -> int:
    if (rc := _open_db()) != EXIT_OK:
        return rc
    rows = db.query("SELECT * FROM users ORDER BY id")
    if not rows:
        _out("No accounts. Create one with: python manage.py create-user owner")
        return EXIT_OK
    _out(f"{'id':>3}  {'username':<16} {'name':<24} {'active':<7} {'locked until':<20} last sign-in")
    for row in rows:
        _out(
            f"{row['id']:>3}  {row['username']:<16} {(row['display_name'] or ''):<24} "
            f"{('yes' if row['is_active'] else 'NO'):<7} "
            f"{(row['locked_until'] or '-'):<20} {row['last_login_at'] or 'never'}"
        )
    return EXIT_OK


def cmd_unlock(args: argparse.Namespace) -> int:
    if (rc := _open_db()) != EXIT_OK:
        return rc
    with db.transaction():
        cursor = db.execute(
            "UPDATE users SET failed_attempts = 0, locked_until = NULL WHERE username = ?",
            (args.username.strip(),),
        )
    if not cursor.rowcount:
        return _fail(f"no such user: {args.username}")
    _out(f"'{args.username}' unlocked.")
    return EXIT_OK


# -- shop details ------------------------------------------------------------


def cmd_seed_shop(args: argparse.Namespace) -> int:
    if (rc := _open_db()) != EXIT_OK:
        return rc
    migrations.migrate()
    current = repo.get_shop_settings()
    changes = {
        field: value
        for field, value in SHOP_SEED.items()
        if not str(current.get(field) or "").strip() or args.force
    }
    if not changes:
        _out("Shop details are already filled in. Nothing changed.")
        _out("Edit them at /settings, or re-run with --force to overwrite from the certificate.")
        return EXIT_OK
    with db.transaction():
        repo.update_shop_settings(changes)
        repo.audit("settings.seed", entity="shop_settings", entity_id=1, detail=",".join(changes))
    for field, value in changes.items():
        _out(f"  {field:<18} {value}")
    _out()
    _out("Address, phone, email and bank details are deliberately not seeded — fill them in")
    _out("at /settings exactly as they appear on the GST certificate, because they print on")
    _out("every invoice.")
    return EXIT_OK


# -- backup ------------------------------------------------------------------


def cmd_backup(args: argparse.Namespace) -> int:
    """Copy the database with SQLite's own backup API, safe to run while serving."""
    if (rc := _open_db()) != EXIT_OK:
        return rc
    if not settings.db_path.exists():
        return _fail(f"no database at {settings.db_path} — run init-db first")

    settings.ensure_dirs()
    target_dir = Path(args.into).resolve() if args.into else settings.backup_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    target = target_dir / f"{settings.db_path.stem}-{stamp}.db"

    source = db.get_connection()
    try:
        # A live copy through the driver, so WAL contents are included and the copy is
        # consistent even if a sale is being saved at that moment. The backup keeps the
        # source's encryption, so it needs the same DB_KEY to open.
        destination = db.connect_to(target)
        try:
            source.backup(destination)
        finally:
            destination.close()
    except (AttributeError, *db.DB_ERRORS) as exc:
        # Some SQLCipher builds do not expose .backup(); a file copy is the fallback and
        # is safe when nothing is being written.
        _out(f"Live backup unavailable ({exc}); falling back to a file copy.")
        shutil.copy2(settings.db_path, target)

    size = target.stat().st_size
    _out(f"Backed up to {target} ({size / 1024:.0f} KiB)")
    if db.encryption_status()["encrypted"]:
        _out("The copy is encrypted with the same DB_KEY. Keep the key somewhere separate")
        _out("from the backups, or neither is any use.")
    else:
        _out("WARNING: this copy is NOT encrypted. Anyone who reads the file reads the shop.")

    if args.keep > 0:
        existing = sorted(target_dir.glob(f"{settings.db_path.stem}-*.db"))
        for stale in existing[: max(0, len(existing) - args.keep)]:
            try:
                stale.unlink()
                _out(f"Removed old backup {stale.name}")
            except OSError as exc:
                _out(f"Could not remove {stale.name}: {exc}")
    return EXIT_OK


# -- entry point -------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="manage.py",
        description="Admin tasks for the shop app. Accounts can only be created here.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="create or migrate the database").set_defaults(func=cmd_init_db)
    sub.add_parser("status", help="what is where, and whether it is encrypted").set_defaults(
        func=cmd_status
    )
    sub.add_parser("users", help="list accounts").set_defaults(func=cmd_users)

    create = sub.add_parser("create-user", help="create a login")
    create.add_argument("username")
    create.add_argument("--name", default="", help="display name shown in the app")
    create.add_argument(
        "--password",
        default="",
        help="skip the prompt (avoid: it lands in your shell history)",
    )
    create.set_defaults(func=cmd_create_user)

    reset = sub.add_parser("set-password", help="change a password and sign out every device")
    reset.add_argument("username")
    reset.add_argument("--password", default="", help="skip the prompt (avoid)")
    reset.set_defaults(func=cmd_set_password)

    unlock = sub.add_parser("unlock", help="clear a lockout after too many failed sign-ins")
    unlock.add_argument("username")
    unlock.set_defaults(func=cmd_unlock)

    seed = sub.add_parser("seed-shop", help="fill in the registered business details")
    seed.add_argument(
        "--force", action="store_true", help="overwrite fields that already have a value"
    )
    seed.set_defaults(func=cmd_seed_shop)

    backup = sub.add_parser("backup", help="copy the encrypted database somewhere safe")
    backup.add_argument("--into", default="", help="target folder (default: BACKUP_DIR)")
    backup.add_argument(
        "--keep", type=int, default=30, help="how many backups to keep in that folder (0 = all)"
    )
    backup.set_defaults(func=cmd_backup)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return EXIT_FAIL
    finally:
        db.close_thread_connection()


if __name__ == "__main__":
    os.chdir(Path(__file__).resolve().parent)
    raise SystemExit(main())
