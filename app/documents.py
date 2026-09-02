"""Creation and reversal of the three stock-moving documents: purchases, sales and
service jobs.

Kept out of the routers so the rules that matter — gap-free invoice numbering, stock never
going negative, serials only being sold once — are in one place and can be tested directly.
Every public function here runs the whole document inside a single transaction.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from typing import Any

from . import db, gst, migrations, money, repo


class DocumentError(Exception):
    """A validation or business-rule failure with a message safe to show the user."""


# -- line parsing ------------------------------------------------------------


@dataclass(slots=True)
class LineInput:
    product_id: int
    qty: int
    unit_price_paise: int
    gst_rate_bp: int
    hsn_code: str
    description: str
    discount_paise: int = 0
    serials: list[str] = field(default_factory=list)
    warranty_months: int = 0
    product: db.Row | None = None


def _clean_serials(raw: Any) -> list[str]:
    """Accept a list or a comma/newline separated string; de-duplicate case-insensitively."""
    if raw is None:
        return []
    if isinstance(raw, str):
        candidates = raw.replace("\n", ",").replace(";", ",").split(",")
    elif isinstance(raw, (list, tuple)):
        candidates = [str(item) for item in raw]
    else:
        raise DocumentError("Serial numbers must be a list or comma-separated text")
    seen: dict[str, str] = {}
    for candidate in candidates:
        value = str(candidate).strip()
        if not value:
            continue
        key = value.casefold()
        if key in seen:
            raise DocumentError(f"Serial number '{value}' is listed twice")
        seen[key] = value
    return list(seen.values())


def parse_lines(
    lines_json: str,
    *,
    price_field: str,
    require_price: bool = True,
) -> list[LineInput]:
    """Parse the ``lines_json`` hidden field posted by the entry screens.

    ``price_field`` is ``"unit_price"`` for sales and ``"unit_cost"`` for purchases, which
    keeps the two forms readable while sharing this validator.
    """
    try:
        raw = json.loads(lines_json or "[]")
    except json.JSONDecodeError as exc:
        raise DocumentError("Could not read the line items — please re-enter them") from exc
    if not isinstance(raw, list) or not raw:
        raise DocumentError("Add at least one item before saving")

    parsed: list[LineInput] = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise DocumentError(f"Line {index} is malformed")
        label = f"Line {index}"
        try:
            product_id = int(item.get("product_id") or 0)
        except (TypeError, ValueError) as exc:
            raise DocumentError(f"{label}: product is missing") from exc
        if product_id <= 0:
            raise DocumentError(f"{label}: pick a product")

        product = repo.get_product(product_id)
        if product is None:
            raise DocumentError(f"{label}: product #{product_id} no longer exists")

        try:
            qty = money.parse_qty(item.get("qty"), field=f"{label} quantity")
            price = money.paise(item.get(price_field), field=f"{label} price")
            discount = money.paise(item.get("discount"), field=f"{label} discount")
        except money.MoneyError as exc:
            raise DocumentError(str(exc)) from exc
        if require_price and price <= 0 and discount == 0:
            # A zero-value line is almost always a mis-scan, and it silently distorts
            # the GST register, so make the operator confirm it as a discount instead.
            raise DocumentError(f"{label}: enter a price above zero")
        if discount > qty * price:
            raise DocumentError(f"{label}: discount is larger than the line amount")

        rate_raw = item.get("gst_rate_bp")
        try:
            rate_bp = int(rate_raw) if rate_raw not in (None, "") else int(product["gst_rate_bp"])
        except (TypeError, ValueError) as exc:
            raise DocumentError(f"{label}: GST rate is not valid") from exc
        if rate_bp < 0 or rate_bp > 10_000:
            raise DocumentError(f"{label}: GST rate must be between 0% and 100%")

        warranty_raw = item.get("warranty_months")
        try:
            warranty_months = (
                int(warranty_raw) if warranty_raw not in (None, "") else int(product["warranty_months"])
            )
        except (TypeError, ValueError) as exc:
            raise DocumentError(f"{label}: warranty months is not a number") from exc
        if warranty_months < 0:
            raise DocumentError(f"{label}: warranty months cannot be negative")

        serials = _clean_serials(item.get("serials"))
        if serials and not int(product["is_serialized"]):
            raise DocumentError(
                f"{label}: {product['name']} is not marked as serial-tracked — "
                "turn that on in the product first"
            )
        if int(product["is_serialized"]) and len(serials) != qty:
            raise DocumentError(
                f"{label}: {product['name']} is serial-tracked, so exactly {qty} "
                f"serial number(s) are needed ({len(serials)} given)"
            )

        parsed.append(
            LineInput(
                product_id=product_id,
                qty=qty,
                unit_price_paise=price,
                gst_rate_bp=rate_bp,
                hsn_code=str(item.get("hsn_code") or product["hsn_code"] or "").strip(),
                description=str(item.get("description") or product["name"]).strip(),
                discount_paise=discount,
                serials=serials,
                warranty_months=warranty_months,
                product=product,
            )
        )

    # A product scanned twice becomes two lines; that is fine, but the same serial across
    # two lines is not.
    all_serials = [s.casefold() for line in parsed for s in line.serials]
    duplicates = {s for s in all_serials if all_serials.count(s) > 1}
    if duplicates:
        raise DocumentError(f"Serial number(s) repeated across lines: {', '.join(sorted(duplicates))}")
    return parsed


# -- sales -------------------------------------------------------------------


def next_invoice_number(invoice_date: dt.date, conn: db.Connection) -> tuple[str, int, str]:
    """Allocate the next invoice number in the financial year's series.

    Returns ``(invoice_number, sequence, fy_label)``. Called inside the sale's transaction
    so a rolled-back sale also rolls back the counter, keeping the series gap-free as GST
    rules require.
    """
    label = gst.fy_label(invoice_date)
    sequence = migrations.next_counter(f"invoice:{label}", conn)
    prefix_row = conn.execute("SELECT invoice_prefix FROM shop_settings WHERE id = 1").fetchone()
    prefix = (prefix_row["invoice_prefix"] if prefix_row else "") or "INV"
    return f"{prefix}/{label}/{sequence:04d}", sequence, label


@dataclass(slots=True)
class CustomerInput:
    name: str = ""
    phone: str = ""
    gstin: str = ""
    state_code: str = ""
    address: str = ""


def create_sale(
    *,
    invoice_date: str,
    customer: CustomerInput,
    lines: list[LineInput],
    payment_mode: str = "Cash",
    prices_include_gst: bool = False,
    round_to_rupee: bool = True,
    notes: str = "",
    user_id: int | None = None,
) -> dict[str, Any]:
    """Record a sale, compute its GST, allocate an invoice number and move stock."""
    if not lines:
        raise DocumentError("Add at least one item before saving")

    iso_date = repo.parse_date(invoice_date, field="Invoice date")
    date_obj = dt.date.fromisoformat(iso_date)
    if date_obj > dt.date.today():
        raise DocumentError("Invoice date cannot be in the future")

    gstin = gst.normalise_gstin(customer.gstin)
    ok, problem = gst.validate_gstin(gstin)
    if not ok:
        raise DocumentError(f"Customer GSTIN: {problem}")

    shop_state = repo.shop_state_code()
    # A blank state means a walk-in buyer, who is treated as local — this is what makes
    # CGST+SGST the default and IGST the exception.
    party_state = gst.resolve_state_code(customer.state_code, gstin, shop_state)
    interstate = gst.is_interstate(shop_state, party_state)

    taxed = [
        gst.compute_line(
            qty=line.qty,
            unit_price_paise=line.unit_price_paise,
            rate_bp=line.gst_rate_bp,
            interstate=interstate,
            price_includes_gst=prices_include_gst,
            hsn_code=line.hsn_code,
            discount_paise=line.discount_paise,
        )
        for line in lines
    ]
    totals = gst.total_lines(taxed, round_to_rupee=round_to_rupee)

    with db.transaction() as conn:
        invoice_number, sequence, fy = next_invoice_number(date_obj, conn)
        sale_id = db.insert(
            """
            INSERT INTO sales (
                invoice_number, invoice_seq, fy_label, invoice_date,
                customer_name, customer_phone, customer_gstin, customer_state_code,
                customer_address, interstate, prices_include_gst,
                taxable_paise, cgst_paise, sgst_paise, igst_paise,
                round_off_paise, total_paise, payment_mode, notes, created_by
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                invoice_number,
                sequence,
                fy,
                iso_date,
                customer.name.strip(),
                customer.phone.strip(),
                gstin,
                party_state,
                customer.address.strip(),
                int(interstate),
                int(prices_include_gst),
                totals.taxable_paise,
                totals.cgst_paise,
                totals.sgst_paise,
                totals.igst_paise,
                totals.round_off_paise,
                totals.grand_total_paise,
                (payment_mode or "Cash").strip(),
                notes.strip(),
                user_id,
            ),
        )

        for line, tax in zip(lines, taxed, strict=True):
            item_id = db.insert(
                """
                INSERT INTO sale_items (
                    sale_id, product_id, description, hsn_code, qty, unit_price_paise,
                    discount_paise, gst_rate_bp, taxable_paise,
                    cgst_paise, sgst_paise, igst_paise, total_paise
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sale_id,
                    line.product_id,
                    line.description,
                    tax.hsn_code,
                    line.qty,
                    line.unit_price_paise,
                    line.discount_paise,
                    line.gst_rate_bp,
                    tax.taxable_paise,
                    tax.cgst_paise,
                    tax.sgst_paise,
                    tax.igst_paise,
                    tax.total_paise,
                ),
            )
            try:
                repo.apply_stock_movement(
                    product_id=line.product_id,
                    delta=-line.qty,
                    ref_type="sale",
                    ref_id=sale_id,
                    note=invoice_number,
                    user_id=user_id,
                )
            except repo.StockError as exc:
                raise DocumentError(str(exc)) from exc

            if line.serials:
                _sell_serials(
                    serials=line.serials,
                    product_id=line.product_id,
                    sale_id=sale_id,
                    sale_item_id=item_id,
                    sale_date=iso_date,
                )

        repo.audit(
            "sale.create",
            entity="sale",
            entity_id=sale_id,
            detail=f"{invoice_number} — {money.fmt_money(totals.grand_total_paise)}",
            user_id=user_id,
        )

    return {
        "sale_id": sale_id,
        "invoice_number": invoice_number,
        "total_paise": totals.grand_total_paise,
        "interstate": interstate,
    }


def _sell_serials(
    *, serials: list[str], product_id: int, sale_id: int, sale_item_id: int, sale_date: str
) -> None:
    """Attach serials to a sale, refusing anything already sold or belonging elsewhere."""
    basis = str(
        db.scalar("SELECT warranty_basis FROM shop_settings WHERE id = 1", default="sale")
    )
    for serial in serials:
        row = db.query_one(
            "SELECT * FROM serials WHERE serial_no = ? COLLATE NOCASE", (serial,)
        )
        if row is None:
            raise DocumentError(
                f"Serial '{serial}' is not in stock — add it through a purchase entry first"
            )
        if int(row["product_id"]) != product_id:
            other = repo.get_product(int(row["product_id"]))
            name = other["name"] if other else f"product #{row['product_id']}"
            raise DocumentError(f"Serial '{serial}' belongs to {name}, not the product on this line")
        if row["status"] != "in_stock":
            raise DocumentError(f"Serial '{serial}' is already marked '{row['status']}'")

        months = int(row["warranty_months"] or 0)
        if basis == "sale" and months:
            expiry = repo.add_months(dt.date.fromisoformat(sale_date), months).isoformat()
        else:
            expiry = row["warranty_expiry"]
        db.execute(
            """
            UPDATE serials
               SET status = 'sold', sale_id = ?, sale_item_id = ?, sold_at = ?, warranty_expiry = ?
             WHERE id = ?
            """,
            (sale_id, sale_item_id, sale_date, expiry, int(row["id"])),
        )


def void_sale(sale_id: int, *, reason: str, user_id: int | None = None) -> None:
    """Cancel a sale, returning stock and serials.

    The invoice number is deliberately kept: deleting it would leave a gap in the series,
    which is exactly what GST rules forbid. A cancelled invoice is reported as cancelled.
    """
    with db.transaction():
        sale = db.query_one("SELECT * FROM sales WHERE id = ?", (sale_id,))
        if sale is None:
            raise DocumentError("That sale no longer exists")
        if int(sale["is_void"]):
            raise DocumentError("That sale is already cancelled")

        for item in db.query("SELECT * FROM sale_items WHERE sale_id = ?", (sale_id,)):
            repo.apply_stock_movement(
                product_id=int(item["product_id"]),
                delta=int(item["qty"]),
                ref_type="sale_void",
                ref_id=sale_id,
                note=f"Cancelled {sale['invoice_number']}",
                user_id=user_id,
            )

        basis = str(
            db.scalar("SELECT warranty_basis FROM shop_settings WHERE id = 1", default="sale")
        )
        for serial in db.query("SELECT * FROM serials WHERE sale_id = ?", (sale_id,)):
            months = int(serial["warranty_months"] or 0)
            expiry = serial["warranty_expiry"]
            if basis == "sale" and months and serial["purchase_date"]:
                expiry = repo.add_months(
                    dt.date.fromisoformat(str(serial["purchase_date"])[:10]), months
                ).isoformat()
            db.execute(
                """
                UPDATE serials
                   SET status = 'in_stock', sale_id = NULL, sale_item_id = NULL,
                       sold_at = NULL, warranty_expiry = ?
                 WHERE id = ?
                """,
                (expiry, int(serial["id"])),
            )

        db.execute(
            """
            UPDATE sales
               SET is_void = 1, void_reason = ?, voided_at = datetime('now', 'localtime')
             WHERE id = ?
            """,
            (reason.strip() or "Cancelled", sale_id),
        )
        repo.audit(
            "sale.void",
            entity="sale",
            entity_id=sale_id,
            detail=f"{sale['invoice_number']} — {reason.strip()}",
            user_id=user_id,
        )


# -- purchases ---------------------------------------------------------------


@dataclass(slots=True)
class PurchaseTaxInput:
    """GST exactly as printed on the dealer's bill.

    The app suggests these from the line items but stores what the operator confirms: the
    purchase register must mirror the physical invoice, not our recalculation of it.
    """

    taxable_paise: int
    cgst_paise: int = 0
    sgst_paise: int = 0
    igst_paise: int = 0
    round_off_paise: int = 0
    total_paise: int = 0


def create_purchase(
    *,
    dealer_id: int,
    bill_number: str,
    bill_date: str,
    lines: list[LineInput],
    taxes: PurchaseTaxInput,
    notes: str = "",
    update_cost_prices: bool = True,
    user_id: int | None = None,
) -> dict[str, Any]:
    """Record a dealer bill, add stock and register any serial numbers."""
    if not lines:
        raise DocumentError("Add at least one item before saving")

    dealer = repo.get_dealer(dealer_id)
    if dealer is None:
        raise DocumentError("Pick a dealer from the list")

    iso_date = repo.parse_date(bill_date, field="Bill date")
    date_obj = dt.date.fromisoformat(iso_date)
    if date_obj > dt.date.today():
        raise DocumentError("Bill date cannot be in the future")

    shop_state = repo.shop_state_code()
    dealer_state = gst.resolve_state_code(dealer["state_code"], dealer["gstin"], shop_state)
    interstate = gst.is_interstate(shop_state, dealer_state)

    bill_number = (bill_number or "").strip()
    if bill_number:
        clash = db.query_one(
            """
            SELECT id FROM purchases
             WHERE dealer_id = ? AND bill_number = ? COLLATE NOCASE
            """,
            (dealer_id, bill_number),
        )
        if clash is not None:
            raise DocumentError(
                f"Bill #{bill_number} from {dealer['name']} is already entered "
                f"(purchase #{clash['id']})"
            )

    # Per-line tax is informational: it lets the detail screen flag a mismatch against the
    # dealer's printed totals instead of silently disagreeing with them.
    per_line = [
        gst.compute_line(
            qty=line.qty,
            unit_price_paise=line.unit_price_paise,
            rate_bp=line.gst_rate_bp,
            interstate=interstate,
            hsn_code=line.hsn_code,
        )
        for line in lines
    ]

    with db.transaction():
        purchase_id = db.insert(
            """
            INSERT INTO purchases (
                dealer_id, bill_number, bill_date, interstate,
                taxable_paise, cgst_paise, sgst_paise, igst_paise,
                round_off_paise, total_paise, notes, created_by
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                dealer_id,
                bill_number,
                iso_date,
                int(interstate),
                taxes.taxable_paise,
                taxes.cgst_paise,
                taxes.sgst_paise,
                taxes.igst_paise,
                taxes.round_off_paise,
                taxes.total_paise,
                notes.strip(),
                user_id,
            ),
        )

        for line, tax in zip(lines, per_line, strict=True):
            item_id = db.insert(
                """
                INSERT INTO purchase_items (
                    purchase_id, product_id, description, hsn_code, qty, unit_cost_paise,
                    gst_rate_bp, taxable_paise, cgst_paise, sgst_paise, igst_paise, total_paise
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    purchase_id,
                    line.product_id,
                    line.description,
                    tax.hsn_code,
                    line.qty,
                    line.unit_price_paise,
                    line.gst_rate_bp,
                    tax.taxable_paise,
                    tax.cgst_paise,
                    tax.sgst_paise,
                    tax.igst_paise,
                    tax.total_paise,
                ),
            )
            repo.apply_stock_movement(
                product_id=line.product_id,
                delta=line.qty,
                ref_type="purchase",
                ref_id=purchase_id,
                note=f"{dealer['name']} bill {bill_number or '(no number)'}",
                user_id=user_id,
            )
            if update_cost_prices and line.unit_price_paise > 0:
                db.execute(
                    "UPDATE products SET cost_price_paise = ?, updated_at = datetime('now', 'localtime') WHERE id = ?",
                    (line.unit_price_paise, line.product_id),
                )
            for serial in line.serials:
                _register_serial(
                    serial_no=serial,
                    product_id=line.product_id,
                    purchase_id=purchase_id,
                    purchase_item_id=item_id,
                    purchase_date=iso_date,
                    warranty_months=line.warranty_months,
                )

        repo.audit(
            "purchase.create",
            entity="purchase",
            entity_id=purchase_id,
            detail=f"{dealer['name']} bill {bill_number or '(none)'} — {money.fmt_money(taxes.total_paise)}",
            user_id=user_id,
        )

    return {"purchase_id": purchase_id, "interstate": interstate}


def _register_serial(
    *,
    serial_no: str,
    product_id: int,
    purchase_id: int,
    purchase_item_id: int,
    purchase_date: str,
    warranty_months: int,
) -> None:
    existing = db.query_one(
        "SELECT id, status FROM serials WHERE serial_no = ? COLLATE NOCASE", (serial_no,)
    )
    if existing is not None:
        raise DocumentError(
            f"Serial '{serial_no}' is already recorded (currently '{existing['status']}')"
        )
    expiry = (
        repo.add_months(dt.date.fromisoformat(purchase_date), warranty_months).isoformat()
        if warranty_months
        else None
    )
    db.execute(
        """
        INSERT INTO serials (
            product_id, serial_no, purchase_id, purchase_item_id, purchase_date,
            warranty_months, warranty_expiry, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'in_stock')
        """,
        (product_id, serial_no, purchase_id, purchase_item_id, purchase_date, warranty_months, expiry),
    )


def void_purchase(purchase_id: int, *, reason: str, user_id: int | None = None) -> None:
    """Cancel a purchase, removing the stock it added.

    Refuses when any of its serials have already been sold, because reversing the stock
    would then contradict the sale that used it.
    """
    with db.transaction():
        purchase = db.query_one("SELECT * FROM purchases WHERE id = ?", (purchase_id,))
        if purchase is None:
            raise DocumentError("That purchase no longer exists")
        if int(purchase["is_void"]):
            raise DocumentError("That purchase is already cancelled")

        sold = db.query(
            "SELECT serial_no FROM serials WHERE purchase_id = ? AND status != 'in_stock'",
            (purchase_id,),
        )
        if sold:
            listed = ", ".join(r["serial_no"] for r in sold[:5])
            raise DocumentError(
                f"Cannot cancel: serial(s) from this bill have already left stock ({listed}). "
                "Cancel the sale first."
            )

        for item in db.query("SELECT * FROM purchase_items WHERE purchase_id = ?", (purchase_id,)):
            try:
                repo.apply_stock_movement(
                    product_id=int(item["product_id"]),
                    delta=-int(item["qty"]),
                    ref_type="purchase_void",
                    ref_id=purchase_id,
                    note=f"Cancelled bill {purchase['bill_number'] or '(no number)'}",
                    user_id=user_id,
                )
            except repo.StockError as exc:
                raise DocumentError(
                    f"Cannot cancel this bill: {exc}. Some of the stock has already been sold."
                ) from exc

        db.execute("DELETE FROM serials WHERE purchase_id = ? AND status = 'in_stock'", (purchase_id,))
        db.execute(
            """
            UPDATE purchases
               SET is_void = 1, void_reason = ?, voided_at = datetime('now', 'localtime')
             WHERE id = ?
            """,
            (reason.strip() or "Cancelled", purchase_id),
        )
        repo.audit(
            "purchase.void",
            entity="purchase",
            entity_id=purchase_id,
            detail=f"bill {purchase['bill_number'] or '(none)'} — {reason.strip()}",
            user_id=user_id,
        )


# -- service jobs ------------------------------------------------------------


def next_job_number(job_date: dt.date, conn: db.Connection) -> str:
    label = gst.fy_label(job_date)
    sequence = migrations.next_counter(f"service:{label}", conn)
    return f"JOB/{label}/{sequence:04d}"


def create_service_job(
    *,
    job_date: str,
    customer_name: str,
    customer_phone: str = "",
    customer_address: str = "",
    customer_state_code: str = "",
    description: str = "",
    amount_paise: int = 0,
    sac_code: str = "",
    gst_rate_bp: int = 0,
    status: str = "pending",
    notes: str = "",
    parts: list[LineInput] | None = None,
    user_id: int | None = None,
) -> dict[str, Any]:
    """Record an installation/service job, issuing any parts used out of stock."""
    if not customer_name.strip():
        raise DocumentError("Customer name is required")
    if status not in {"pending", "completed", "cancelled"}:
        raise DocumentError("Status must be pending, completed or cancelled")

    iso_date = repo.parse_date(job_date, field="Job date")
    date_obj = dt.date.fromisoformat(iso_date)
    parts = parts or []

    shop_state = repo.shop_state_code()
    party_state = gst.resolve_state_code(customer_state_code, "", shop_state)
    interstate = gst.is_interstate(shop_state, party_state)

    service_tax = gst.compute_line(
        qty=1,
        unit_price_paise=amount_paise,
        rate_bp=gst_rate_bp,
        interstate=interstate,
    ) if amount_paise else None

    parts_total = sum(line.qty * line.unit_price_paise for line in parts)
    total = (service_tax.total_paise if service_tax else 0) + parts_total

    with db.transaction() as conn:
        job_number = next_job_number(date_obj, conn)
        job_id = db.insert(
            """
            INSERT INTO service_jobs (
                job_number, customer_name, customer_phone, customer_address,
                customer_state_code, job_date, description, sac_code, amount_paise,
                gst_rate_bp, cgst_paise, sgst_paise, igst_paise,
                parts_total_paise, total_paise, status, notes, completed_at, created_by
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_number,
                customer_name.strip(),
                customer_phone.strip(),
                customer_address.strip(),
                party_state,
                iso_date,
                description.strip(),
                sac_code.strip(),
                amount_paise,
                gst_rate_bp,
                service_tax.cgst_paise if service_tax else 0,
                service_tax.sgst_paise if service_tax else 0,
                service_tax.igst_paise if service_tax else 0,
                parts_total,
                total,
                status,
                notes.strip(),
                dt.datetime.now().isoformat(timespec="seconds") if status == "completed" else None,
                user_id,
            ),
        )
        for line in parts:
            db.insert(
                """
                INSERT INTO service_job_parts (job_id, product_id, description, qty, unit_price_paise, total_paise)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    line.product_id,
                    line.description,
                    line.qty,
                    line.unit_price_paise,
                    line.qty * line.unit_price_paise,
                ),
            )
            try:
                repo.apply_stock_movement(
                    product_id=line.product_id,
                    delta=-line.qty,
                    ref_type="service",
                    ref_id=job_id,
                    note=job_number,
                    user_id=user_id,
                )
            except repo.StockError as exc:
                raise DocumentError(str(exc)) from exc

        repo.audit(
            "service.create",
            entity="service_job",
            entity_id=job_id,
            detail=f"{job_number} — {customer_name.strip()}",
            user_id=user_id,
        )
    return {"job_id": job_id, "job_number": job_number, "total_paise": total}


def set_service_status(job_id: int, status: str, *, user_id: int | None = None) -> None:
    if status not in {"pending", "completed", "cancelled"}:
        raise DocumentError("Status must be pending, completed or cancelled")
    with db.transaction():
        job = db.query_one("SELECT * FROM service_jobs WHERE id = ?", (job_id,))
        if job is None:
            raise DocumentError("That job no longer exists")
        if status == "cancelled" and job["status"] != "cancelled":
            # Return any parts that were issued for the job.
            for part in db.query("SELECT * FROM service_job_parts WHERE job_id = ?", (job_id,)):
                repo.apply_stock_movement(
                    product_id=int(part["product_id"]),
                    delta=int(part["qty"]),
                    ref_type="service_void",
                    ref_id=job_id,
                    note=f"Cancelled {job['job_number']}",
                    user_id=user_id,
                )
        db.execute(
            "UPDATE service_jobs SET status = ?, completed_at = ? WHERE id = ?",
            (
                status,
                dt.datetime.now().isoformat(timespec="seconds") if status == "completed" else None,
                job_id,
            ),
        )
        repo.audit(
            "service.status",
            entity="service_job",
            entity_id=job_id,
            detail=f"{job['job_number']} -> {status}",
            user_id=user_id,
        )
