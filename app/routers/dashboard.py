"""Home screen — the handful of numbers worth seeing on opening the app."""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse, PlainTextResponse

from .. import db, migrations, repo
from ..deps import render
from ..session import SessionUser

router = APIRouter()


@router.get("/healthz")
def healthz():
    """Liveness check — also the endpoint to watch from the Windows service wrapper."""
    try:
        version = migrations.current_version(db.get_connection())
    except Exception as exc:  # noqa: BLE001 - the whole point is to report any failure
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=503)
    return JSONResponse({"ok": True, "schema_version": version, **repo.storage_status()})


@router.get("/favicon.ico")
def favicon():
    # A 204 keeps the log clean without shipping an icon file.
    return PlainTextResponse("", status_code=204)


@router.get("/")
def dashboard(request: Request):
    user: SessionUser | None = getattr(request.state, "user", None)
    if user is None:
        from ..deps import redirect

        return redirect("/login")

    today = dt.date.today()
    month_start = today.replace(day=1).isoformat()
    today_iso = today.isoformat()
    soon = (today + dt.timedelta(days=30)).isoformat()

    def sales_span(date_from: str, date_to: str):
        return db.query_one(
            """
            SELECT count(*) AS invoices,
                   COALESCE(sum(taxable_paise), 0) AS taxable,
                   COALESCE(sum(cgst_paise + sgst_paise + igst_paise), 0) AS tax,
                   COALESCE(sum(total_paise), 0) AS total
              FROM sales
             WHERE invoice_date BETWEEN ? AND ? AND is_void = 0
            """,
            (date_from, date_to),
        )

    stock = db.query_one(
        """
        SELECT count(*) AS products,
               COALESCE(sum(quantity), 0) AS units,
               COALESCE(sum(quantity * cost_price_paise), 0) AS cost_value,
               COALESCE(sum(quantity <= low_stock_threshold), 0) AS low,
               COALESCE(sum(quantity <= 0), 0) AS out
          FROM products WHERE is_active = 1
        """
    )
    low_stock = db.query(
        """
        SELECT id, sku, name, category, quantity, low_stock_threshold
          FROM products
         WHERE is_active = 1 AND quantity <= low_stock_threshold
         ORDER BY (quantity <= 0) DESC, quantity, name COLLATE NOCASE
         LIMIT 12
        """
    )
    recent_sales = db.query(
        """
        SELECT id, invoice_number, invoice_date, customer_name, total_paise, is_void
          FROM sales ORDER BY id DESC LIMIT 8
        """
    )
    pending_jobs = db.query(
        """
        SELECT id, job_number, job_date, customer_name, description, total_paise
          FROM service_jobs WHERE status = 'pending'
         ORDER BY job_date, id LIMIT 8
        """
    )
    expiring = db.query(
        """
        SELECT s.id, s.serial_no, s.warranty_expiry, pr.name AS product_name,
               sa.customer_name, sa.customer_phone
          FROM serials s
          JOIN products pr ON pr.id = s.product_id
          LEFT JOIN sales sa ON sa.id = s.sale_id
         WHERE s.status = 'sold' AND s.warranty_expiry BETWEEN ? AND ?
         ORDER BY s.warranty_expiry LIMIT 10
        """,
        (today_iso, soon),
    )
    month_purchases = db.query_one(
        """
        SELECT count(*) AS bills, COALESCE(sum(total_paise), 0) AS total
          FROM purchases WHERE bill_date BETWEEN ? AND ? AND is_void = 0
        """,
        (month_start, today_iso),
    )
    counts = db.query_one(
        """
        SELECT (SELECT count(*) FROM dealers WHERE is_active = 1) AS dealers,
               (SELECT count(*) FROM serials WHERE status = 'in_stock') AS serials_in_stock,
               (SELECT count(*) FROM service_jobs WHERE status = 'pending') AS jobs_pending
        """
    )

    return render(
        request,
        "dashboard.html",
        today_sales=sales_span(today_iso, today_iso),
        month_sales=sales_span(month_start, today_iso),
        month_purchases=month_purchases,
        stock=stock,
        counts=counts,
        low_stock=low_stock,
        recent_sales=recent_sales,
        pending_jobs=pending_jobs,
        expiring=expiring,
        month_start=month_start,
    )
