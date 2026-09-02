"""Storage layer: driver-agnosticism and encryption at rest.

The app supports two DBAPI drivers — the stdlib ``sqlite3`` module and SQLCipher via
``sqlcipher3`` — and they are *different C extensions with different types*. Code that
names ``sqlite3.Row`` or catches ``sqlite3.Error`` looks fine on a developer machine with
no SQLCipher and then breaks the moment the shop server has it installed:

    TypeError: Row() argument 1 must be sqlite3.Cursor, not sqlcipher3.dbapi2.Cursor

That is a real crash this app shipped with. These tests pin the contract so it cannot
come back: every DBAPI type the app touches must come from the loaded driver.

The encryption tests are skipped when SQLCipher is not installed. They are not optional
on the shop server — encryption at rest is the requirement that must not silently fail —
so ``test_encryption_available_when_expected`` reports loudly which mode ran.
"""

from __future__ import annotations

import sqlite3
import unittest

from app import db
from app.config import settings

from tests.support import TEST_DB_KEY, ShopTestCase

needs_sqlcipher = unittest.skipUnless(
    db.ENCRYPTION_AVAILABLE, "SQLCipher driver not installed on this machine"
)


class DriverAgnosticTypeTests(unittest.TestCase):
    """The regression tests for the sqlcipher3 TypeError."""

    def test_types_come_from_the_loaded_driver(self):
        # If the driver is SQLCipher, none of these may be the stdlib class. If it is the
        # stdlib driver, all of them are — and both cases are correct.
        expected_stdlib = db._driver_name == "sqlite3"
        for name, value in (
            ("Connection", db.Connection),
            ("Cursor", db.Cursor),
            ("Row", db.Row),
            ("Error", db.Error),
            ("IntegrityError", db.IntegrityError),
            ("OperationalError", db.OperationalError),
        ):
            with self.subTest(type=name):
                is_stdlib = value is getattr(sqlite3, name)
                self.assertEqual(
                    is_stdlib,
                    expected_stdlib,
                    f"db.{name} is {value!r}, which does not belong to driver "
                    f"'{db._driver_name}'",
                )

    def test_row_factory_matches_the_driver_cursor(self):
        # The exact failure that was reported: sqlite3.Row type-checks its cursor argument,
        # so a mismatched Row raises TypeError on the first fetch, not at assignment.
        conn = db.connect()
        try:
            row = conn.execute("SELECT 1 AS one, 'x' AS two").fetchone()
            self.assertIsInstance(row, db.Row)
        finally:
            conn.close()

    def test_rows_behave_as_mappings(self):
        # Templates and repo.product_payload rely on name access and dict(row); the
        # driver's Row is a separate C type, so this is worth asserting rather than
        # assuming it behaves like the stdlib one.
        conn = db.connect()
        try:
            row = conn.execute("SELECT 1 AS one, 'x' AS two").fetchone()
            self.assertEqual(row["one"], 1)
            self.assertEqual(row["two"], "x")
            self.assertEqual(list(row.keys()), ["one", "two"])
            self.assertEqual(dict(row), {"one": 1, "two": "x"})
            self.assertEqual(tuple(row), (1, "x"))
        finally:
            conn.close()

    def test_db_errors_can_actually_catch_a_driver_error(self):
        # A handler written against the wrong exception class does not crash — it simply
        # stops handling, which is harder to notice than a traceback.
        conn = db.connect()
        try:
            with self.assertRaises(db.DB_ERRORS):
                conn.execute("SELECT * FROM a_table_that_does_not_exist")
        finally:
            conn.close()

    def test_db_errors_includes_the_driver_and_the_stdlib_class(self):
        self.assertIn(db.Error, db.DB_ERRORS)
        self.assertIn(sqlite3.Error, db.DB_ERRORS)
        # Deduplicated on the stdlib driver rather than listing the same class twice.
        expected = 1 if db._driver_name == "sqlite3" else 2
        self.assertEqual(len(db.DB_ERRORS), expected)

    def test_no_module_names_the_stdlib_types_directly(self):
        # A grep-as-a-test: the whole bug class is "someone wrote sqlite3.Row again".
        # Scoped to the application code. app/db.py is exempt because that is where the
        # driver lookup and its stdlib fallbacks legitimately live.
        import pathlib

        root = pathlib.Path(__file__).resolve().parent.parent
        targets = [p for p in sorted(root.glob("app/**/*.py")) if p.name != "db.py"]
        targets += [p for p in (root / "manage.py", root / "run.py") if p.exists()]

        offenders = []
        for path in targets:
            for lineno, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), 1
            ):
                if line.lstrip().startswith("#"):
                    continue
                for bad in ("sqlite3.Row", "sqlite3.Cursor", "sqlite3.Connection"):
                    if bad in line:
                        offenders.append(f"{path.relative_to(root)}:{lineno}: {bad}")
        self.assertEqual(
            offenders,
            [],
            "these reference stdlib sqlite3 types directly; use db.Row / db.Cursor / "
            "db.Connection so SQLCipher works too:\n  " + "\n  ".join(offenders),
        )


class EncryptionStatusTests(ShopTestCase):
    def test_status_reports_the_mode_it_is_actually_in(self):
        status = db.encryption_status()
        self.assertEqual(status["driver"], db._driver_name)
        self.assertEqual(status["encrypted"], db.ENCRYPTION_AVAILABLE)
        self.assertTrue(str(status["detail"]))

    def test_encryption_available_when_expected(self):
        # Not a failure on a developer machine, but it must be visible which mode ran:
        # a green suite that silently skipped every encryption test is how "encryption
        # at rest" ends up not working on the one machine that needs it.
        if not db.ENCRYPTION_AVAILABLE:
            self.skipTest(
                "SQLCipher is NOT installed here — encryption at rest is untested in "
                "this run. On the shop server this must not be skipped."
            )
        self.assertTrue(db.encryption_status()["encrypted"])


@needs_sqlcipher
class EncryptedRoundTripTests(ShopTestCase):
    def test_the_file_on_disk_is_not_readable_plaintext(self):
        with db.transaction():
            db.insert(
                "INSERT INTO products (sku, name) VALUES (?, ?)",
                ("SECRET-SKU", "Nobody should read this"),
            )
        db.close_thread_connection()

        raw = settings.db_path.read_bytes()
        self.assertFalse(raw.startswith(b"SQLite format 3"))
        self.assertNotIn(b"SECRET-SKU", raw)
        self.assertNotIn(b"Nobody should read this", raw)

    def test_stdlib_driver_cannot_open_an_encrypted_file(self):
        db.close_thread_connection()
        with self.assertRaises(sqlite3.DatabaseError):
            sqlite3.connect(str(settings.db_path)).execute(
                "SELECT count(*) FROM sqlite_master"
            ).fetchone()

    def test_written_data_survives_a_fresh_connection(self):
        with db.transaction():
            product_id = db.insert(
                "INSERT INTO products (sku, name, gst_rate_bp) VALUES (?, ?, ?)",
                ("RT-1", "Round trip", 1800),
            )
        db.close_thread_connection()

        conn = db.connect()  # brand new connection, decrypts from disk
        try:
            conn.row_factory = db.Row
            row = conn.execute(
                "SELECT sku, name, gst_rate_bp FROM products WHERE id = ?", (product_id,)
            ).fetchone()
            self.assertEqual(row["sku"], "RT-1")
            self.assertEqual(row["name"], "Round trip")
            self.assertEqual(row["gst_rate_bp"], 1800)
        finally:
            conn.close()

    def test_wrong_key_is_refused_with_a_message_about_the_key(self):
        db.close_thread_connection()
        settings.db_key = TEST_DB_KEY + "-wrong"
        with self.assertRaises(db.DatabaseNotConfigured) as caught:
            db.connect()
        self.assertIn("DB_KEY", str(caught.exception))

    def test_missing_key_against_an_encrypted_file_is_refused_clearly(self):
        # Reachable via ALLOW_UNENCRYPTED_DB, which skips the preflight check: without
        # this guard the operator gets a bare "file is not a database" traceback instead
        # of being told the key is missing.
        db.close_thread_connection()
        settings.db_key = ""
        settings.allow_unencrypted_db = True
        with self.assertRaises(db.DatabaseNotConfigured) as caught:
            db.connect()
        message = str(caught.exception)
        self.assertIn("DB_KEY", message)
        self.assertIn("encrypted", message)

    def test_backup_copy_is_encrypted_with_the_same_key(self):
        with db.transaction():
            db.insert(
                "INSERT INTO products (sku, name) VALUES (?, ?)", ("BK-1", "Backup me")
            )
        source = db.get_connection()
        target = self._tmp / "copy.db"
        destination = db.connect_to(target)
        try:
            source.backup(destination)
        finally:
            destination.close()

        raw = target.read_bytes()
        self.assertFalse(raw.startswith(b"SQLite format 3"))
        self.assertNotIn(b"Backup me", raw)

        # Same key opens the copy, and the row is there.
        copy = db.connect_to(target)
        try:
            copy.row_factory = db.Row
            row = copy.execute("SELECT name FROM products WHERE sku = 'BK-1'").fetchone()
            self.assertEqual(row["name"], "Backup me")
        finally:
            copy.close()


if __name__ == "__main__":
    unittest.main()
