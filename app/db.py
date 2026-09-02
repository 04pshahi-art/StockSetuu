"""SQLite/SQLCipher connection handling.

The shop database is encrypted at rest with SQLCipher, so the raw ``.db`` file is
unreadable without ``DB_KEY``. SQLCipher ships as a drop-in DBAPI module; we probe for
the two common distributions and fall back to the stdlib driver only when the operator
has explicitly opted out of encryption for local development.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

from .config import settings

# -- driver selection --------------------------------------------------------

_driver: Any = None
_driver_name = ""

for _candidate in ("sqlcipher3", "pysqlcipher3.dbapi2", "sqlcipher3.dbapi2"):
    try:  # pragma: no cover - depends on what is installed on the host
        module = __import__(_candidate, fromlist=["dbapi2"])
        _driver = getattr(module, "dbapi2", module)
        if not hasattr(_driver, "connect"):
            continue
        _driver_name = _candidate
        break
    except ImportError:
        continue

if _driver is None:  # no SQLCipher on this machine
    _driver = sqlite3
    _driver_name = "sqlite3"

ENCRYPTION_AVAILABLE = _driver_name != "sqlite3"


# -- DBAPI types, taken from whichever driver actually loaded -----------------
#
# SQLCipher is not a plugin for the stdlib module; it is a separate C extension with its
# own Row, Cursor, Connection and exception classes. Naming the stdlib ones directly works
# right up until the day the real driver is installed, and then:
#
#   * ``conn.row_factory = sqlite3.Row`` raises
#     "TypeError: Row() argument 1 must be sqlite3.Cursor, not sqlcipher3.dbapi2.Cursor"
#     on the first query, because sqlite3.Row is a C type that type-checks its cursor.
#   * ``except sqlite3.Error`` never fires, because sqlcipher3's exceptions inherit from
#     its own Error, not the stdlib's. That failure is worse than the crash: the handler
#     silently stops handling.
#
# So every type the app touches is resolved through the driver here, once, and referenced
# from this module everywhere else. ``getattr`` with a stdlib fallback keeps this working
# for any of the three probed distributions; Row and Cursor are pysqlite extensions rather
# than PEP 249 requirements, so a driver is technically allowed not to expose them.
Connection = getattr(_driver, "Connection", sqlite3.Connection)
Cursor = getattr(_driver, "Cursor", sqlite3.Cursor)
Row = getattr(_driver, "Row", sqlite3.Row)

Error = getattr(_driver, "Error", sqlite3.Error)
IntegrityError = getattr(_driver, "IntegrityError", sqlite3.IntegrityError)
OperationalError = getattr(_driver, "OperationalError", sqlite3.OperationalError)

# For ``except`` clauses. A tuple rather than a single class so a handler stays correct
# even when a wrapper somewhere raises the stdlib class against a SQLCipher connection.
# Deduplicated via dict.fromkeys, which collapses to one entry on the stdlib driver and
# keeps the order stable.
DB_ERRORS: tuple[type[BaseException], ...] = tuple(dict.fromkeys((Error, sqlite3.Error)))


class DatabaseNotConfigured(RuntimeError):
    """Raised when the app is asked to open an unencrypted database by accident."""


def encryption_status() -> dict[str, object]:
    """Describe the storage layer, for the startup banner and the settings screen."""
    encrypted = ENCRYPTION_AVAILABLE and bool(settings.db_key)
    if encrypted:
        detail = f"Encrypted at rest via SQLCipher ({_driver_name})."
    elif not ENCRYPTION_AVAILABLE:
        detail = (
            "SQLCipher driver not installed — the database file is NOT encrypted. "
            "Install 'sqlcipher3-wheels' and restart to enable encryption."
        )
    else:
        detail = "DB_KEY is not set — the database file is NOT encrypted."
    return {"encrypted": encrypted, "driver": _driver_name, "detail": detail}


def preflight() -> None:
    """Fail fast at boot rather than silently writing plaintext shop data to disk."""
    if ENCRYPTION_AVAILABLE and settings.db_key:
        return
    if settings.allow_unencrypted_db:
        return
    status = encryption_status()
    raise DatabaseNotConfigured(
        f"Refusing to start: {status['detail']}\n"
        "Set DB_KEY in .env (and install sqlcipher3-wheels), or set "
        "ALLOW_UNENCRYPTED_DB=1 for local development only."
    )


# -- connections -------------------------------------------------------------

_local = threading.local()


def _configure(conn: Connection) -> None:
    conn.row_factory = Row
    keyed = ENCRYPTION_AVAILABLE and bool(settings.db_key)
    if keyed:
        # PRAGMA key must be the very first statement on the connection. The key is
        # passed as a quoted string literal, so any embedded quote is doubled.
        escaped = settings.db_key.replace("'", "''")
        conn.execute(f"PRAGMA key = '{escaped}'")
        conn.execute("PRAGMA cipher_memory_security = ON")

    # Touch the schema before anything else runs. This is checked even when no key was
    # supplied, because "no key against a file that turns out to be encrypted" fails just
    # as hard as a wrong key — and reaches here via ALLOW_UNENCRYPTED_DB, which skips the
    # preflight check. Without this the next ordinary PRAGMA dies with a bare
    # "file is not a database" traceback, which tells the operator nothing about the key.
    try:
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
    except DB_ERRORS as exc:
        raise DatabaseNotConfigured(_unreadable_message(keyed)) from exc

    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 8000")


def _unreadable_message(keyed: bool) -> str:
    """Explain an unreadable database file in terms of the key, not the driver."""
    if keyed:
        return (
            "Could not open the database with the supplied DB_KEY. "
            "The key is wrong, or the file was created with a different key."
        )
    if not ENCRYPTION_AVAILABLE:
        return (
            f"{settings.db_path} could not be read. It looks like an encrypted database, "
            "but the SQLCipher driver is not installed on this machine. "
            "Install 'sqlcipher3-wheels' and set DB_KEY, then try again."
        )
    hatch = (
        " ALLOW_UNENCRYPTED_DB is switched on, which is why startup did not stop earlier."
        if settings.allow_unencrypted_db
        else ""
    )
    return (
        f"{settings.db_path} could not be read. It looks like an encrypted database and "
        f"DB_KEY is not set.{hatch} Set DB_KEY in .env to the key this database was "
        "created with. There is no way to open it without that key."
    )


def connect_to(path: Any) -> Connection:
    """Open a configured connection to an arbitrary file, keyed the same way.

    Used by the backup command, so a copy of an encrypted database is itself encrypted
    with the same key rather than being written out in the clear.
    """
    conn = _driver.connect(
        str(path),
        timeout=15,
        isolation_level=None,  # explicit transactions via BEGIN/COMMIT
        check_same_thread=False,
    )
    _configure(conn)
    return conn


def connect() -> Connection:
    """Open a fresh configured connection. Callers are responsible for closing it."""
    settings.ensure_dirs()
    return connect_to(settings.db_path)


def get_connection() -> Connection:
    """Return this thread's long-lived connection, creating it on first use.

    FastAPI runs sync endpoints in a worker thread pool, so one connection per thread
    keeps things simple without needing a full pool.
    """
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = connect()
        _local.conn = conn
    return conn


def close_thread_connection() -> None:
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None


@contextmanager
def transaction(conn: Connection | None = None) -> Iterator[Connection]:
    """Run a block inside one IMMEDIATE transaction.

    Used for every stock-moving operation so that a failure halfway through a sale can
    never leave stock decremented without the sale row, and so an allocated invoice
    number is rolled back with its sale (no gaps in the series).
    """
    conn = conn or get_connection()
    if conn.in_transaction:
        # Nested use: join the caller's transaction instead of opening a second one.
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


# -- small query helpers -----------------------------------------------------


def query(sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> list[Row]:
    return get_connection().execute(sql, params).fetchall()


def query_one(sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> Row | None:
    return get_connection().execute(sql, params).fetchone()


def scalar(sql: str, params: Sequence[Any] | dict[str, Any] = (), default: Any = 0) -> Any:
    row = query_one(sql, params)
    if row is None or row[0] is None:
        return default
    return row[0]


def execute(sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> Cursor:
    return get_connection().execute(sql, params)


def insert(sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> int:
    return int(get_connection().execute(sql, params).lastrowid or 0)
