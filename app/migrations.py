"""Schema migrations.

Versioned with ``PRAGMA user_version`` so upgrades are automatic and idempotent. Add new
work as a new entry in ``MIGRATIONS``; never edit a migration that has shipped.

Every timestamp default is ``datetime('now', 'localtime')``, not the more obvious
``datetime('now')``. SQLite's ``now`` is UTC, which on this shop's clock reads 5 hours 30
minutes early — an invoice recorded at 7:25 in the evening was being stamped 1:55 in the
afternoon. There is one shop in one timezone here, so wall-clock time is the only reading
anyone wants, and it has to match the ``datetime.now()`` values Python writes elsewhere.
"""

from __future__ import annotations

from . import db

# Each migration is (version, list of statements). Applied in order, in one transaction.
MIGRATIONS: list[tuple[int, list[str]]] = [
    (
        1,
        [
            # -- users -----------------------------------------------------
            """
            CREATE TABLE users (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                username        TEXT NOT NULL COLLATE NOCASE UNIQUE,
                display_name    TEXT NOT NULL DEFAULT '',
                password_hash   TEXT NOT NULL,
                is_active       INTEGER NOT NULL DEFAULT 1,
                session_epoch   INTEGER NOT NULL DEFAULT 1,
                failed_attempts INTEGER NOT NULL DEFAULT 0,
                locked_until    TEXT,
                last_login_at   TEXT,
                created_at      TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
            )
            """,
            # -- shop settings (single row) --------------------------------
            """
            CREATE TABLE shop_settings (
                id                       INTEGER PRIMARY KEY CHECK (id = 1),
                legal_name               TEXT NOT NULL DEFAULT '',
                trade_name               TEXT NOT NULL DEFAULT '',
                gstin                    TEXT NOT NULL DEFAULT '',
                registration_type        TEXT NOT NULL DEFAULT 'Regular',
                state_code               TEXT NOT NULL DEFAULT '27',
                address_line1            TEXT NOT NULL DEFAULT '',
                address_line2            TEXT NOT NULL DEFAULT '',
                city                     TEXT NOT NULL DEFAULT '',
                pincode                  TEXT NOT NULL DEFAULT '',
                phone                    TEXT NOT NULL DEFAULT '',
                email                    TEXT NOT NULL DEFAULT '',
                bank_details              TEXT NOT NULL DEFAULT '',
                invoice_prefix           TEXT NOT NULL DEFAULT 'PCS',
                invoice_terms            TEXT NOT NULL DEFAULT '',
                warranty_basis           TEXT NOT NULL DEFAULT 'sale'
                                          CHECK (warranty_basis IN ('sale','purchase')),
                default_prices_include_gst INTEGER NOT NULL DEFAULT 0,
                round_invoice_to_rupee   INTEGER NOT NULL DEFAULT 1,
                default_gst_rate_bp      INTEGER NOT NULL DEFAULT 1800,
                default_low_stock        INTEGER NOT NULL DEFAULT 2,
                default_service_sac      TEXT NOT NULL DEFAULT '998713',
                updated_at               TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
            )
            """,
            "INSERT INTO shop_settings (id) VALUES (1)",
            # -- catalogue -------------------------------------------------
            """
            CREATE TABLE categories (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                name     TEXT NOT NULL COLLATE NOCASE UNIQUE,
                sort_order INTEGER NOT NULL DEFAULT 100
            )
            """,
            "INSERT INTO categories (name, sort_order) VALUES ('Computer Parts', 10)",
            "INSERT INTO categories (name, sort_order) VALUES ('CCTV Parts', 20)",
            "INSERT INTO categories (name, sort_order) VALUES ('Accessories', 30)",
            "INSERT INTO categories (name, sort_order) VALUES ('Other', 90)",
            """
            CREATE TABLE products (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                sku                 TEXT NOT NULL COLLATE NOCASE UNIQUE,
                name                TEXT NOT NULL,
                category            TEXT NOT NULL DEFAULT 'Other',
                brand               TEXT NOT NULL DEFAULT '',
                unit                TEXT NOT NULL DEFAULT 'Nos',
                hsn_code            TEXT NOT NULL DEFAULT '',
                gst_rate_bp         INTEGER NOT NULL DEFAULT 1800,
                cost_price_paise    INTEGER NOT NULL DEFAULT 0,
                sale_price_paise    INTEGER NOT NULL DEFAULT 0,
                quantity            INTEGER NOT NULL DEFAULT 0,
                low_stock_threshold INTEGER NOT NULL DEFAULT 2,
                is_serialized       INTEGER NOT NULL DEFAULT 0,
                warranty_months     INTEGER NOT NULL DEFAULT 0,
                specs               TEXT NOT NULL DEFAULT '',
                is_active           INTEGER NOT NULL DEFAULT 1,
                created_at          TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                updated_at          TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
            )
            """,
            "CREATE INDEX idx_products_name ON products(name)",
            "CREATE INDEX idx_products_category ON products(category)",
            # -- dealers ---------------------------------------------------
            """
            CREATE TABLE dealers (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                name           TEXT NOT NULL COLLATE NOCASE UNIQUE,
                contact_number TEXT NOT NULL DEFAULT '',
                gstin          TEXT NOT NULL DEFAULT '',
                state_code     TEXT NOT NULL DEFAULT '',
                address        TEXT NOT NULL DEFAULT '',
                notes          TEXT NOT NULL DEFAULT '',
                is_active      INTEGER NOT NULL DEFAULT 1,
                created_at     TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
            )
            """,
            # -- purchases (stock in) --------------------------------------
            """
            CREATE TABLE purchases (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                dealer_id      INTEGER NOT NULL REFERENCES dealers(id),
                bill_number    TEXT NOT NULL DEFAULT '',
                bill_date      TEXT NOT NULL,
                interstate     INTEGER NOT NULL DEFAULT 0,
                taxable_paise  INTEGER NOT NULL DEFAULT 0,
                cgst_paise     INTEGER NOT NULL DEFAULT 0,
                sgst_paise     INTEGER NOT NULL DEFAULT 0,
                igst_paise     INTEGER NOT NULL DEFAULT 0,
                round_off_paise INTEGER NOT NULL DEFAULT 0,
                total_paise    INTEGER NOT NULL DEFAULT 0,
                notes          TEXT NOT NULL DEFAULT '',
                is_void        INTEGER NOT NULL DEFAULT 0,
                void_reason    TEXT NOT NULL DEFAULT '',
                voided_at      TEXT,
                created_by     INTEGER REFERENCES users(id),
                created_at     TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
            )
            """,
            "CREATE INDEX idx_purchases_dealer ON purchases(dealer_id, bill_date)",
            "CREATE INDEX idx_purchases_date ON purchases(bill_date)",
            "CREATE INDEX idx_purchases_bill ON purchases(bill_number COLLATE NOCASE)",
            # One dealer cannot have the same bill number twice; blank is allowed freely.
            """
            CREATE UNIQUE INDEX idx_purchases_dealer_bill
                ON purchases(dealer_id, bill_number COLLATE NOCASE)
                WHERE bill_number <> ''
            """,
            """
            CREATE TABLE purchase_items (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                purchase_id      INTEGER NOT NULL REFERENCES purchases(id) ON DELETE CASCADE,
                product_id       INTEGER NOT NULL REFERENCES products(id),
                description      TEXT NOT NULL DEFAULT '',
                hsn_code         TEXT NOT NULL DEFAULT '',
                qty              INTEGER NOT NULL,
                unit_cost_paise  INTEGER NOT NULL DEFAULT 0,
                gst_rate_bp      INTEGER NOT NULL DEFAULT 0,
                taxable_paise    INTEGER NOT NULL DEFAULT 0,
                cgst_paise       INTEGER NOT NULL DEFAULT 0,
                sgst_paise       INTEGER NOT NULL DEFAULT 0,
                igst_paise       INTEGER NOT NULL DEFAULT 0,
                total_paise      INTEGER NOT NULL DEFAULT 0
            )
            """,
            "CREATE INDEX idx_purchase_items_purchase ON purchase_items(purchase_id)",
            "CREATE INDEX idx_purchase_items_product ON purchase_items(product_id)",
            # -- sales (stock out) -----------------------------------------
            """
            CREATE TABLE sales (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                invoice_number      TEXT NOT NULL UNIQUE,
                invoice_seq         INTEGER NOT NULL,
                fy_label            TEXT NOT NULL,
                invoice_date        TEXT NOT NULL,
                customer_name       TEXT NOT NULL DEFAULT '',
                customer_phone      TEXT NOT NULL DEFAULT '',
                customer_gstin      TEXT NOT NULL DEFAULT '',
                customer_state_code TEXT NOT NULL DEFAULT '',
                customer_address    TEXT NOT NULL DEFAULT '',
                interstate          INTEGER NOT NULL DEFAULT 0,
                prices_include_gst  INTEGER NOT NULL DEFAULT 0,
                taxable_paise       INTEGER NOT NULL DEFAULT 0,
                cgst_paise          INTEGER NOT NULL DEFAULT 0,
                sgst_paise          INTEGER NOT NULL DEFAULT 0,
                igst_paise          INTEGER NOT NULL DEFAULT 0,
                round_off_paise     INTEGER NOT NULL DEFAULT 0,
                total_paise         INTEGER NOT NULL DEFAULT 0,
                payment_mode        TEXT NOT NULL DEFAULT 'Cash',
                notes               TEXT NOT NULL DEFAULT '',
                is_void             INTEGER NOT NULL DEFAULT 0,
                void_reason         TEXT NOT NULL DEFAULT '',
                voided_at           TEXT,
                created_by          INTEGER REFERENCES users(id),
                created_at          TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
            )
            """,
            "CREATE INDEX idx_sales_date ON sales(invoice_date)",
            "CREATE INDEX idx_sales_customer ON sales(customer_name COLLATE NOCASE)",
            "CREATE INDEX idx_sales_phone ON sales(customer_phone)",
            "CREATE UNIQUE INDEX idx_sales_series ON sales(fy_label, invoice_seq)",
            """
            CREATE TABLE sale_items (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                sale_id          INTEGER NOT NULL REFERENCES sales(id) ON DELETE CASCADE,
                product_id       INTEGER NOT NULL REFERENCES products(id),
                description      TEXT NOT NULL DEFAULT '',
                hsn_code         TEXT NOT NULL DEFAULT '',
                qty              INTEGER NOT NULL,
                unit_price_paise INTEGER NOT NULL DEFAULT 0,
                discount_paise   INTEGER NOT NULL DEFAULT 0,
                gst_rate_bp      INTEGER NOT NULL DEFAULT 0,
                taxable_paise    INTEGER NOT NULL DEFAULT 0,
                cgst_paise       INTEGER NOT NULL DEFAULT 0,
                sgst_paise       INTEGER NOT NULL DEFAULT 0,
                igst_paise       INTEGER NOT NULL DEFAULT 0,
                total_paise      INTEGER NOT NULL DEFAULT 0
            )
            """,
            "CREATE INDEX idx_sale_items_sale ON sale_items(sale_id)",
            "CREATE INDEX idx_sale_items_product ON sale_items(product_id)",
            # -- serial / warranty tracking --------------------------------
            """
            CREATE TABLE serials (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id       INTEGER NOT NULL REFERENCES products(id),
                serial_no        TEXT NOT NULL COLLATE NOCASE UNIQUE,
                purchase_id      INTEGER REFERENCES purchases(id),
                purchase_item_id INTEGER REFERENCES purchase_items(id),
                purchase_date    TEXT,
                warranty_months  INTEGER NOT NULL DEFAULT 0,
                warranty_expiry  TEXT,
                status           TEXT NOT NULL DEFAULT 'in_stock'
                                  CHECK (status IN ('in_stock','sold','returned')),
                sale_id          INTEGER REFERENCES sales(id),
                sale_item_id     INTEGER REFERENCES sale_items(id),
                sold_at          TEXT,
                notes            TEXT NOT NULL DEFAULT '',
                created_at       TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
            )
            """,
            "CREATE INDEX idx_serials_product ON serials(product_id, status)",
            "CREATE INDEX idx_serials_status ON serials(status)",
            "CREATE INDEX idx_serials_sale ON serials(sale_id)",
            "CREATE INDEX idx_serials_expiry ON serials(warranty_expiry)",
            # -- service jobs ----------------------------------------------
            """
            CREATE TABLE service_jobs (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                job_number          TEXT NOT NULL UNIQUE,
                customer_name       TEXT NOT NULL DEFAULT '',
                customer_phone      TEXT NOT NULL DEFAULT '',
                customer_address    TEXT NOT NULL DEFAULT '',
                customer_state_code TEXT NOT NULL DEFAULT '',
                job_date            TEXT NOT NULL,
                description         TEXT NOT NULL DEFAULT '',
                sac_code            TEXT NOT NULL DEFAULT '',
                amount_paise        INTEGER NOT NULL DEFAULT 0,
                gst_rate_bp         INTEGER NOT NULL DEFAULT 0,
                cgst_paise          INTEGER NOT NULL DEFAULT 0,
                sgst_paise          INTEGER NOT NULL DEFAULT 0,
                igst_paise          INTEGER NOT NULL DEFAULT 0,
                parts_total_paise   INTEGER NOT NULL DEFAULT 0,
                total_paise         INTEGER NOT NULL DEFAULT 0,
                status              TEXT NOT NULL DEFAULT 'pending'
                                     CHECK (status IN ('pending','completed','cancelled')),
                notes               TEXT NOT NULL DEFAULT '',
                completed_at        TEXT,
                created_by          INTEGER REFERENCES users(id),
                created_at          TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
            )
            """,
            "CREATE INDEX idx_service_status ON service_jobs(status, job_date)",
            "CREATE INDEX idx_service_date ON service_jobs(job_date)",
            "CREATE INDEX idx_service_customer ON service_jobs(customer_name COLLATE NOCASE)",
            """
            CREATE TABLE service_job_parts (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id           INTEGER NOT NULL REFERENCES service_jobs(id) ON DELETE CASCADE,
                product_id       INTEGER NOT NULL REFERENCES products(id),
                description      TEXT NOT NULL DEFAULT '',
                qty              INTEGER NOT NULL,
                unit_price_paise INTEGER NOT NULL DEFAULT 0,
                total_paise      INTEGER NOT NULL DEFAULT 0
            )
            """,
            "CREATE INDEX idx_service_parts_job ON service_job_parts(job_id)",
            # -- stock ledger ----------------------------------------------
            """
            CREATE TABLE stock_movements (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id INTEGER NOT NULL REFERENCES products(id),
                delta      INTEGER NOT NULL,
                ref_type   TEXT NOT NULL,
                ref_id     INTEGER,
                note       TEXT NOT NULL DEFAULT '',
                created_by INTEGER REFERENCES users(id),
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
            )
            """,
            "CREATE INDEX idx_movements_product ON stock_movements(product_id, created_at)",
            "CREATE INDEX idx_movements_ref ON stock_movements(ref_type, ref_id)",
            # -- gap-free counters ------------------------------------------
            """
            CREATE TABLE counters (
                name  TEXT PRIMARY KEY,
                value INTEGER NOT NULL DEFAULT 0
            )
            """,
            # -- audit trail -------------------------------------------------
            """
            CREATE TABLE audit_log (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                at        TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                user_id   INTEGER REFERENCES users(id),
                action    TEXT NOT NULL,
                entity    TEXT NOT NULL DEFAULT '',
                entity_id INTEGER,
                detail    TEXT NOT NULL DEFAULT ''
            )
            """,
            "CREATE INDEX idx_audit_at ON audit_log(at)",
        ],
    ),
]

LATEST_VERSION = max(version for version, _ in MIGRATIONS)


def current_version(conn: db.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def migrate(conn: db.Connection | None = None) -> list[int]:
    """Apply any outstanding migrations. Returns the versions applied."""
    conn = conn or db.get_connection()
    applied: list[int] = []
    for version, statements in MIGRATIONS:
        if current_version(conn) >= version:
            continue
        with db.transaction(conn):
            for statement in statements:
                conn.execute(statement)
        # PRAGMA user_version cannot be parameterised and must sit outside the txn.
        conn.execute(f"PRAGMA user_version = {int(version)}")
        applied.append(version)
    return applied


def next_counter(name: str, conn: db.Connection | None = None) -> int:
    """Atomically increment and return a counter.

    Must be called inside the caller's transaction: if that transaction rolls back the
    counter rolls back with it, which is what keeps the invoice series gap-free.
    """
    conn = conn or db.get_connection()
    conn.execute(
        "INSERT INTO counters(name, value) VALUES (?, 0) ON CONFLICT(name) DO NOTHING",
        (name,),
    )
    conn.execute("UPDATE counters SET value = value + 1 WHERE name = ?", (name,))
    return int(conn.execute("SELECT value FROM counters WHERE name = ?", (name,)).fetchone()[0])
