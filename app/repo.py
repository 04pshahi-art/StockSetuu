"""Shared data access: shop settings, product lookup, stock ledger, audit trail."""

from __future__ import annotations

import datetime as dt
from typing import Any

from . import db, gst

SHOP_FIELDS = (
    "legal_name",
    "trade_name",
    "gstin",
    "registration_type",
    "state_code",
    "address_line1",
    "address_line2",
    "city",
    "pincode",
    "phone",
    "email",
    "bank_details",
    "invoice_prefix",
    "invoice_terms",
    "warranty_basis",
    "default_prices_include_gst",
    "round_invoice_to_rupee",
    "default_gst_rate_bp",
    "default_low_stock",
    "default_service_sac",
)


def storage_status() -> dict[str, object]:
    return db.encryption_status()


# -- shop settings -----------------------------------------------------------


def get_shop_settings() -> dict[str, Any]:
    row = db.query_one("SELECT * FROM shop_settings WHERE id = 1")
    data = dict(row) if row is not None else {}
    data.setdefault("state_code", gst.HOME_STATE_CODE)
    data["state_name"] = gst.STATE_NAME_BY_CODE.get(str(data.get("state_code") or ""), "")
    data["address_block"] = format_shop_address(data)
    return data


def format_shop_address(shop: dict[str, Any]) -> str:
    parts = [
        str(shop.get("address_line1") or "").strip(),
        str(shop.get("address_line2") or "").strip(),
    ]
    city_line = " ".join(
        p for p in (str(shop.get("city") or "").strip(), str(shop.get("pincode") or "").strip()) if p
    )
    if city_line:
        parts.append(city_line)
    state_name = gst.STATE_NAME_BY_CODE.get(str(shop.get("state_code") or ""), "")
    if state_name:
        parts.append(f"{state_name} ({shop.get('state_code')})")
    return "\n".join(p for p in parts if p)


def update_shop_settings(values: dict[str, Any]) -> None:
    fields = [f for f in SHOP_FIELDS if f in values]
    if not fields:
        return
    assignments = ", ".join(f"{f} = ?" for f in fields)
    params = [values[f] for f in fields]
    with db.transaction():
        db.execute(
            f"UPDATE shop_settings SET {assignments}, updated_at = datetime('now', 'localtime') WHERE id = 1",
            params,
        )


def shop_state_code() -> str:
    row = db.query_one("SELECT state_code FROM shop_settings WHERE id = 1")
    code = str(row["state_code"]).strip() if row else ""
    return code or gst.HOME_STATE_CODE


# -- categories --------------------------------------------------------------


def list_categories() -> list[str]:
    rows = db.query("SELECT name FROM categories ORDER BY sort_order, name")
    return [r["name"] for r in rows]


def ensure_category(name: str) -> str:
    """Return an existing category name, adding it if the shop uses a new one."""
    clean = (name or "").strip() or "Other"
    row = db.query_one("SELECT name FROM categories WHERE name = ? COLLATE NOCASE", (clean,))
    if row is not None:
        return row["name"]
    db.execute("INSERT INTO categories (name, sort_order) VALUES (?, 50)", (clean,))
    return clean


# -- products ----------------------------------------------------------------


def get_product(product_id: int) -> db.Row | None:
    return db.query_one("SELECT * FROM products WHERE id = ?", (product_id,))


def get_product_by_sku(sku: str) -> db.Row | None:
    return db.query_one("SELECT * FROM products WHERE sku = ? COLLATE NOCASE", ((sku or "").strip(),))


def search_products(term: str, *, limit: int = 20, active_only: bool = True) -> list[db.Row]:
    """Match on SKU, name or brand — what the counter would type or scan.

    An exact SKU hit is ranked first so a barcode scan lands on the right row even when
    the code is also a substring of some other product's name.
    """
    term = (term or "").strip()
    where = ["1 = 1"]
    params: list[Any] = []
    if active_only:
        where.append("is_active = 1")
    if term:
        where.append("(sku LIKE ? OR name LIKE ? OR brand LIKE ?)")
        like = f"%{term}%"
        params.extend([like, like, like])
    params.append(term)
    params.append(limit)
    return db.query(
        f"""
        SELECT * FROM products
        WHERE {' AND '.join(where)}
        ORDER BY (sku = ? COLLATE NOCASE) DESC, name COLLATE NOCASE
        LIMIT ?
        """,
        params,
    )


def product_payload(row: db.Row) -> dict[str, Any]:
    """Trim a product row down to what the sale/purchase entry screens need."""
    return {
        "id": int(row["id"]),
        "sku": row["sku"],
        "name": row["name"],
        "brand": row["brand"],
        "category": row["category"],
        "unit": row["unit"],
        "hsn_code": row["hsn_code"],
        "gst_rate_bp": int(row["gst_rate_bp"]),
        "cost_price_paise": int(row["cost_price_paise"]),
        "sale_price_paise": int(row["sale_price_paise"]),
        "quantity": int(row["quantity"]),
        "low_stock_threshold": int(row["low_stock_threshold"]),
        "is_serialized": bool(row["is_serialized"]),
        "warranty_months": int(row["warranty_months"]),
        "specs": row["specs"],
    }


# -- stock ledger ------------------------------------------------------------


class StockError(Exception):
    """Raised when a movement would push stock below zero."""


def apply_stock_movement(
    *,
    product_id: int,
    delta: int,
    ref_type: str,
    ref_id: int | None,
    note: str = "",
    user_id: int | None = None,
    allow_negative: bool = False,
) -> int:
    """Move stock and record it in the ledger. Must run inside a transaction.

    ``products.quantity`` is a cache over ``stock_movements``; both are written together
    so the ledger can always be replayed to audit the on-hand number.
    """
    if delta == 0:
        return 0
    row = db.query_one("SELECT name, quantity FROM products WHERE id = ?", (product_id,))
    if row is None:
        raise StockError(f"Product #{product_id} no longer exists")
    new_quantity = int(row["quantity"]) + delta
    if new_quantity < 0 and not allow_negative:
        raise StockError(
            f"Not enough stock for {row['name']}: {row['quantity']} on hand, {abs(delta)} needed"
        )
    db.execute(
        "UPDATE products SET quantity = ?, updated_at = datetime('now', 'localtime') WHERE id = ?",
        (new_quantity, product_id),
    )
    db.execute(
        """
        INSERT INTO stock_movements (product_id, delta, ref_type, ref_id, note, created_by)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (product_id, delta, ref_type, ref_id, note, user_id),
    )
    return new_quantity


def set_stock(
    *, product_id: int, new_quantity: int, note: str, user_id: int | None = None
) -> None:
    """Adjust to an absolute count (stock take, CSV import)."""
    row = db.query_one("SELECT quantity FROM products WHERE id = ?", (product_id,))
    if row is None:
        raise StockError(f"Product #{product_id} no longer exists")
    delta = int(new_quantity) - int(row["quantity"])
    if delta:
        apply_stock_movement(
            product_id=product_id,
            delta=delta,
            ref_type="adjustment",
            ref_id=None,
            note=note,
            user_id=user_id,
            allow_negative=True,
        )


# -- audit -------------------------------------------------------------------


def audit(
    action: str,
    *,
    entity: str = "",
    entity_id: int | None = None,
    detail: str = "",
    user_id: int | None = None,
) -> None:
    db.execute(
        """
        INSERT INTO audit_log (user_id, action, entity, entity_id, detail)
        VALUES (?, ?, ?, ?, ?)
        """,
        (user_id, action, entity, entity_id, detail),
    )


# -- small shared queries ----------------------------------------------------


def list_dealers(*, active_only: bool = False) -> list[db.Row]:
    where = "WHERE is_active = 1" if active_only else ""
    return db.query(f"SELECT * FROM dealers {where} ORDER BY name COLLATE NOCASE")


def get_dealer(dealer_id: int) -> db.Row | None:
    return db.query_one("SELECT * FROM dealers WHERE id = ?", (dealer_id,))


def parse_date(value: str | None, *, field: str = "date", default_today: bool = True) -> str:
    """Validate an ISO date coming from a form; returns the normalised ISO string."""
    text = (value or "").strip()
    if not text:
        if default_today:
            return dt.date.today().isoformat()
        raise ValueError(f"{field} is required")
    try:
        return dt.date.fromisoformat(text[:10]).isoformat()
    except ValueError as exc:
        raise ValueError(f"{field} must be a valid date (YYYY-MM-DD)") from exc


def add_months(start: dt.date, months: int) -> dt.date:
    """Calendar-correct month addition, clamping to the end of a short month."""
    if not months:
        return start
    total = start.month - 1 + months
    year = start.year + total // 12
    month = total % 12 + 1
    day = min(start.day, _days_in_month(year, month))
    return dt.date(year, month, day)


def _days_in_month(year: int, month: int) -> int:
    if month == 12:
        return 31
    return (dt.date(year + month // 12, month % 12 + 1, 1) - dt.timedelta(days=1)).day
