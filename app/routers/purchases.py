"""Purchase entry (stock in) — dealer bill number/date, barcode scanning, GST as printed."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request

from .. import db, documents, gst, money, repo
from ..deps import redirect, render, require_user

router = APIRouter(prefix="/purchases")

PAGE_SIZE = 50


@router.get("")
def list_purchases(request: Request):
    require_user(request)
    term = request.query_params.get("q", "").strip()
    dealer_id = request.query_params.get("dealer_id", "").strip()
    date_from = request.query_params.get("from", "").strip()
    date_to = request.query_params.get("to", "").strip()
    try:
        page = max(1, int(request.query_params.get("page", "1")))
    except ValueError:
        page = 1

    where = ["1 = 1"]
    params: list[object] = []
    if term:
        where.append("(p.bill_number LIKE ? OR d.name LIKE ? OR p.notes LIKE ?)")
        like = f"%{term}%"
        params.extend([like, like, like])
    if dealer_id.isdigit():
        where.append("p.dealer_id = ?")
        params.append(int(dealer_id))
    if date_from:
        where.append("p.bill_date >= ?")
        params.append(date_from)
    if date_to:
        where.append("p.bill_date <= ?")
        params.append(date_to)
    clause = " AND ".join(where)

    total = int(
        db.scalar(
            f"SELECT count(*) FROM purchases p JOIN dealers d ON d.id = p.dealer_id WHERE {clause}",
            params,
            default=0,
        )
    )
    rows = db.query(
        f"""
        SELECT p.*, d.name AS dealer_name, d.gstin AS dealer_gstin,
               (SELECT count(*) FROM purchase_items pi WHERE pi.purchase_id = p.id) AS item_count
          FROM purchases p JOIN dealers d ON d.id = p.dealer_id
         WHERE {clause}
         ORDER BY p.bill_date DESC, p.id DESC
         LIMIT ? OFFSET ?
        """,
        [*params, PAGE_SIZE, (page - 1) * PAGE_SIZE],
    )
    summary = db.query_one(
        f"""
        SELECT COALESCE(sum(p.taxable_paise), 0) AS taxable,
               COALESCE(sum(p.cgst_paise + p.sgst_paise + p.igst_paise), 0) AS tax,
               COALESCE(sum(p.total_paise), 0) AS total
          FROM purchases p JOIN dealers d ON d.id = p.dealer_id
         WHERE {clause} AND p.is_void = 0
        """,
        params,
    )
    return render(
        request,
        "purchases/list.html",
        purchases=rows,
        dealers=repo.list_dealers(),
        term=term,
        dealer_id=dealer_id,
        date_from=date_from,
        date_to=date_to,
        summary=summary,
        page=page,
        total=total,
        pages=max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE),
    )


@router.get("/new")
def new_purchase(request: Request):
    require_user(request)
    dealers = repo.list_dealers(active_only=True)
    return render(
        request,
        "purchases/form.html",
        dealers=dealers,
        dealer_states={
            int(d["id"]): gst.resolve_state_code(d["state_code"], d["gstin"], repo.shop_state_code())
            for d in dealers
        },
        shop_state=repo.shop_state_code(),
    )


@router.get("/{purchase_id}")
def view_purchase(request: Request, purchase_id: int):
    require_user(request)
    purchase = db.query_one(
        """
        SELECT p.*, d.name AS dealer_name, d.gstin AS dealer_gstin, d.state_code AS dealer_state,
               d.contact_number AS dealer_phone
          FROM purchases p JOIN dealers d ON d.id = p.dealer_id
         WHERE p.id = ?
        """,
        (purchase_id,),
    )
    if purchase is None:
        return redirect("/purchases", error="That purchase does not exist.")
    items = db.query(
        """
        SELECT pi.*, pr.sku, pr.name AS product_name, pr.unit
          FROM purchase_items pi JOIN products pr ON pr.id = pi.product_id
         WHERE pi.purchase_id = ?
         ORDER BY pi.id
        """,
        (purchase_id,),
    )
    serials = db.query(
        """
        SELECT s.*, pr.name AS product_name
          FROM serials s JOIN products pr ON pr.id = s.product_id
         WHERE s.purchase_id = ?
         ORDER BY pr.name, s.serial_no
        """,
        (purchase_id,),
    )
    # The header amounts are what the dealer printed; the line sums are ours. Surfacing a
    # difference beats silently reporting a number that does not match the paper bill.
    line_taxable = sum(int(i["taxable_paise"]) for i in items)
    line_tax = sum(int(i["cgst_paise"]) + int(i["sgst_paise"]) + int(i["igst_paise"]) for i in items)
    mismatch = {
        "taxable": line_taxable - int(purchase["taxable_paise"]),
        "tax": line_tax
        - (int(purchase["cgst_paise"]) + int(purchase["sgst_paise"]) + int(purchase["igst_paise"])),
    }
    return render(
        request,
        "purchases/detail.html",
        purchase=purchase,
        items=items,
        serials=serials,
        line_taxable=line_taxable,
        line_tax=line_tax,
        mismatch=mismatch,
    )


@router.post("")
def create_purchase(
    request: Request,
    dealer_id: str = Form(""),
    bill_number: str = Form(""),
    bill_date: str = Form(""),
    lines_json: str = Form("[]"),
    bill_taxable: str = Form("0"),
    bill_cgst: str = Form("0"),
    bill_sgst: str = Form("0"),
    bill_igst: str = Form("0"),
    bill_round_off: str = Form("0"),
    bill_total: str = Form("0"),
    notes: str = Form(""),
    update_cost_prices: bool = Form(False),
):
    user = require_user(request)
    try:
        if not dealer_id.strip().isdigit():
            raise documents.DocumentError("Pick a dealer")
        lines = documents.parse_lines(lines_json, price_field="unit_cost")
        taxes = documents.PurchaseTaxInput(
            taxable_paise=money.paise(bill_taxable, field="Taxable value"),
            cgst_paise=money.paise(bill_cgst, field="CGST"),
            sgst_paise=money.paise(bill_sgst, field="SGST"),
            igst_paise=money.paise(bill_igst, field="IGST"),
            round_off_paise=money.paise(bill_round_off, field="Round off", allow_negative=True),
            total_paise=money.paise(bill_total, field="Bill total"),
        )
        result = documents.create_purchase(
            dealer_id=int(dealer_id),
            bill_number=bill_number,
            bill_date=bill_date,
            lines=lines,
            taxes=taxes,
            notes=notes,
            update_cost_prices=update_cost_prices,
            user_id=user.id,
        )
    except (documents.DocumentError, money.MoneyError, repo.StockError, ValueError) as exc:
        return redirect("/purchases/new", error=str(exc))
    return redirect(f"/purchases/{result['purchase_id']}", ok="Purchase saved and stock updated.")


@router.post("/{purchase_id}/void")
def void_purchase(request: Request, purchase_id: int, reason: str = Form("")):
    user = require_user(request)
    try:
        documents.void_purchase(purchase_id, reason=reason, user_id=user.id)
    except (documents.DocumentError, repo.StockError) as exc:
        return redirect(f"/purchases/{purchase_id}", error=str(exc))
    return redirect(f"/purchases/{purchase_id}", ok="Purchase cancelled and stock reversed.")
