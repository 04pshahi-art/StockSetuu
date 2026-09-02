"""Reports: stock, sales, purchases, dealer-wise history and the two GST registers.

Deliberately narrow — these exist to answer the shop's daily questions and to make return
filing on the GST portal easier. Nothing here files anything.
"""

from __future__ import annotations

import csv
import datetime as dt
import io

from fastapi import APIRouter, Request
from starlette.responses import Response

from .. import db, gst, money, repo, tally_export
from ..deps import redirect, render, require_user

router = APIRouter(prefix="/reports")


def _xml_response(filename: str, xml_text: str) -> Response:
    return Response(
        xml_text,
        media_type="application/xml; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _tally_export_data(date_from: str, date_to: str):
    """Gather everything the Tally export needs for one date range, pre-joined."""
    sales = db.query(
        "SELECT * FROM sales WHERE invoice_date BETWEEN ? AND ? AND is_void = 0 ORDER BY invoice_seq",
        (date_from, date_to),
    )
    purchases = db.query(
        "SELECT * FROM purchases WHERE bill_date BETWEEN ? AND ? AND is_void = 0 ORDER BY bill_date",
        (date_from, date_to),
    )

    sale_ids = [int(s["id"]) for s in sales]
    purchase_ids = [int(p["id"]) for p in purchases]

    def _items_by(table: str, fk: str, ids: list[int]) -> dict[int, list[dict]]:
        if not ids:
            return {}
        placeholders = ",".join("?" * len(ids))
        rows = db.query(
            f"""
            SELECT it.*, pr.name AS _product_name, pr.sku AS _sku, pr.unit AS _unit
              FROM {table} it JOIN products pr ON pr.id = it.product_id
             WHERE it.{fk} IN ({placeholders})
            """,
            tuple(ids),
        )
        out: dict[int, list[dict]] = {}
        for row in rows:
            d = dict(row)
            d["_stock_name"] = f"{d['_product_name']} ({d['_sku']})"
            out.setdefault(int(d[fk]), []).append(d)
        return out

    sale_items_by_sale = _items_by("sale_items", "sale_id", sale_ids)
    purchase_items_by_purchase = _items_by("purchase_items", "purchase_id", purchase_ids)

    dealer_ids = {int(p["dealer_id"]) for p in purchases}
    all_dealers = {int(d["id"]): dict(d) for d in db.query("SELECT * FROM dealers")}
    dealers_by_id = {k: v for k, v in all_dealers.items() if k in dealer_ids}

    product_ids: set[int] = set()
    for items in sale_items_by_sale.values():
        product_ids.update(int(it["product_id"]) for it in items)
    for items in purchase_items_by_purchase.values():
        product_ids.update(int(it["product_id"]) for it in items)
    products = [dict(p) for p in db.query("SELECT * FROM products")] if product_ids else []
    products = [p for p in products if int(p["id"]) in product_ids]

    sale_rate_bps = {int(it["gst_rate_bp"]) for items in sale_items_by_sale.values() for it in items}
    purchase_rate_bps = {int(it["gst_rate_bp"]) for items in purchase_items_by_purchase.values() for it in items}

    return {
        "sales": [dict(s) for s in sales],
        "purchases": [dict(p) for p in purchases],
        "sale_items_by_sale": sale_items_by_sale,
        "purchase_items_by_purchase": purchase_items_by_purchase,
        "dealers_by_id": dealers_by_id,
        "products": products,
        "sale_rate_bps": sale_rate_bps,
        "purchase_rate_bps": purchase_rate_bps,
    }


@router.get("/tally/masters")
def tally_masters_export(request: Request):
    require_user(request)
    date_from, date_to = _range(request)
    shop = repo.get_shop_settings()
    data = _tally_export_data(date_from, date_to)
    xml_text = tally_export.build_masters_xml(
        company_name=shop.get("trade_name") or shop.get("legal_name") or "Company",
        products=data["products"],
        sales=data["sales"],
        purchases=data["purchases"],
        dealers_by_id=data["dealers_by_id"],
        sale_rate_bps=data["sale_rate_bps"],
        purchase_rate_bps=data["purchase_rate_bps"],
    )
    return _xml_response(f"tally-masters-{date_from}_to_{date_to}.xml", xml_text)


@router.get("/tally/vouchers")
def tally_vouchers_export(request: Request):
    require_user(request)
    date_from, date_to = _range(request)
    shop = repo.get_shop_settings()
    data = _tally_export_data(date_from, date_to)
    xml_text = tally_export.build_vouchers_xml(
        company_name=shop.get("trade_name") or shop.get("legal_name") or "Company",
        sales=data["sales"],
        sale_items_by_sale=data["sale_items_by_sale"],
        purchases=data["purchases"],
        purchase_items_by_purchase=data["purchase_items_by_purchase"],
        dealers_by_id=data["dealers_by_id"],
    )
    return _xml_response(f"tally-vouchers-{date_from}_to_{date_to}.xml", xml_text)


def _month_bounds(today: dt.date | None = None) -> tuple[str, str]:
    today = today or dt.date.today()
    first = today.replace(day=1)
    return first.isoformat(), today.isoformat()


def _range(request: Request) -> tuple[str, str]:
    """Read ?from/?to, defaulting to the current month to date."""
    default_from, default_to = _month_bounds()
    date_from = request.query_params.get("from", "").strip() or default_from
    date_to = request.query_params.get("to", "").strip() or default_to
    try:
        dt.date.fromisoformat(date_from)
        dt.date.fromisoformat(date_to)
    except ValueError:
        return default_from, default_to
    if date_from > date_to:
        date_from, date_to = date_to, date_from
    return date_from, date_to


def _csv_response(filename: str, header: list[str], rows: list[list[object]]) -> Response:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(header)
    writer.writerows(rows)
    # A UTF-8 BOM makes Excel on Windows open the ₹ sign and Indian names correctly.
    return Response(
        "﻿" + buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("")
def index(request: Request):
    require_user(request)
    date_from, date_to = _month_bounds()
    return render(request, "reports/index.html", date_from=date_from, date_to=date_to)


# -- stock -------------------------------------------------------------------


@router.get("/stock")
def stock_report(request: Request):
    require_user(request)
    category = request.query_params.get("category", "").strip()
    only = request.query_params.get("only", "").strip()

    where = ["is_active = 1"]
    params: list[object] = []
    if category:
        where.append("category = ?")
        params.append(category)
    if only == "low":
        where.append("quantity <= low_stock_threshold")
    elif only == "out":
        where.append("quantity <= 0")
    clause = " AND ".join(where)

    rows = db.query(
        f"""
        SELECT * FROM products
         WHERE {clause}
         ORDER BY (quantity <= low_stock_threshold) DESC, category, name COLLATE NOCASE
        """,
        params,
    )
    totals = {
        "items": len(rows),
        "units": sum(int(r["quantity"]) for r in rows),
        "cost_value": sum(int(r["quantity"]) * int(r["cost_price_paise"]) for r in rows),
        "sale_value": sum(int(r["quantity"]) * int(r["sale_price_paise"]) for r in rows),
        "low": sum(1 for r in rows if int(r["quantity"]) <= int(r["low_stock_threshold"])),
        "out": sum(1 for r in rows if int(r["quantity"]) <= 0),
    }

    if request.query_params.get("format") == "csv":
        return _csv_response(
            "stock.csv",
            ["SKU", "Name", "Category", "Brand", "HSN", "GST %", "Qty", "Low Stock",
             "Cost", "Sale", "Stock Value (Cost)"],
            [
                [
                    r["sku"], r["name"], r["category"], r["brand"], r["hsn_code"],
                    money.fmt_rate(int(r["gst_rate_bp"])).rstrip("%"),
                    r["quantity"], r["low_stock_threshold"],
                    money.rupees(int(r["cost_price_paise"])),
                    money.rupees(int(r["sale_price_paise"])),
                    money.rupees(int(r["quantity"]) * int(r["cost_price_paise"])),
                ]
                for r in rows
            ],
        )

    return render(
        request,
        "reports/stock.html",
        products=rows,
        totals=totals,
        categories=repo.list_categories(),
        category=category,
        only=only,
    )


# -- sales / purchases -------------------------------------------------------


@router.get("/sales")
def sales_report(request: Request):
    require_user(request)
    date_from, date_to = _range(request)
    rows = db.query(
        """
        SELECT s.*, (SELECT count(*) FROM sale_items si WHERE si.sale_id = s.id) AS item_count
          FROM sales s
         WHERE s.invoice_date BETWEEN ? AND ?
         ORDER BY s.invoice_date, s.invoice_seq
        """,
        (date_from, date_to),
    )
    live = [r for r in rows if not int(r["is_void"])]
    totals = {
        "count": len(live),
        "cancelled": len(rows) - len(live),
        "taxable": sum(int(r["taxable_paise"]) for r in live),
        "tax": sum(int(r["cgst_paise"]) + int(r["sgst_paise"]) + int(r["igst_paise"]) for r in live),
        "total": sum(int(r["total_paise"]) for r in live),
    }
    by_mode = db.query(
        """
        SELECT payment_mode, count(*) AS n, COALESCE(sum(total_paise), 0) AS total
          FROM sales
         WHERE invoice_date BETWEEN ? AND ? AND is_void = 0
         GROUP BY payment_mode ORDER BY total DESC
        """,
        (date_from, date_to),
    )
    top_products = db.query(
        """
        SELECT pr.sku, pr.name, sum(si.qty) AS qty,
               COALESCE(sum(si.taxable_paise), 0) AS taxable
          FROM sale_items si
          JOIN sales s ON s.id = si.sale_id
          JOIN products pr ON pr.id = si.product_id
         WHERE s.invoice_date BETWEEN ? AND ? AND s.is_void = 0
         GROUP BY pr.id ORDER BY taxable DESC LIMIT 15
        """,
        (date_from, date_to),
    )
    if request.query_params.get("format") == "csv":
        return _csv_response(
            f"sales_{date_from}_to_{date_to}.csv",
            ["Date", "Invoice", "Customer", "Phone", "Payment", "Taxable", "CGST", "SGST", "IGST",
             "Round Off", "Total", "Status"],
            [
                [
                    r["invoice_date"], r["invoice_number"], r["customer_name"], r["customer_phone"],
                    r["payment_mode"], money.rupees(int(r["taxable_paise"])),
                    money.rupees(int(r["cgst_paise"])), money.rupees(int(r["sgst_paise"])),
                    money.rupees(int(r["igst_paise"])), money.rupees(int(r["round_off_paise"])),
                    money.rupees(int(r["total_paise"])),
                    "CANCELLED" if int(r["is_void"]) else "Active",
                ]
                for r in rows
            ],
        )
    return render(
        request,
        "reports/sales.html",
        sales=rows,
        totals=totals,
        by_mode=by_mode,
        top_products=top_products,
        date_from=date_from,
        date_to=date_to,
    )


@router.get("/purchases")
def purchases_report(request: Request):
    require_user(request)
    date_from, date_to = _range(request)
    dealer_id = request.query_params.get("dealer_id", "").strip()

    where = ["p.bill_date BETWEEN ? AND ?"]
    params: list[object] = [date_from, date_to]
    if dealer_id.isdigit():
        where.append("p.dealer_id = ?")
        params.append(int(dealer_id))
    clause = " AND ".join(where)

    rows = db.query(
        f"""
        SELECT p.*, d.name AS dealer_name, d.gstin AS dealer_gstin, d.state_code AS dealer_state,
               (SELECT count(*) FROM purchase_items pi WHERE pi.purchase_id = p.id) AS item_count
          FROM purchases p JOIN dealers d ON d.id = p.dealer_id
         WHERE {clause}
         ORDER BY p.bill_date, p.id
        """,
        params,
    )
    live = [r for r in rows if not int(r["is_void"])]
    totals = {
        "count": len(live),
        "cancelled": len(rows) - len(live),
        "taxable": sum(int(r["taxable_paise"]) for r in live),
        "tax": sum(int(r["cgst_paise"]) + int(r["sgst_paise"]) + int(r["igst_paise"]) for r in live),
        "total": sum(int(r["total_paise"]) for r in live),
    }
    by_dealer = db.query(
        f"""
        SELECT d.id, d.name, count(*) AS bills,
               COALESCE(sum(p.taxable_paise), 0) AS taxable,
               COALESCE(sum(p.total_paise), 0) AS total
          FROM purchases p JOIN dealers d ON d.id = p.dealer_id
         WHERE {clause} AND p.is_void = 0
         GROUP BY d.id ORDER BY total DESC
        """,
        params,
    )
    if request.query_params.get("format") == "csv":
        return _csv_response(
            f"purchases_{date_from}_to_{date_to}.csv",
            ["Bill Date", "Dealer", "Dealer GSTIN", "Bill No", "Taxable", "CGST", "SGST", "IGST",
             "Round Off", "Total", "Status"],
            [
                [
                    r["bill_date"], r["dealer_name"], r["dealer_gstin"], r["bill_number"],
                    money.rupees(int(r["taxable_paise"])), money.rupees(int(r["cgst_paise"])),
                    money.rupees(int(r["sgst_paise"])), money.rupees(int(r["igst_paise"])),
                    money.rupees(int(r["round_off_paise"])), money.rupees(int(r["total_paise"])),
                    "CANCELLED" if int(r["is_void"]) else "Active",
                ]
                for r in rows
            ],
        )
    return render(
        request,
        "reports/purchases.html",
        purchases=rows,
        totals=totals,
        by_dealer=by_dealer,
        dealers=repo.list_dealers(),
        dealer_id=dealer_id,
        date_from=date_from,
        date_to=date_to,
    )


@router.get("/dealers")
def dealer_history(request: Request):
    """Dealer-wise purchase history, and price comparison for the same item across dealers."""
    require_user(request)
    date_from, date_to = _range(request)
    product_id = request.query_params.get("product_id", "").strip()

    dealers = db.query(
        """
        SELECT d.id, d.name, d.gstin, d.contact_number, d.state_code,
               count(p.id) AS bills,
               COALESCE(sum(p.total_paise), 0) AS total,
               max(p.bill_date) AS last_bill
          FROM dealers d
          LEFT JOIN purchases p
                 ON p.dealer_id = d.id AND p.is_void = 0 AND p.bill_date BETWEEN ? AND ?
         GROUP BY d.id
         ORDER BY total DESC, d.name COLLATE NOCASE
        """,
        (date_from, date_to),
    )

    comparison = []
    selected_product = None
    if product_id.isdigit():
        selected_product = repo.get_product(int(product_id))
        comparison = db.query(
            """
            SELECT d.id AS dealer_id, d.name AS dealer_name, p.bill_number, p.bill_date,
                   pi.qty, pi.unit_cost_paise, pi.gst_rate_bp
              FROM purchase_items pi
              JOIN purchases p ON p.id = pi.purchase_id
              JOIN dealers d ON d.id = p.dealer_id
             WHERE pi.product_id = ? AND p.is_void = 0
             ORDER BY pi.unit_cost_paise, p.bill_date DESC
            """,
            (int(product_id),),
        )

    return render(
        request,
        "reports/dealers.html",
        dealers=dealers,
        date_from=date_from,
        date_to=date_to,
        products=db.query(
            """
            SELECT DISTINCT pr.id, pr.sku, pr.name
              FROM products pr JOIN purchase_items pi ON pi.product_id = pr.id
             ORDER BY pr.name COLLATE NOCASE
            """
        ),
        product_id=product_id,
        selected_product=selected_product,
        comparison=comparison,
    )


# -- GST registers -----------------------------------------------------------


@router.get("/gst/sales")
def gst_sales_register(request: Request):
    """Sales register laid out to make GSTR-1 data entry straightforward."""
    require_user(request)
    date_from, date_to = _range(request)
    shop = repo.get_shop_settings()

    rows = db.query(
        """
        SELECT * FROM sales
         WHERE invoice_date BETWEEN ? AND ?
         ORDER BY fy_label, invoice_seq
        """,
        (date_from, date_to),
    )
    live = [r for r in rows if not int(r["is_void"])]

    totals = {
        "taxable": sum(int(r["taxable_paise"]) for r in live),
        "cgst": sum(int(r["cgst_paise"]) for r in live),
        "sgst": sum(int(r["sgst_paise"]) for r in live),
        "igst": sum(int(r["igst_paise"]) for r in live),
        "round_off": sum(int(r["round_off_paise"]) for r in live),
        "total": sum(int(r["total_paise"]) for r in live),
        "count": len(live),
        "cancelled": len(rows) - len(live),
    }

    # B2B (registered buyer) vs B2C — GSTR-1 reports these in different tables.
    b2b = [r for r in live if (r["customer_gstin"] or "").strip()]
    b2c = [r for r in live if not (r["customer_gstin"] or "").strip()]
    split = {
        "b2b_count": len(b2b),
        "b2b_taxable": sum(int(r["taxable_paise"]) for r in b2b),
        "b2b_tax": sum(int(r["cgst_paise"]) + int(r["sgst_paise"]) + int(r["igst_paise"]) for r in b2b),
        "b2c_count": len(b2c),
        "b2c_taxable": sum(int(r["taxable_paise"]) for r in b2c),
        "b2c_tax": sum(int(r["cgst_paise"]) + int(r["sgst_paise"]) + int(r["igst_paise"]) for r in b2c),
    }

    rate_rows = db.query(
        """
        SELECT si.gst_rate_bp AS rate_bp,
               COALESCE(sum(si.taxable_paise), 0) AS taxable,
               COALESCE(sum(si.cgst_paise), 0) AS cgst,
               COALESCE(sum(si.sgst_paise), 0) AS sgst,
               COALESCE(sum(si.igst_paise), 0) AS igst
          FROM sale_items si JOIN sales s ON s.id = si.sale_id
         WHERE s.invoice_date BETWEEN ? AND ? AND s.is_void = 0
         GROUP BY si.gst_rate_bp ORDER BY si.gst_rate_bp
        """,
        (date_from, date_to),
    )
    hsn_rows = db.query(
        """
        SELECT COALESCE(NULLIF(si.hsn_code, ''), '—') AS hsn_code, si.gst_rate_bp AS rate_bp,
               sum(si.qty) AS qty, pr.unit AS unit,
               COALESCE(sum(si.taxable_paise), 0) AS taxable,
               COALESCE(sum(si.cgst_paise), 0) AS cgst,
               COALESCE(sum(si.sgst_paise), 0) AS sgst,
               COALESCE(sum(si.igst_paise), 0) AS igst
          FROM sale_items si
          JOIN sales s ON s.id = si.sale_id
          JOIN products pr ON pr.id = si.product_id
         WHERE s.invoice_date BETWEEN ? AND ? AND s.is_void = 0
         -- Group on the expression, not the output alias: both sale_items and products
         -- have an hsn_code column, so the bare name is ambiguous to SQLite.
         GROUP BY COALESCE(NULLIF(si.hsn_code, ''), '—'), si.gst_rate_bp
         ORDER BY COALESCE(NULLIF(si.hsn_code, ''), '—'), si.gst_rate_bp
        """,
        (date_from, date_to),
    )
    state_rows = db.query(
        """
        SELECT customer_state_code AS code, count(*) AS invoices,
               COALESCE(sum(taxable_paise), 0) AS taxable,
               COALESCE(sum(igst_paise), 0) AS igst
          FROM sales
         WHERE invoice_date BETWEEN ? AND ? AND is_void = 0
         GROUP BY customer_state_code ORDER BY taxable DESC
        """,
        (date_from, date_to),
    )
    # Service charges are billed outside the invoice series, so they are reported
    # separately rather than folded into the figures above.
    service_rows = db.query_one(
        """
        SELECT COALESCE(sum(amount_paise), 0) AS taxable,
               COALESCE(sum(cgst_paise), 0) AS cgst,
               COALESCE(sum(sgst_paise), 0) AS sgst,
               COALESCE(sum(igst_paise), 0) AS igst,
               count(*) AS jobs
          FROM service_jobs
         WHERE job_date BETWEEN ? AND ? AND status != 'cancelled' AND amount_paise > 0
        """,
        (date_from, date_to),
    )

    if request.query_params.get("format") == "csv":
        return _csv_response(
            f"gst_sales_register_{date_from}_to_{date_to}.csv",
            ["Invoice Date", "Invoice No", "Buyer", "Buyer GSTIN", "Place of Supply",
             "Supply Type", "Taxable Value", "CGST", "SGST", "IGST", "Round Off",
             "Invoice Total", "Status"],
            [
                [
                    r["invoice_date"], r["invoice_number"], r["customer_name"],
                    r["customer_gstin"], gst.state_label(r["customer_state_code"]),
                    "Inter-State" if int(r["interstate"]) else "Intra-State",
                    money.rupees(int(r["taxable_paise"])), money.rupees(int(r["cgst_paise"])),
                    money.rupees(int(r["sgst_paise"])), money.rupees(int(r["igst_paise"])),
                    money.rupees(int(r["round_off_paise"])), money.rupees(int(r["total_paise"])),
                    "CANCELLED" if int(r["is_void"]) else "Active",
                ]
                for r in rows
            ],
        )

    return render(
        request,
        "reports/gst_sales.html",
        sales=rows,
        totals=totals,
        split=split,
        rate_rows=rate_rows,
        hsn_rows=hsn_rows,
        state_rows=state_rows,
        service_rows=service_rows,
        date_from=date_from,
        date_to=date_to,
        shop=shop,
    )


@router.get("/gst/purchases")
def gst_purchase_register(request: Request):
    """Purchase register for input-tax-credit reference."""
    require_user(request)
    date_from, date_to = _range(request)

    rows = db.query(
        """
        SELECT p.*, d.name AS dealer_name, d.gstin AS dealer_gstin, d.state_code AS dealer_state
          FROM purchases p JOIN dealers d ON d.id = p.dealer_id
         WHERE p.bill_date BETWEEN ? AND ?
         ORDER BY p.bill_date, p.id
        """,
        (date_from, date_to),
    )
    live = [r for r in rows if not int(r["is_void"])]
    totals = {
        "taxable": sum(int(r["taxable_paise"]) for r in live),
        "cgst": sum(int(r["cgst_paise"]) for r in live),
        "sgst": sum(int(r["sgst_paise"]) for r in live),
        "igst": sum(int(r["igst_paise"]) for r in live),
        "total": sum(int(r["total_paise"]) for r in live),
        "count": len(live),
        "cancelled": len(rows) - len(live),
    }
    # Bills from a dealer with no GSTIN on file cannot support an ITC claim.
    no_gstin = [r for r in live if not (r["dealer_gstin"] or "").strip()]
    unclaimable = {
        "count": len(no_gstin),
        "tax": sum(int(r["cgst_paise"]) + int(r["sgst_paise"]) + int(r["igst_paise"]) for r in no_gstin),
    }
    by_dealer = db.query(
        """
        SELECT d.name, d.gstin, count(*) AS bills,
               COALESCE(sum(p.taxable_paise), 0) AS taxable,
               COALESCE(sum(p.cgst_paise), 0) AS cgst,
               COALESCE(sum(p.sgst_paise), 0) AS sgst,
               COALESCE(sum(p.igst_paise), 0) AS igst,
               COALESCE(sum(p.total_paise), 0) AS total
          FROM purchases p JOIN dealers d ON d.id = p.dealer_id
         WHERE p.bill_date BETWEEN ? AND ? AND p.is_void = 0
         GROUP BY d.id ORDER BY total DESC
        """,
        (date_from, date_to),
    )

    if request.query_params.get("format") == "csv":
        return _csv_response(
            f"gst_purchase_register_{date_from}_to_{date_to}.csv",
            ["Bill Date", "Dealer", "Dealer GSTIN", "Dealer State", "Bill No", "Supply Type",
             "Taxable Value", "CGST", "SGST", "IGST", "Round Off", "Bill Total", "Status"],
            [
                [
                    r["bill_date"], r["dealer_name"], r["dealer_gstin"],
                    gst.state_label(r["dealer_state"]), r["bill_number"],
                    "Inter-State" if int(r["interstate"]) else "Intra-State",
                    money.rupees(int(r["taxable_paise"])), money.rupees(int(r["cgst_paise"])),
                    money.rupees(int(r["sgst_paise"])), money.rupees(int(r["igst_paise"])),
                    money.rupees(int(r["round_off_paise"])), money.rupees(int(r["total_paise"])),
                    "CANCELLED" if int(r["is_void"]) else "Active",
                ]
                for r in rows
            ],
        )

    return render(
        request,
        "reports/gst_purchases.html",
        purchases=rows,
        totals=totals,
        unclaimable=unclaimable,
        by_dealer=by_dealer,
        date_from=date_from,
        date_to=date_to,
    )


@router.get("/audit")
def audit_trail(request: Request):
    require_user(request)
    rows = db.query(
        """
        SELECT a.*, u.display_name AS user_name, u.username
          FROM audit_log a LEFT JOIN users u ON u.id = a.user_id
         ORDER BY a.id DESC LIMIT 300
        """
    )
    return render(request, "reports/audit.html", entries=rows)
