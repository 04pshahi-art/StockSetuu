"""Product catalogue: list, create, edit, deactivate, manual stock adjustment."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from starlette.responses import Response

from .. import db, money, repo
from ..deps import redirect, render, require_user

router = APIRouter(prefix="/products")

PAGE_SIZE = 50


@router.get("")
def list_products(request: Request):
    require_user(request)
    term = request.query_params.get("q", "").strip()
    category = request.query_params.get("category", "").strip()
    stock_filter = request.query_params.get("stock", "").strip()
    missing = request.query_params.get("missing", "").strip()
    show_inactive = request.query_params.get("inactive") == "1"
    try:
        page = max(1, int(request.query_params.get("page", "1")))
    except ValueError:
        page = 1

    where = ["1 = 1"]
    params: list[object] = []
    if not show_inactive:
        where.append("is_active = 1")
    if term:
        where.append("(sku LIKE ? OR name LIKE ? OR brand LIKE ? OR hsn_code LIKE ?)")
        like = f"%{term}%"
        params.extend([like, like, like, like])
    if category:
        where.append("category = ?")
        params.append(category)
    if stock_filter == "low":
        where.append("quantity <= low_stock_threshold")
    elif stock_filter == "out":
        where.append("quantity <= 0")
    # The GST registers link here to chase up whatever is missing a mandatory field.
    if missing == "hsn":
        where.append("COALESCE(TRIM(hsn_code), '') = ''")
    clause = " AND ".join(where)

    total = int(db.scalar(f"SELECT count(*) FROM products WHERE {clause}", params, default=0))
    rows = db.query(
        f"""
        SELECT * FROM products
         WHERE {clause}
         ORDER BY (quantity <= low_stock_threshold) DESC, name COLLATE NOCASE
         LIMIT ? OFFSET ?
        """,
        [*params, PAGE_SIZE, (page - 1) * PAGE_SIZE],
    )
    return render(
        request,
        "products/list.html",
        products=rows,
        categories=repo.list_categories(),
        term=term,
        category=category,
        stock_filter=stock_filter,
        missing=missing,
        show_inactive=show_inactive,
        page=page,
        page_size=PAGE_SIZE,
        total=total,
        pages=max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE),
    )


@router.get("/new")
def new_product(request: Request):
    require_user(request)
    shop = repo.get_shop_settings()
    blank = {
        "id": None,
        "sku": request.query_params.get("sku", "").strip(),
        "name": "",
        "category": repo.list_categories()[0] if repo.list_categories() else "Other",
        "brand": "",
        "unit": "Nos",
        "hsn_code": "",
        "gst_rate_bp": int(shop.get("default_gst_rate_bp") or 1800),
        "cost_price_paise": 0,
        "sale_price_paise": 0,
        "quantity": 0,
        "low_stock_threshold": int(shop.get("default_low_stock") or 2),
        "is_serialized": 0,
        "warranty_months": 0,
        "specs": "",
        "is_active": 1,
    }
    return render(
        request,
        "products/form.html",
        product=blank,
        categories=repo.list_categories(),
        is_new=True,
    )


@router.get("/{product_id}")
def view_product(request: Request, product_id: int):
    require_user(request)
    product = repo.get_product(product_id)
    if product is None:
        return redirect("/products", error="That product does not exist.")
    movements = db.query(
        """
        SELECT m.*, u.display_name AS user_name
          FROM stock_movements m
          LEFT JOIN users u ON u.id = m.created_by
         WHERE m.product_id = ?
         ORDER BY m.id DESC
         LIMIT 60
        """,
        (product_id,),
    )
    serials = db.query(
        "SELECT * FROM serials WHERE product_id = ? ORDER BY status, serial_no LIMIT 200",
        (product_id,),
    )
    recent_purchases = db.query(
        """
        SELECT p.id, p.bill_number, p.bill_date, p.is_void, d.name AS dealer_name,
               pi.qty, pi.unit_cost_paise
          FROM purchase_items pi
          JOIN purchases p ON p.id = pi.purchase_id
          JOIN dealers d ON d.id = p.dealer_id
         WHERE pi.product_id = ?
         ORDER BY p.bill_date DESC, p.id DESC
         LIMIT 20
        """,
        (product_id,),
    )
    return render(
        request,
        "products/detail.html",
        product=product,
        movements=movements,
        serials=serials,
        recent_purchases=recent_purchases,
    )


@router.get("/{product_id}/edit")
def edit_product(request: Request, product_id: int):
    require_user(request)
    product = repo.get_product(product_id)
    if product is None:
        return redirect("/products", error="That product does not exist.")
    return render(
        request,
        "products/form.html",
        product=product,
        categories=repo.list_categories(),
        is_new=False,
    )


def _read_form(
    *,
    sku: str,
    name: str,
    category: str,
    brand: str,
    unit: str,
    hsn_code: str,
    gst_rate: str,
    cost_price: str,
    sale_price: str,
    low_stock_threshold: str,
    warranty_months: str,
    specs: str,
    is_serialized: bool,
    is_active: bool,
) -> dict[str, object]:
    sku = sku.strip()
    name = name.strip()
    if not sku:
        raise money.MoneyError("SKU / barcode is required")
    if not name:
        raise money.MoneyError("Product name is required")
    try:
        threshold = int(low_stock_threshold or 0)
    except ValueError as exc:
        raise money.MoneyError("Low-stock threshold must be a whole number") from exc
    try:
        warranty = int(warranty_months or 0)
    except ValueError as exc:
        raise money.MoneyError("Warranty months must be a whole number") from exc
    if threshold < 0 or warranty < 0:
        raise money.MoneyError("Low-stock threshold and warranty months cannot be negative")
    return {
        "sku": sku,
        "name": name,
        "category": repo.ensure_category(category),
        "brand": brand.strip(),
        "unit": unit.strip() or "Nos",
        "hsn_code": hsn_code.strip(),
        "gst_rate_bp": money.parse_rate_bp(gst_rate),
        "cost_price_paise": money.paise(cost_price, field="Cost price"),
        "sale_price_paise": money.paise(sale_price, field="Sale price"),
        "low_stock_threshold": threshold,
        "warranty_months": warranty,
        "specs": specs.strip(),
        "is_serialized": int(is_serialized),
        "is_active": int(is_active),
    }


@router.post("")
def create_product(
    request: Request,
    sku: str = Form(""),
    name: str = Form(""),
    category: str = Form("Other"),
    brand: str = Form(""),
    unit: str = Form("Nos"),
    hsn_code: str = Form(""),
    gst_rate: str = Form("18"),
    cost_price: str = Form("0"),
    sale_price: str = Form("0"),
    opening_quantity: str = Form("0"),
    low_stock_threshold: str = Form("2"),
    warranty_months: str = Form("0"),
    specs: str = Form(""),
    is_serialized: bool = Form(False),
    is_active: bool = Form(True),
):
    user = require_user(request)
    try:
        values = _read_form(
            sku=sku,
            name=name,
            category=category,
            brand=brand,
            unit=unit,
            hsn_code=hsn_code,
            gst_rate=gst_rate,
            cost_price=cost_price,
            sale_price=sale_price,
            low_stock_threshold=low_stock_threshold,
            warranty_months=warranty_months,
            specs=specs,
            is_serialized=is_serialized,
            is_active=is_active,
        )
        opening = int(opening_quantity or 0)
    except (money.MoneyError, ValueError) as exc:
        return redirect("/products/new", error=str(exc))

    if repo.get_product_by_sku(values["sku"]):  # type: ignore[arg-type]
        return redirect("/products/new", error=f"SKU '{values['sku']}' is already used.")

    columns = ", ".join(values)
    placeholders = ", ".join("?" for _ in values)
    with db.transaction():
        product_id = db.insert(
            f"INSERT INTO products ({columns}) VALUES ({placeholders})", list(values.values())
        )
        if opening:
            repo.apply_stock_movement(
                product_id=product_id,
                delta=opening,
                ref_type="opening",
                ref_id=None,
                note="Opening stock",
                user_id=user.id,
                allow_negative=True,
            )
        repo.audit(
            "product.create", entity="product", entity_id=product_id,
            detail=f"{values['sku']} {values['name']}", user_id=user.id,
        )
    return redirect(f"/products/{product_id}", ok="Product saved.")


@router.post("/{product_id}")
def update_product(
    request: Request,
    product_id: int,
    sku: str = Form(""),
    name: str = Form(""),
    category: str = Form("Other"),
    brand: str = Form(""),
    unit: str = Form("Nos"),
    hsn_code: str = Form(""),
    gst_rate: str = Form("18"),
    cost_price: str = Form("0"),
    sale_price: str = Form("0"),
    low_stock_threshold: str = Form("2"),
    warranty_months: str = Form("0"),
    specs: str = Form(""),
    is_serialized: bool = Form(False),
    is_active: bool = Form(True),
):
    user = require_user(request)
    existing = repo.get_product(product_id)
    if existing is None:
        return redirect("/products", error="That product does not exist.")
    try:
        values = _read_form(
            sku=sku,
            name=name,
            category=category,
            brand=brand,
            unit=unit,
            hsn_code=hsn_code,
            gst_rate=gst_rate,
            cost_price=cost_price,
            sale_price=sale_price,
            low_stock_threshold=low_stock_threshold,
            warranty_months=warranty_months,
            specs=specs,
            is_serialized=is_serialized,
            is_active=is_active,
        )
    except money.MoneyError as exc:
        return redirect(f"/products/{product_id}/edit", error=str(exc))

    clash = repo.get_product_by_sku(str(values["sku"]))
    if clash is not None and int(clash["id"]) != product_id:
        return redirect(f"/products/{product_id}/edit", error=f"SKU '{values['sku']}' is already used.")

    if not values["is_serialized"] and int(existing["is_serialized"]):
        held = int(
            db.scalar("SELECT count(*) FROM serials WHERE product_id = ?", (product_id,), default=0)
        )
        if held:
            return redirect(
                f"/products/{product_id}/edit",
                error=f"Cannot turn off serial tracking: {held} serial number(s) are recorded.",
            )

    assignments = ", ".join(f"{key} = ?" for key in values)
    with db.transaction():
        db.execute(
            f"UPDATE products SET {assignments}, updated_at = datetime('now', 'localtime') WHERE id = ?",
            [*values.values(), product_id],
        )
        repo.audit(
            "product.update", entity="product", entity_id=product_id,
            detail=f"{values['sku']} {values['name']}", user_id=user.id,
        )
    return redirect(f"/products/{product_id}", ok="Product updated.")


@router.post("/{product_id}/adjust-stock")
def adjust_stock(
    request: Request,
    product_id: int,
    new_quantity: str = Form(""),
    reason: str = Form(""),
):
    user = require_user(request)
    product = repo.get_product(product_id)
    if product is None:
        return redirect("/products", error="That product does not exist.")
    try:
        target = int((new_quantity or "").strip())
    except ValueError:
        return redirect(f"/products/{product_id}", error="Enter the counted quantity as a whole number.")
    if target < 0:
        return redirect(f"/products/{product_id}", error="Counted quantity cannot be negative.")
    note = reason.strip() or "Manual stock adjustment"
    with db.transaction():
        repo.set_stock(product_id=product_id, new_quantity=target, note=note, user_id=user.id)
        repo.audit(
            "product.adjust_stock", entity="product", entity_id=product_id,
            detail=f"{product['quantity']} -> {target}: {note}", user_id=user.id,
        )
    return redirect(f"/products/{product_id}", ok="Stock adjusted.")


@router.get("/export/csv")
def export_csv(request: Request) -> Response:
    """Full catalogue as CSV — also the template shape the Tally importer accepts."""
    require_user(request)
    import csv
    import io

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "SKU", "Name", "Category", "Brand", "Unit", "HSN", "GST Rate %",
            "Cost Price", "Sale Price", "Quantity", "Low Stock", "Serial Tracked",
            "Warranty Months", "Specs",
        ]
    )
    for row in db.query("SELECT * FROM products ORDER BY name COLLATE NOCASE"):
        writer.writerow(
            [
                row["sku"], row["name"], row["category"], row["brand"], row["unit"],
                row["hsn_code"], money.fmt_rate(int(row["gst_rate_bp"])).rstrip("%"),
                money.rupees(int(row["cost_price_paise"])),
                money.rupees(int(row["sale_price_paise"])),
                row["quantity"], row["low_stock_threshold"],
                "Yes" if int(row["is_serialized"]) else "No",
                row["warranty_months"], row["specs"],
            ]
        )
    return Response(
        buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="products.csv"'},
    )
