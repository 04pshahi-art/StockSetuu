"""CSV / XLSX product import, built for whatever Tally actually exports.

No Tally schema is assumed or hardcoded. The flow is: upload the file → the app shows the
columns it genuinely found → the user maps those columns onto product fields → a dry run
shows exactly what would happen to every row → only then does anything get written.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import secrets
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, File, Form, Request, UploadFile

from .. import db, money, repo, tabular
from ..config import settings
from ..deps import redirect, render, require_user

router = APIRouter(prefix="/import")

MAX_UPLOAD_BYTES = 8 * 1024 * 1024
JOB_TTL_HOURS = 24
PREVIEW_ROWS = 400

# The product fields a column can be mapped onto. ``required`` here means "the import
# cannot identify or name a product without it".
FIELDS: list[dict[str, Any]] = [
    {"key": "sku", "label": "SKU / Barcode", "required": True,
     "hint": "Tally may call this Item Code, Part No or Alias. Must be unique."},
    {"key": "name", "label": "Product name", "required": True,
     "hint": "Tally's Stock Item / Particulars column."},
    {"key": "category", "label": "Category", "required": False,
     "hint": "Tally's Stock Group, if you use one. Unmapped rows take the default below."},
    {"key": "brand", "label": "Brand", "required": False, "hint": ""},
    {"key": "unit", "label": "Unit", "required": False, "hint": "Nos, Mtr, Set…"},
    {"key": "hsn_code", "label": "HSN code", "required": False, "hint": ""},
    {"key": "gst_rate", "label": "GST rate %", "required": False,
     "hint": "Accepts 18, 18%, or 0.18."},
    {"key": "cost_price", "label": "Cost price", "required": False,
     "hint": "Tally's Rate under Purchase, or closing-value rate."},
    {"key": "sale_price", "label": "Sale price", "required": False, "hint": ""},
    {"key": "quantity", "label": "Quantity in stock", "required": False,
     "hint": "Closing Balance quantity. '12 Nos' and '12.000' both work."},
    {"key": "low_stock_threshold", "label": "Low-stock alert level", "required": False, "hint": ""},
    {"key": "warranty_months", "label": "Warranty months", "required": False, "hint": ""},
    {"key": "specs", "label": "Notes / specification", "required": False, "hint": ""},
]
FIELD_KEYS = [f["key"] for f in FIELDS]

# Words that hint at a column's meaning. Only used to pre-select the dropdowns — the user
# always sees and can change every choice, so a wrong guess costs nothing.
GUESSES: dict[str, tuple[str, ...]] = {
    "sku": ("sku", "barcode", "item code", "itemcode", "part no", "part number", "code", "alias"),
    "name": ("name", "particulars", "stock item", "item name", "description", "product"),
    "category": ("category", "group", "stock group", "under"),
    "brand": ("brand", "make", "manufacturer", "company"),
    "unit": ("unit", "uom", "units"),
    "hsn_code": ("hsn", "hsn/sac", "sac"),
    "gst_rate": ("gst", "gst rate", "tax rate", "rate of tax", "tax %"),
    "cost_price": ("purchase rate", "cost", "purchase", "buy", "rate"),
    "sale_price": ("sale rate", "selling", "sales rate", "mrp", "sale price", "price"),
    "quantity": ("closing balance", "closing qty", "quantity", "qty", "stock", "balance"),
    "low_stock_threshold": ("reorder", "minimum", "min level", "low stock"),
    "warranty_months": ("warranty", "guarantee"),
    "specs": ("notes", "remarks", "specification", "specs", "details"),
}


# -- job storage -------------------------------------------------------------
#
# Parsed uploads live as JSON under data/imports/ rather than in the session cookie or a
# module global: a mapping step can take a while, and the file must survive a restart.


def _jobs_dir():
    path = settings.data_dir / "imports"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _job_path(token: str):
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", token or ""):
        return None
    return _jobs_dir() / f"{token}.json"


def _save_job(token: str, payload: dict[str, Any]) -> None:
    path = _job_path(token)
    if path is None:
        raise ValueError("bad import token")
    path.write_text(json.dumps(payload), encoding="utf-8")


def _load_job(token: str) -> dict[str, Any] | None:
    path = _job_path(token)
    if path is None or not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _sweep_jobs() -> None:
    """Delete stale uploads. Best effort — a locked file is not worth failing a request."""
    cutoff = dt.datetime.now().timestamp() - JOB_TTL_HOURS * 3600
    for path in _jobs_dir().glob("*.json"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            continue


# -- value parsing -----------------------------------------------------------

_TRAILING_UNIT = re.compile(r"[A-Za-z%₹/\s]+$")


def _number(raw: str) -> Decimal | None:
    """Pull a number out of a Tally cell.

    Handles '1,234.50', '12 Nos', '12.000 PCS', '₹450', '(1,234.50)' for negatives and
    the 'Dr'/'Cr' suffixes Tally puts on values. Returns None for a blank cell.
    """
    text = (raw or "").strip()
    if not text:
        return None
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()").strip()
    text = re.sub(r"\b(Dr|Cr)\b\.?$", "", text, flags=re.IGNORECASE).strip()
    text = _TRAILING_UNIT.sub("", text).strip()
    text = text.lstrip("₹Rs.rs ").replace(",", "").strip()
    if not text or text in {"-", "."}:
        return None
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    return -value if negative else value


def _int_or_none(raw: str, *, allow_negative: bool = False) -> int | None:
    value = _number(raw)
    if value is None:
        return None
    whole = int(value.to_integral_value(rounding="ROUND_HALF_UP"))
    if whole < 0 and not allow_negative:
        return None
    return whole


def _rate_bp(raw: str) -> int | None:
    value = _number(raw)
    if value is None:
        return None
    # A rate written as 0.18 means 18%; 18 also means 18%.
    if 0 < value < 1:
        value = value * 100
    return int((value * 100).to_integral_value(rounding="ROUND_HALF_UP"))


def _guess_mapping(headers: list[str]) -> dict[str, int]:
    lowered = [h.strip().lower() for h in headers]
    mapping: dict[str, int] = {}
    taken: set[int] = set()
    # Longest keyword first so "purchase rate" beats the bare "rate".
    for key in FIELD_KEYS:
        best_index, best_length = None, 0
        for keyword in sorted(GUESSES.get(key, ()), key=len, reverse=True):
            for index, header in enumerate(lowered):
                if index in taken or not header:
                    continue
                if keyword == header or keyword in header:
                    if len(keyword) > best_length:
                        best_index, best_length = index, len(keyword)
                    break
            if best_index is not None:
                break
        if best_index is not None:
            mapping[key] = best_index
            taken.add(best_index)
    return mapping


# -- analysis ----------------------------------------------------------------


def _analyse(job: dict[str, Any], options: dict[str, Any]) -> dict[str, Any]:
    """Work out what each row would do. Used for both the dry run and the commit."""
    headers: list[str] = job["headers"]
    rows: list[list[str]] = job["rows"]
    mapping: dict[str, int] = {k: int(v) for k, v in job.get("mapping", {}).items()}

    existing = {
        str(r["sku"]).strip().lower(): r
        for r in db.query("SELECT id, sku, name, quantity FROM products")
    }
    known_categories = {c.lower(): c for c in repo.list_categories()}
    default_rate = int(options["default_gst_rate_bp"])
    default_low = int(options["default_low_stock"])
    default_category = options["default_category"]
    on_existing = options["on_existing"]

    results: list[dict[str, Any]] = []
    seen_skus: dict[str, int] = {}
    counts = {"create": 0, "update": 0, "skip": 0, "error": 0}

    def cell(row: list[str], key: str) -> str:
        index = mapping.get(key)
        if index is None or index >= len(row):
            return ""
        return (row[index] or "").strip()

    for position, row in enumerate(rows, start=1):
        record: dict[str, Any] = {
            "row": position,
            "action": "create",
            "errors": [],
            "warnings": [],
            "values": {},
            "product_id": None,
        }
        sku = cell(row, "sku")
        name = cell(row, "name")

        if not sku and not name:
            continue  # blank filler row
        if not sku:
            record["errors"].append("No SKU")
        if not name:
            record["errors"].append("No product name")
        if len(sku) > 64:
            record["errors"].append("SKU longer than 64 characters")

        key = sku.lower()
        if key and key in seen_skus:
            record["errors"].append(f"SKU repeats row {seen_skus[key]} of this file")
        elif key:
            seen_skus[key] = position

        values: dict[str, Any] = {"sku": sku, "name": name}

        category = cell(row, "category") or default_category
        values["category"] = known_categories.get(category.lower(), category)

        values["brand"] = cell(row, "brand")
        values["unit"] = cell(row, "unit") or "Nos"
        values["hsn_code"] = re.sub(r"[^0-9]", "", cell(row, "hsn_code"))[:8]
        values["specs"] = cell(row, "specs")

        raw_rate = cell(row, "gst_rate")
        rate_bp = _rate_bp(raw_rate) if raw_rate else None
        if raw_rate and rate_bp is None:
            record["warnings"].append(f"GST rate '{raw_rate}' not understood — using default")
        if rate_bp is None:
            rate_bp = default_rate
        elif rate_bp not in money.GST_SLABS_BP:
            record["warnings"].append(f"{money.fmt_rate(rate_bp)} is not a standard slab")
        values["gst_rate_bp"] = rate_bp

        for target, field_key in (("cost_price_paise", "cost_price"), ("sale_price_paise", "sale_price")):
            raw = cell(row, field_key)
            if not raw:
                values[target] = 0
                continue
            amount = _number(raw)
            if amount is None or amount < 0:
                record["warnings"].append(f"{field_key.replace('_', ' ')} '{raw}' not understood")
                values[target] = 0
            else:
                values[target] = money.paise(amount, field=field_key)

        raw_qty = cell(row, "quantity")
        quantity = _int_or_none(raw_qty) if raw_qty else None
        if raw_qty and quantity is None:
            record["warnings"].append(f"Quantity '{raw_qty}' not understood — treated as 0")
            quantity = 0
        values["quantity"] = quantity

        low = _int_or_none(cell(row, "low_stock_threshold"))
        values["low_stock_threshold"] = default_low if low is None else low
        warranty = _int_or_none(cell(row, "warranty_months"))
        values["warranty_months"] = 0 if warranty is None else warranty

        record["values"] = values

        if record["errors"]:
            record["action"] = "error"
        else:
            found = existing.get(key)
            if found is not None:
                record["product_id"] = int(found["id"])
                record["existing_name"] = found["name"]
                record["existing_qty"] = int(found["quantity"])
                record["action"] = "update" if on_existing == "update" else "skip"

        counts[record["action"]] += 1
        results.append(record)

    return {
        "headers": headers,
        "results": results,
        "counts": counts,
        "total": len(results),
        "mapped": {k: headers[v] for k, v in mapping.items() if v < len(headers)},
    }


def _read_options(form: dict[str, Any]) -> dict[str, Any]:
    categories = repo.list_categories()
    default_category = str(form.get("default_category") or "").strip()
    if default_category not in categories:
        default_category = categories[0] if categories else "Other"
    try:
        rate_bp = money.parse_rate_bp(form.get("default_gst_rate") or "18")
    except money.MoneyError:
        rate_bp = 1800
    try:
        low = max(0, int(str(form.get("default_low_stock") or "2").strip() or 2))
    except ValueError:
        low = 2
    on_existing = str(form.get("on_existing") or "update")
    quantity_mode = str(form.get("quantity_mode") or "new_only")
    return {
        "default_category": default_category,
        "default_gst_rate_bp": rate_bp,
        "default_low_stock": low,
        "on_existing": on_existing if on_existing in {"update", "skip"} else "update",
        "quantity_mode": quantity_mode if quantity_mode in {"ignore", "new_only", "set"} else "new_only",
    }


# -- screens -----------------------------------------------------------------


@router.get("")
def upload_screen(request: Request):
    require_user(request)
    _sweep_jobs()
    recent = db.query(
        """
        SELECT * FROM audit_log WHERE action LIKE 'import.%'
         ORDER BY id DESC LIMIT 10
        """
    )
    return render(request, "import/upload.html", recent=recent)


@router.post("")
async def upload(request: Request, upload_file: UploadFile = File(...)):
    require_user(request)
    raw = await upload_file.read()
    if not raw:
        return redirect("/import", error="That file is empty.")
    if len(raw) > MAX_UPLOAD_BYTES:
        return redirect("/import", error="That file is larger than 8 MB. Split it and try again.")
    try:
        rows = tabular.read_table(upload_file.filename or "", raw)
    except tabular.TableError as exc:
        return redirect("/import", error=str(exc))
    if not rows:
        return redirect("/import", error="No rows found in that file.")

    token = secrets.token_urlsafe(12)
    header_index = tabular.find_header(rows)
    headers, data = tabular.normalise(rows, header_index)
    _save_job(
        token,
        {
            "filename": upload_file.filename or "upload",
            "raw_rows": rows,
            "header_index": header_index,
            "headers": headers,
            "rows": data,
            "mapping": _guess_mapping(headers),
        },
    )
    return redirect(f"/import/{token}", ok=f"Read {len(data)} rows. Now check the column mapping.")


@router.get("/{token}")
def mapping_screen(request: Request, token: str):
    require_user(request)
    job = _load_job(token)
    if job is None:
        return redirect("/import", error="That upload has expired. Please upload the file again.")
    shop = repo.get_shop_settings()
    return render(
        request,
        "import/map.html",
        token=token,
        job=job,
        fields=FIELDS,
        headers=job["headers"],
        sample=job["rows"][:8],
        raw_preview=job["raw_rows"][:12],
        row_count=len(job["rows"]),
        categories=repo.list_categories(),
        default_gst_rate_bp=int(shop.get("default_gst_rate_bp") or 1800),
        default_low_stock=int(shop.get("default_low_stock") or 2),
    )


@router.post("/{token}/header")
def set_header(request: Request, token: str, header_index: str = Form("0")):
    """Re-pick the header row when the guess landed on a title line."""
    require_user(request)
    job = _load_job(token)
    if job is None:
        return redirect("/import", error="That upload has expired. Please upload the file again.")
    try:
        index = int(header_index)
    except ValueError:
        return redirect(f"/import/{token}", error="Pick a valid header row.")
    headers, data = tabular.normalise(job["raw_rows"], index)
    job.update(
        {"header_index": index, "headers": headers, "rows": data, "mapping": _guess_mapping(headers)}
    )
    _save_job(token, job)
    return redirect(f"/import/{token}", ok=f"Header set to row {index + 1}. {len(data)} data rows.")


async def _mapping_from_form(request: Request, job: dict[str, Any]):
    form = await request.form()
    values = {key: form.get(key) for key in form.keys()}
    mapping: dict[str, int] = {}
    used: dict[int, str] = {}
    problems: list[str] = []
    for field in FIELDS:
        raw = str(values.get(f"map__{field['key']}") or "").strip()
        if raw == "":
            if field["required"]:
                problems.append(f"{field['label']} must be mapped to a column")
            continue
        try:
            index = int(raw)
        except ValueError:
            problems.append(f"{field['label']} has an invalid column")
            continue
        if not 0 <= index < len(job["headers"]):
            problems.append(f"{field['label']} points at a column that is not in the file")
            continue
        if index in used:
            problems.append(
                f"Column '{job['headers'][index]}' is mapped to both "
                f"{used[index]} and {field['label']}"
            )
            continue
        used[index] = field["label"]
        mapping[field["key"]] = index
    return mapping, values, problems


@router.post("/{token}/preview")
async def preview(request: Request, token: str):
    require_user(request)
    job = _load_job(token)
    if job is None:
        return redirect("/import", error="That upload has expired. Please upload the file again.")
    mapping, form_values, problems = await _mapping_from_form(request, job)
    if problems:
        return redirect(f"/import/{token}", error="; ".join(problems))

    job["mapping"] = mapping
    job["options"] = _read_options(form_values)
    _save_job(token, job)

    analysis = _analyse(job, job["options"])
    return render(
        request,
        "import/preview.html",
        token=token,
        job=job,
        options=job["options"],
        analysis=analysis,
        rows=analysis["results"][:PREVIEW_ROWS],
        truncated=max(0, analysis["total"] - PREVIEW_ROWS),
    )


@router.post("/{token}/commit")
def commit(request: Request, token: str, confirm: str = Form("")):
    user = require_user(request)
    job = _load_job(token)
    if job is None:
        return redirect("/import", error="That upload has expired. Please upload the file again.")
    if confirm != "yes":
        return redirect(f"/import/{token}", error="Import not confirmed.")
    options = job.get("options")
    if not options or not job.get("mapping"):
        return redirect(f"/import/{token}", error="Map the columns and run the dry run first.")

    # Re-analyse rather than trusting the preview: the catalogue may have changed since.
    analysis = _analyse(job, options)
    quantity_mode = options["quantity_mode"]
    created = updated = skipped = stock_changes = 0

    try:
        with db.transaction():
            for record in analysis["results"]:
                if record["action"] in {"error", "skip"}:
                    skipped += 1
                    continue
                values = record["values"]
                quantity = values["quantity"]

                if record["action"] == "create":
                    repo.ensure_category(values["category"])
                    product_id = db.insert(
                        """
                        INSERT INTO products
                          (sku, name, category, brand, unit, hsn_code, gst_rate_bp,
                           cost_price_paise, sale_price_paise, quantity, low_stock_threshold,
                           warranty_months, specs)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
                        """,
                        (
                            values["sku"], values["name"], values["category"], values["brand"],
                            values["unit"], values["hsn_code"], values["gst_rate_bp"],
                            values["cost_price_paise"], values["sale_price_paise"],
                            values["low_stock_threshold"], values["warranty_months"],
                            values["specs"],
                        ),
                    )
                    created += 1
                    # Stock always arrives through the ledger, never as a bare column write,
                    # so the on-hand number stays explainable.
                    if quantity_mode != "ignore" and quantity:
                        repo.apply_stock_movement(
                            product_id=product_id,
                            delta=quantity,
                            ref_type="import",
                            ref_id=None,
                            note=f"Opening stock from {job['filename']}",
                            user_id=user.id,
                            allow_negative=True,
                        )
                        stock_changes += 1
                    continue

                product_id = int(record["product_id"])
                repo.ensure_category(values["category"])
                # Only overwrite columns the user actually mapped — an unmapped column
                # must not silently blank out data that is already correct.
                mapped = set(job["mapping"].keys())
                assignments: list[str] = ["name = ?", "updated_at = datetime('now', 'localtime')"]
                params: list[object] = [values["name"]]
                column_for = {
                    "category": ("category = ?", values["category"]),
                    "brand": ("brand = ?", values["brand"]),
                    "unit": ("unit = ?", values["unit"]),
                    "hsn_code": ("hsn_code = ?", values["hsn_code"]),
                    "gst_rate": ("gst_rate_bp = ?", values["gst_rate_bp"]),
                    "cost_price": ("cost_price_paise = ?", values["cost_price_paise"]),
                    "sale_price": ("sale_price_paise = ?", values["sale_price_paise"]),
                    "low_stock_threshold": (
                        "low_stock_threshold = ?", values["low_stock_threshold"],
                    ),
                    "warranty_months": ("warranty_months = ?", values["warranty_months"]),
                    "specs": ("specs = ?", values["specs"]),
                }
                for field_key, (fragment, value) in column_for.items():
                    if field_key in mapped:
                        assignments.insert(-1, fragment)
                        params.append(value)
                params.append(product_id)
                db.execute(
                    f"UPDATE products SET {', '.join(assignments)} WHERE id = ?", params
                )
                updated += 1

                if quantity_mode == "set" and quantity is not None:
                    if quantity != int(record.get("existing_qty") or 0):
                        repo.set_stock(
                            product_id=product_id,
                            new_quantity=quantity,
                            note=f"Stock take from {job['filename']}",
                            user_id=user.id,
                        )
                        stock_changes += 1

            repo.audit(
                "import.products",
                entity="product",
                detail=(
                    f"{job['filename']}: {created} created, {updated} updated, "
                    f"{skipped} skipped, {stock_changes} stock adjustments"
                ),
                user_id=user.id,
            )
    except (repo.StockError, money.MoneyError, ValueError) as exc:
        return redirect(f"/import/{token}", error=f"Import stopped, nothing saved: {exc}")

    path = _job_path(token)
    if path is not None:
        try:
            path.unlink()
        except OSError:
            pass
    return redirect(
        "/products",
        ok=f"Imported: {created} new, {updated} updated, {skipped} skipped.",
    )
