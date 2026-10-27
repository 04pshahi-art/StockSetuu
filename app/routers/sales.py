"""Sale entry (stock out) and GST tax invoice generation."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request

from .. import db, documents, gst, money, repo
from ..deps import redirect, render, require_user

router = APIRouter(prefix="/sales")

PAGE_SIZE = 50


@router.get("")
def list_sales(request: Request):
    require_user(request)
    term = request.query_params.get("q", "").strip()
    date_from = request.query_params.get("from", "").strip()
    date_to = request.query_params.get("to", "").strip()
    try:
        page = max(1, int(request.query_params.get("page", "1")))
    except ValueError:
        page = 1

    where = ["1 = 1"]
    params: list[object] = []
    if term:
        where.append(
            "(invoice_number LIKE ? OR customer_name LIKE ? OR customer_phone LIKE ? "
            "OR customer_gstin LIKE ?)"
        )
        like = f"%{term}%"
        params.extend([like, like, like, like])
    if date_from:
        where.append("invoice_date >= ?")
        params.append(date_from)
    if date_to:
        where.append("invoice_date <= ?")
        params.append(date_to)
    clause = " AND ".join(where)

    total = int(db.scalar(f"SELECT count(*) FROM sales WHERE {clause}", params, default=0))
    rows = db.query(
        f"""
        SELECT s.*, (SELECT count(*) FROM sale_items si WHERE si.sale_id = s.id) AS item_count
          FROM sales s
         WHERE {clause}
         ORDER BY s.invoice_date DESC, s.id DESC
         LIMIT ? OFFSET ?
        """,
        [*params, PAGE_SIZE, (page - 1) * PAGE_SIZE],
    )
    summary = db.query_one(
        f"""
        SELECT COALESCE(sum(taxable_paise), 0) AS taxable,
               COALESCE(sum(cgst_paise + sgst_paise + igst_paise), 0) AS tax,
               COALESCE(sum(total_paise), 0) AS total
          FROM sales WHERE {clause} AND is_void = 0
        """,
        params,
    )
    return render(
        request,
        "sales/list.html",
        sales=rows,
        term=term,
        date_from=date_from,
        date_to=date_to,
        summary=summary,
        page=page,
        total=total,
        pages=max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE),
    )


@router.get("/new")
def new_sale(request: Request):
    require_user(request)
    shop = repo.get_shop_settings()
    return render(
        request,
        "sales/form.html",
        shop_state=shop.get("state_code") or gst.HOME_STATE_CODE,
        prices_include_gst=bool(shop.get("default_prices_include_gst")),
        round_to_rupee=bool(shop.get("round_invoice_to_rupee")),
    )


def _load_sale(sale_id: int):
    sale = db.query_one("SELECT * FROM sales WHERE id = ?", (sale_id,))
    if sale is None:
        return None, [], []
    items = db.query(
        """
        SELECT si.*, pr.sku, pr.name AS product_name, pr.unit
          FROM sale_items si JOIN products pr ON pr.id = si.product_id
         WHERE si.sale_id = ?
         ORDER BY si.id
        """,
        (sale_id,),
    )
    serials = db.query(
        """
        SELECT s.serial_no, s.sale_item_id, s.warranty_expiry, s.warranty_months
          FROM serials s WHERE s.sale_id = ?
         ORDER BY s.serial_no
        """,
        (sale_id,),
    )
    return sale, items, serials


@router.get("/{sale_id}")
def view_sale(request: Request, sale_id: int):
    require_user(request)
    sale, items, serials = _load_sale(sale_id)
    if sale is None:
        return redirect("/sales", error="That sale does not exist.")
    by_item: dict[int, list[str]] = {}
    for row in serials:
        by_item.setdefault(int(row["sale_item_id"] or 0), []).append(row["serial_no"])
    return render(
        request,
        "sales/detail.html",
        sale=sale,
        items=items,
        serials_by_item=by_item,
    )


@router.get("/{sale_id}/invoice")
def invoice(request: Request, sale_id: int):
    """The GST tax invoice. Styled for A4 print; the browser's Print dialog saves it as PDF."""
    require_user(request)
    sale, items, serials = _load_sale(sale_id)
    if sale is None:
        return redirect("/sales", error="That sale does not exist.")

    by_item: dict[int, list[str]] = {}
    for row in serials:
        by_item.setdefault(int(row["sale_item_id"] or 0), []).append(row["serial_no"])

    taxed = [
        gst.TaxedLine(
            qty=int(i["qty"]),
            unit_price_paise=int(i["unit_price_paise"]),
            rate_bp=int(i["gst_rate_bp"]),
            taxable_paise=int(i["taxable_paise"]),
            cgst_paise=int(i["cgst_paise"]),
            sgst_paise=int(i["sgst_paise"]),
            igst_paise=int(i["igst_paise"]),
            hsn_code=i["hsn_code"],
            interstate=bool(sale["interstate"]),
        )
        for i in items
    ]
    return render(
        request,
        "sales/invoice.html",
        sale=sale,
        items=items,
        serials_by_item=by_item,
        hsn_rows=gst.hsn_summary(taxed),
        copy_label=request.query_params.get("copy", "Original for Recipient"),
    )


@router.post("")
def create_sale(
    request: Request,
    invoice_date: str = Form(""),
    customer_name: str = Form(""),
    customer_phone: str = Form(""),
    customer_gstin: str = Form(""),
    customer_state_code: str = Form(""),
    customer_address: str = Form(""),
    lines_json: str = Form("[]"),
    payment_mode: str = Form("Cash"),
    prices_include_gst: bool = Form(False),
    round_to_rupee: bool = Form(False),
    notes: str = Form(""),
):
    user = require_user(request)
    try:
        lines = documents.parse_lines(lines_json, price_field="unit_price")
        result = documents.create_sale(
            invoice_date=invoice_date,
            customer=documents.CustomerInput(
                name=customer_name,
                phone=customer_phone,
                gstin=customer_gstin,
                state_code=customer_state_code,
                address=customer_address,
            ),
            lines=lines,
            payment_mode=payment_mode,
            prices_include_gst=prices_include_gst,
            round_to_rupee=round_to_rupee,
            notes=notes,
            user_id=user.id,
        )
    except (documents.DocumentError, money.MoneyError, repo.StockError, ValueError) as exc:
        return redirect("/sales/new", error=str(exc))
    return redirect(
        f"/sales/{result['sale_id']}/invoice",
        ok=f"Invoice {result['invoice_number']} created.",
    )


@router.post("/{sale_id}/void")
def void_sale(request: Request, sale_id: int, reason: str = Form("")):
    user = require_user(request)
    try:
        documents.void_sale(sale_id, reason=reason, user_id=user.id)
    except (documents.DocumentError, repo.StockError) as exc:
        return redirect(f"/sales/{sale_id}", error=str(exc))
    return redirect(
        f"/sales/{sale_id}",
        ok="Invoice cancelled. The number is kept so the series has no gaps.",
    )
