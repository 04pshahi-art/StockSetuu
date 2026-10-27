"""Shared harness for the test suite.

Every test class gets its own throwaway database. ``app.config.settings`` is a
module-level singleton created at import time, so the settings are redirected in place
and the thread's cached connection is dropped — that is cheaper and more predictable
than trying to re-import the app package per test.

The tests run against whichever driver is installed. When SQLCipher is present they run
against a real encrypted database with a throwaway key, so the driver-specific paths
(``Row``, ``Cursor``, the exception classes) are exercised rather than assumed; with only
the stdlib driver they fall back to an unencrypted file. Either way the assertions are the
same, which is the point — the shop's arithmetic must not depend on the storage layer.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from app import db, migrations, repo
from app.config import settings

# A fixed throwaway key. Nothing real is stored under it and each test gets a fresh file;
# it exists so the encrypted code path is what the suite actually runs through.
TEST_DB_KEY = "test-suite-throwaway-key"


class ShopTestCase(unittest.TestCase):
    """Base class providing a migrated, empty shop database per test."""

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="pcs-test-"))
        self._saved = (
            settings.data_dir,
            settings.db_path,
            settings.backup_dir,
            settings.db_key,
            settings.allow_unencrypted_db,
        )
        settings.data_dir = self._tmp
        settings.db_path = self._tmp / "shop.db"
        settings.backup_dir = self._tmp / "backups"
        settings.db_key = TEST_DB_KEY if db.ENCRYPTION_AVAILABLE else ""
        settings.allow_unencrypted_db = not db.ENCRYPTION_AVAILABLE

        db.close_thread_connection()
        migrations.migrate()
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        db.close_thread_connection()
        (
            settings.data_dir,
            settings.db_path,
            settings.backup_dir,
            settings.db_key,
            settings.allow_unencrypted_db,
        ) = self._saved
        shutil.rmtree(self._tmp, ignore_errors=True)

    # -- fixtures ------------------------------------------------------------

    def make_product(
        self,
        *,
        sku: str = "TEST-1",
        name: str = "Test item",
        gst_rate_bp: int = 1800,
        hsn_code: str = "8473",
        cost_price_paise: int = 100_000,
        sale_price_paise: int = 150_000,
        quantity: int = 0,
        is_serialized: int = 0,
        warranty_months: int = 0,
        category: str = "Computer Parts",
    ) -> int:
        with db.transaction():
            return db.insert(
                """
                INSERT INTO products (
                    sku, name, category, hsn_code, gst_rate_bp, cost_price_paise,
                    sale_price_paise, quantity, is_serialized, warranty_months
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sku,
                    name,
                    category,
                    hsn_code,
                    gst_rate_bp,
                    cost_price_paise,
                    sale_price_paise,
                    quantity,
                    is_serialized,
                    warranty_months,
                ),
            )

    def make_dealer(
        self,
        *,
        name: str = "Test Dealer",
        gstin: str = "",
        state_code: str = "27",
    ) -> int:
        with db.transaction():
            return db.insert(
                "INSERT INTO dealers (name, gstin, state_code) VALUES (?, ?, ?)",
                (name, gstin, state_code),
            )

    def set_shop_state(self, state_code: str) -> None:
        repo.update_shop_settings({"state_code": state_code})

    # -- assertions ----------------------------------------------------------

    def assertStockLedgerAgrees(self, product_id: int) -> None:
        """``products.quantity`` is a cache; the ledger is the truth.

        Any operation that moves stock must write both, so replaying the ledger has to
        reproduce the cached number exactly. This is the check that would catch a code
        path that quietly updates one without the other.
        """
        cached = db.scalar("SELECT quantity FROM products WHERE id = ?", (product_id,))
        replayed = db.scalar(
            "SELECT COALESCE(SUM(delta), 0) FROM stock_movements WHERE product_id = ?",
            (product_id,),
        )
        self.assertEqual(
            int(cached),
            int(replayed),
            f"products.quantity ({cached}) disagrees with the movement ledger ({replayed})",
        )
