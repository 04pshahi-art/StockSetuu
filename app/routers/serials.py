"""Serial numbers and warranty tracking, plus the customer-facing warranty lookup."""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Form, Request

from .. import db, repo
from ..deps import redirect, render, require_user

router = APIRouter()


def _decorate(rows) -> list[dict]:
    """Attach expiry status and days remaining to serial rows."""
    today = dt.date.today()
    out = []
    for row in rows:
        record = dict(row)
        expiry = record.get("warranty_expiry")
        days = None
        state = "none"
        if expiry:
            try:
                expiry_date = dt.date.fromisoformat(str(expiry)[:10])
            except ValueError:
                expiry_date = None
            if expiry_date is not None:
                days = (expiry_date - today).days
                if days < 0:
                    state = "expired"
                elif days <= 30:
                    state = "expiring"
                else:
                    state = "active"
        record["days_remaining"] = days
        record["warranty_state"] = state
        out.append(record)
    return out


@router.get("/warranty")
def warranty_lookup(request: Request):
    """Search by serial number, customer name or phone — the three things a walk-in has."""
    require_user(request)
    term = request.query_params.get("q", "").strip()
    results: list[dict] = []
    if term:
        like = f"%{term}%"
        rows = db.query(
            """
            SELECT s.*, pr.sku, pr.name AS product_name, pr.brand,
                   sa.invoice_number, sa.invoice_date, sa.customer_name, sa.customer_phone,
                   sa.id AS sale_ref, sa.is_void AS sale_void
              FROM serials s
              JOIN products pr ON pr.id = s.product_id
              LEFT JOIN sales sa ON sa.id = s.sale_id
             WHERE s.serial_no LIKE ?
                OR sa.customer_name LIKE ?
                OR sa.customer_phone LIKE ?
                OR sa.invoice_number LIKE ?
             ORDER BY (s.status = 'sold') DESC, s.warranty_expiry DESC, s.serial_no
             LIMIT 300
            """,
            (like, like, like, like),
        )
        results = _decorate(rows)

        # Also surface non-serialised purchases by the same customer, so "what did this
        # person buy" is answerable even for items without a serial.
        other_sales = db.query(
            """
            SELECT s.id, s.invoice_number, s.invoice_date, s.customer_name, s.customer_phone,
                   s.total_paise, s.is_void
              FROM sales s
             WHERE (s.customer_name LIKE ? OR s.customer_phone LIKE ? OR s.invoice_number LIKE ?)
             ORDER BY s.invoice_date DESC
             LIMIT 50
            """,
            (like, like, like),
        )
    else:
        other_sales = []

    return render(
        request,
        "warranty/lookup.html",
        term=term,
        results=results,
        other_sales=other_sales,
    )


@router.get("/serials")
def list_serials(request: Request):
    require_user(request)
    status = request.query_params.get("status", "").strip()
    product_id = request.query_params.get("product_id", "").strip()
    expiry_filter = request.query_params.get("expiry", "").strip()
    term = request.query_params.get("q", "").strip()

    where = ["1 = 1"]
    params: list[object] = []
    if status in {"in_stock", "sold", "returned"}:
        where.append("s.status = ?")
        params.append(status)
    if product_id.isdigit():
        where.append("s.product_id = ?")
        params.append(int(product_id))
    if term:
        where.append("(s.serial_no LIKE ? OR pr.name LIKE ? OR pr.sku LIKE ?)")
        like = f"%{term}%"
        params.extend([like, like, like])
    today = dt.date.today().isoformat()
    if expiry_filter == "expired":
        where.append("s.warranty_expiry IS NOT NULL AND s.warranty_expiry < ?")
        params.append(today)
    elif expiry_filter == "soon":
        soon = (dt.date.today() + dt.timedelta(days=30)).isoformat()
        where.append("s.warranty_expiry IS NOT NULL AND s.warranty_expiry BETWEEN ? AND ?")
        params.extend([today, soon])

    rows = db.query(
        f"""
        SELECT s.*, pr.sku, pr.name AS product_name, pr.brand,
               sa.invoice_number, sa.customer_name, sa.invoice_date
          FROM serials s
          JOIN products pr ON pr.id = s.product_id
          LEFT JOIN sales sa ON sa.id = s.sale_id
         WHERE {' AND '.join(where)}
         ORDER BY s.status, s.warranty_expiry IS NULL, s.warranty_expiry, s.serial_no
         LIMIT 500
        """,
        params,
    )
    counts = db.query_one(
        """
        SELECT
          COALESCE(sum(status = 'in_stock'), 0) AS in_stock,
          COALESCE(sum(status = 'sold'), 0) AS sold,
          COALESCE(sum(status = 'returned'), 0) AS returned
        FROM serials
        """
    )
    return render(
        request,
        "warranty/serials.html",
        serials=_decorate(rows),
        counts=counts,
        status=status,
        product_id=product_id,
        expiry_filter=expiry_filter,
        term=term,
        products=db.query(
            "SELECT id, sku, name FROM products WHERE is_serialized = 1 ORDER BY name COLLATE NOCASE"
        ),
    )


@router.get("/serials/{serial_id}")
def view_serial(request: Request, serial_id: int):
    require_user(request)
    row = db.query_one(
        """
        SELECT s.*, pr.sku, pr.name AS product_name, pr.brand, pr.warranty_months AS product_warranty,
               sa.invoice_number, sa.invoice_date, sa.customer_name, sa.customer_phone, sa.is_void AS sale_void,
               pu.bill_number, pu.bill_date, d.name AS dealer_name
          FROM serials s
          JOIN products pr ON pr.id = s.product_id
          LEFT JOIN sales sa ON sa.id = s.sale_id
          LEFT JOIN purchases pu ON pu.id = s.purchase_id
          LEFT JOIN dealers d ON d.id = pu.dealer_id
         WHERE s.id = ?
        """,
        (serial_id,),
    )
    if row is None:
        return redirect("/serials", error="That serial number does not exist.")
    return render(request, "warranty/serial_detail.html", serial=_decorate([row])[0])


@router.post("/serials/{serial_id}")
def update_serial(
    request: Request,
    serial_id: int,
    warranty_months: str = Form("0"),
    warranty_expiry: str = Form(""),
    notes: str = Form(""),
):
    """Adjust a warranty by hand — brands sometimes extend or shorten a specific unit."""
    user = require_user(request)
    row = db.query_one("SELECT * FROM serials WHERE id = ?", (serial_id,))
    if row is None:
        return redirect("/serials", error="That serial number does not exist.")
    try:
        months = int(warranty_months or 0)
        if months < 0:
            raise ValueError
    except ValueError:
        return redirect(f"/serials/{serial_id}", error="Warranty months must be a whole number.")

    expiry = (warranty_expiry or "").strip() or None
    if expiry:
        try:
            expiry = dt.date.fromisoformat(expiry[:10]).isoformat()
        except ValueError:
            return redirect(f"/serials/{serial_id}", error="Warranty expiry must be a valid date.")
    elif months:
        basis_date = row["sold_at"] or row["purchase_date"]
        if basis_date:
            expiry = repo.add_months(dt.date.fromisoformat(str(basis_date)[:10]), months).isoformat()

    with db.transaction():
        db.execute(
            "UPDATE serials SET warranty_months = ?, warranty_expiry = ?, notes = ? WHERE id = ?",
            (months, expiry, notes.strip(), serial_id),
        )
        repo.audit(
            "serial.update",
            entity="serial",
            entity_id=serial_id,
            detail=f"{row['serial_no']} warranty -> {months}m / {expiry or 'none'}",
            user_id=user.id,
        )
    return redirect(f"/serials/{serial_id}", ok="Warranty updated.")


@router.post("/serials/{serial_id}/status")
def set_serial_status(request: Request, serial_id: int, status: str = Form("")):
    """Mark a unit returned (RMA) or back in stock, keeping the stock count in step."""
    user = require_user(request)
    row = db.query_one("SELECT * FROM serials WHERE id = ?", (serial_id,))
    if row is None:
        return redirect("/serials", error="That serial number does not exist.")
    if status not in {"in_stock", "returned"}:
        return redirect(f"/serials/{serial_id}", error="Status must be 'in stock' or 'returned'.")
    if row["status"] == status:
        return redirect(f"/serials/{serial_id}", ok="No change needed.")
    if row["status"] == "sold":
        return redirect(
            f"/serials/{serial_id}",
            error="This unit is linked to an invoice. Cancel that invoice to release the serial.",
        )

    with db.transaction():
        db.execute("UPDATE serials SET status = ? WHERE id = ?", (status, serial_id))
        # 'returned' means the unit went back to the dealer, so it leaves our stock;
        # moving it back to 'in_stock' brings it in again.
        delta = -1 if status == "returned" else 1
        repo.apply_stock_movement(
            product_id=int(row["product_id"]),
            delta=delta,
            ref_type="serial_status",
            ref_id=serial_id,
            note=f"{row['serial_no']} -> {status}",
            user_id=user.id,
            allow_negative=True,
        )
        repo.audit(
            "serial.status",
            entity="serial",
            entity_id=serial_id,
            detail=f"{row['serial_no']}: {row['status']} -> {status}",
            user_id=user.id,
        )
    return redirect(f"/serials/{serial_id}", ok=f"Marked {status.replace('_', ' ')}.")
