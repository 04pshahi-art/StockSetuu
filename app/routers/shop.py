"""Shop / business details used on every invoice, plus app-wide defaults."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request

from .. import db, gst, money, repo
from ..config import settings as app_settings
from ..deps import redirect, render, require_user

router = APIRouter(prefix="/settings")


@router.get("")
def view_settings(request: Request):
    require_user(request)
    counters = db.query("SELECT * FROM counters ORDER BY name")
    return render(
        request,
        "settings/shop.html",
        counters=counters,
        categories=repo.list_categories(),
        idle_minutes=app_settings.session_idle_minutes,
        db_path=str(app_settings.db_path),
        backup_dir=str(app_settings.backup_dir),
    )


@router.post("")
def save_settings(
    request: Request,
    legal_name: str = Form(""),
    trade_name: str = Form(""),
    gstin: str = Form(""),
    registration_type: str = Form("Regular"),
    state_code: str = Form("27"),
    address_line1: str = Form(""),
    address_line2: str = Form(""),
    city: str = Form(""),
    pincode: str = Form(""),
    phone: str = Form(""),
    email: str = Form(""),
    bank_details: str = Form(""),
    invoice_prefix: str = Form("PCS"),
    invoice_terms: str = Form(""),
    warranty_basis: str = Form("sale"),
    default_gst_rate: str = Form("18"),
    default_low_stock: str = Form("2"),
    default_service_sac: str = Form(""),
    default_prices_include_gst: bool = Form(False),
    round_invoice_to_rupee: bool = Form(False),
):
    user = require_user(request)

    if not legal_name.strip():
        return redirect("/settings", error="Legal name is required — it appears on every invoice.")

    gstin_clean = gst.normalise_gstin(gstin)
    ok, problem = gst.validate_gstin(gstin_clean)
    if not ok:
        return redirect("/settings", error=f"GSTIN: {problem}")

    state = (state_code or "").strip()
    if state not in gst.STATE_NAME_BY_CODE:
        return redirect("/settings", error="Pick a valid state.")
    from_gstin = gst.state_code_from_gstin(gstin_clean)
    if from_gstin and from_gstin != state:
        return redirect(
            "/settings",
            error=(
                f"The GSTIN is registered in {gst.state_label(from_gstin)} but the state is set to "
                f"{gst.state_label(state)}. These must agree — the state decides CGST/SGST vs IGST."
            ),
        )

    if warranty_basis not in {"sale", "purchase"}:
        return redirect("/settings", error="Warranty basis must be sale date or purchase date.")

    prefix = invoice_prefix.strip().strip("/") or "INV"
    if any(char in prefix for char in " \t\\"):
        return redirect("/settings", error="Invoice prefix cannot contain spaces.")

    # Changing the prefix mid-series would produce two different-looking numbers inside one
    # financial year, which reads like a gap to an auditor even though the sequence is intact.
    existing_prefix = str(db.scalar("SELECT invoice_prefix FROM shop_settings WHERE id = 1", default=""))
    if prefix != existing_prefix:
        issued = int(db.scalar("SELECT count(*) FROM sales", default=0))
        if issued:
            return redirect(
                "/settings",
                error=(
                    f"Invoice prefix cannot change once invoices exist ({issued} issued). "
                    "It would break the continuity of the numbering series."
                ),
            )

    try:
        rate_bp = money.parse_rate_bp(default_gst_rate, field="Default GST rate")
        low_stock = int(default_low_stock or 0)
    except (money.MoneyError, ValueError) as exc:
        return redirect("/settings", error=str(exc))
    if low_stock < 0:
        return redirect("/settings", error="Default low-stock level cannot be negative.")

    repo.update_shop_settings(
        {
            "legal_name": legal_name.strip(),
            "trade_name": trade_name.strip(),
            "gstin": gstin_clean,
            "registration_type": registration_type.strip() or "Regular",
            "state_code": state,
            "address_line1": address_line1.strip(),
            "address_line2": address_line2.strip(),
            "city": city.strip(),
            "pincode": pincode.strip(),
            "phone": phone.strip(),
            "email": email.strip(),
            "bank_details": bank_details.strip(),
            "invoice_prefix": prefix,
            "invoice_terms": invoice_terms.strip(),
            "warranty_basis": warranty_basis,
            "default_gst_rate_bp": rate_bp,
            "default_low_stock": low_stock,
            "default_service_sac": default_service_sac.strip(),
            "default_prices_include_gst": int(default_prices_include_gst),
            "round_invoice_to_rupee": int(round_invoice_to_rupee),
        }
    )
    with db.transaction():
        repo.audit("settings.update", entity="shop_settings", entity_id=1, user_id=user.id)
    return redirect("/settings", ok="Shop details saved.")


@router.post("/categories")
def add_category(request: Request, name: str = Form("")):
    require_user(request)
    clean = name.strip()
    if not clean:
        return redirect("/settings", error="Enter a category name.")
    with db.transaction():
        repo.ensure_category(clean)
    return redirect("/settings", ok=f"Category '{clean}' is available.")
