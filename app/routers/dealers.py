"""Dealers / suppliers, with the GSTIN and state needed for input-tax-credit tracking."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request

from .. import db, gst, repo
from ..deps import redirect, render, require_user

router = APIRouter(prefix="/dealers")


@router.get("")
def list_dealers(request: Request):
    require_user(request)
    term = request.query_params.get("q", "").strip()
    where = ["1 = 1"]
    params: list[object] = []
    if term:
        where.append("(d.name LIKE ? OR d.contact_number LIKE ? OR d.gstin LIKE ?)")
        like = f"%{term}%"
        params.extend([like, like, like])
    rows = db.query(
        f"""
        SELECT d.*,
               (SELECT count(*) FROM purchases p WHERE p.dealer_id = d.id AND p.is_void = 0)
                   AS bill_count,
               (SELECT COALESCE(sum(p.total_paise), 0) FROM purchases p
                 WHERE p.dealer_id = d.id AND p.is_void = 0) AS total_paise,
               (SELECT max(p.bill_date) FROM purchases p
                 WHERE p.dealer_id = d.id AND p.is_void = 0) AS last_bill_date
          FROM dealers d
         WHERE {' AND '.join(where)}
         ORDER BY d.is_active DESC, d.name COLLATE NOCASE
        """,
        params,
    )
    return render(request, "dealers/list.html", dealers=rows, term=term)


@router.get("/new")
def new_dealer(request: Request):
    require_user(request)
    return render(
        request,
        "dealers/form.html",
        dealer={
            "id": None,
            "name": "",
            "contact_number": "",
            "gstin": "",
            "state_code": "",
            "address": "",
            "notes": "",
            "is_active": 1,
        },
        is_new=True,
    )


@router.get("/{dealer_id}")
def view_dealer(request: Request, dealer_id: int):
    require_user(request)
    dealer = repo.get_dealer(dealer_id)
    if dealer is None:
        return redirect("/dealers", error="That dealer does not exist.")
    purchases = db.query(
        """
        SELECT p.*, (SELECT count(*) FROM purchase_items pi WHERE pi.purchase_id = p.id) AS item_count
          FROM purchases p
         WHERE p.dealer_id = ?
         ORDER BY p.bill_date DESC, p.id DESC
         LIMIT 200
        """,
        (dealer_id,),
    )
    # What this dealer has supplied, and at what price — the comparison the shop actually
    # wants when the same part is bought from several dealers.
    items = db.query(
        """
        SELECT pr.id AS product_id, pr.sku, pr.name,
               sum(pi.qty) AS total_qty,
               min(pi.unit_cost_paise) AS min_cost,
               max(pi.unit_cost_paise) AS max_cost,
               max(p.bill_date) AS last_date,
               (SELECT pi2.unit_cost_paise FROM purchase_items pi2
                  JOIN purchases p2 ON p2.id = pi2.purchase_id
                 WHERE pi2.product_id = pr.id AND p2.dealer_id = ? AND p2.is_void = 0
                 ORDER BY p2.bill_date DESC, p2.id DESC LIMIT 1) AS last_cost
          FROM purchase_items pi
          JOIN purchases p ON p.id = pi.purchase_id
          JOIN products pr ON pr.id = pi.product_id
         WHERE p.dealer_id = ? AND p.is_void = 0
         GROUP BY pr.id
         ORDER BY pr.name COLLATE NOCASE
        """,
        (dealer_id, dealer_id),
    )
    totals = db.query_one(
        """
        SELECT COALESCE(sum(taxable_paise), 0) AS taxable,
               COALESCE(sum(cgst_paise), 0) AS cgst,
               COALESCE(sum(sgst_paise), 0) AS sgst,
               COALESCE(sum(igst_paise), 0) AS igst,
               COALESCE(sum(total_paise), 0) AS total
          FROM purchases WHERE dealer_id = ? AND is_void = 0
        """,
        (dealer_id,),
    )
    return render(
        request,
        "dealers/detail.html",
        dealer=dealer,
        purchases=purchases,
        items=items,
        totals=totals,
    )


@router.get("/{dealer_id}/edit")
def edit_dealer(request: Request, dealer_id: int):
    require_user(request)
    dealer = repo.get_dealer(dealer_id)
    if dealer is None:
        return redirect("/dealers", error="That dealer does not exist.")
    return render(request, "dealers/form.html", dealer=dealer, is_new=False)


def _validate(name: str, gstin: str, state_code: str) -> tuple[str, str, str]:
    name = name.strip()
    if not name:
        raise ValueError("Dealer name is required")
    gstin_clean = gst.normalise_gstin(gstin)
    ok, problem = gst.validate_gstin(gstin_clean)
    if not ok:
        raise ValueError(f"GSTIN: {problem}")
    state = (state_code or "").strip()
    if state and state not in gst.STATE_NAME_BY_CODE:
        raise ValueError("Pick a valid state")
    # A GSTIN carries its own state code; trust it over an empty or conflicting choice.
    from_gstin = gst.state_code_from_gstin(gstin_clean)
    if from_gstin:
        if state and state != from_gstin:
            raise ValueError(
                f"State does not match the GSTIN, which is registered in "
                f"{gst.state_label(from_gstin)}"
            )
        state = from_gstin
    return name, gstin_clean, state


@router.post("")
def create_dealer(
    request: Request,
    name: str = Form(""),
    contact_number: str = Form(""),
    gstin: str = Form(""),
    state_code: str = Form(""),
    address: str = Form(""),
    notes: str = Form(""),
    is_active: bool = Form(True),
):
    user = require_user(request)
    try:
        name, gstin_clean, state = _validate(name, gstin, state_code)
    except ValueError as exc:
        return redirect("/dealers/new", error=str(exc))
    if db.query_one("SELECT id FROM dealers WHERE name = ? COLLATE NOCASE", (name,)):
        return redirect("/dealers/new", error=f"A dealer named '{name}' already exists.")
    with db.transaction():
        dealer_id = db.insert(
            """
            INSERT INTO dealers (name, contact_number, gstin, state_code, address, notes, is_active)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (name, contact_number.strip(), gstin_clean, state, address.strip(), notes.strip(), int(is_active)),
        )
        repo.audit("dealer.create", entity="dealer", entity_id=dealer_id, detail=name, user_id=user.id)
    return redirect(f"/dealers/{dealer_id}", ok="Dealer saved.")


@router.post("/{dealer_id}")
def update_dealer(
    request: Request,
    dealer_id: int,
    name: str = Form(""),
    contact_number: str = Form(""),
    gstin: str = Form(""),
    state_code: str = Form(""),
    address: str = Form(""),
    notes: str = Form(""),
    is_active: bool = Form(True),
):
    user = require_user(request)
    if repo.get_dealer(dealer_id) is None:
        return redirect("/dealers", error="That dealer does not exist.")
    try:
        name, gstin_clean, state = _validate(name, gstin, state_code)
    except ValueError as exc:
        return redirect(f"/dealers/{dealer_id}/edit", error=str(exc))
    clash = db.query_one("SELECT id FROM dealers WHERE name = ? COLLATE NOCASE", (name,))
    if clash is not None and int(clash["id"]) != dealer_id:
        return redirect(f"/dealers/{dealer_id}/edit", error=f"A dealer named '{name}' already exists.")
    with db.transaction():
        db.execute(
            """
            UPDATE dealers
               SET name = ?, contact_number = ?, gstin = ?, state_code = ?,
                   address = ?, notes = ?, is_active = ?
             WHERE id = ?
            """,
            (
                name, contact_number.strip(), gstin_clean, state,
                address.strip(), notes.strip(), int(is_active), dealer_id,
            ),
        )
        repo.audit("dealer.update", entity="dealer", entity_id=dealer_id, detail=name, user_id=user.id)
    return redirect(f"/dealers/{dealer_id}", ok="Dealer updated.")
