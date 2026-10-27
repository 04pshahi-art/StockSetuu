"""CCTV installation / service jobs, tracked separately from inventory."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request

from .. import db, documents, money, repo
from ..deps import redirect, render, require_user

router = APIRouter(prefix="/services")

PAGE_SIZE = 50


@router.get("")
def list_jobs(request: Request):
    require_user(request)
    status = request.query_params.get("status", "").strip()
    term = request.query_params.get("q", "").strip()
    date_from = request.query_params.get("from", "").strip()
    date_to = request.query_params.get("to", "").strip()

    where = ["1 = 1"]
    params: list[object] = []
    if status in {"pending", "completed", "cancelled"}:
        where.append("status = ?")
        params.append(status)
    if term:
        where.append("(customer_name LIKE ? OR customer_phone LIKE ? OR description LIKE ? OR job_number LIKE ?)")
        like = f"%{term}%"
        params.extend([like, like, like, like])
    if date_from:
        where.append("job_date >= ?")
        params.append(date_from)
    if date_to:
        where.append("job_date <= ?")
        params.append(date_to)
    clause = " AND ".join(where)

    rows = db.query(
        f"""
        SELECT j.*, (SELECT count(*) FROM service_job_parts p WHERE p.job_id = j.id) AS part_count
          FROM service_jobs j
         WHERE {clause}
         ORDER BY (j.status = 'pending') DESC, j.job_date DESC, j.id DESC
         LIMIT ?
        """,
        [*params, PAGE_SIZE * 4],
    )
    summary = db.query_one(
        f"""
        SELECT COALESCE(sum(total_paise), 0) AS total,
               COALESCE(sum(status = 'pending'), 0) AS pending,
               COALESCE(sum(status = 'completed'), 0) AS completed
          FROM service_jobs WHERE {clause} AND status != 'cancelled'
        """,
        params,
    )
    return render(
        request,
        "services/list.html",
        jobs=rows,
        status=status,
        term=term,
        date_from=date_from,
        date_to=date_to,
        summary=summary,
    )


@router.get("/new")
def new_job(request: Request):
    require_user(request)
    shop = repo.get_shop_settings()
    return render(
        request,
        "services/form.html",
        default_sac=shop.get("default_service_sac") or "",
        default_rate_bp=int(shop.get("default_gst_rate_bp") or 1800),
        shop_state=shop.get("state_code") or "27",
    )


@router.get("/{job_id}")
def view_job(request: Request, job_id: int):
    require_user(request)
    job = db.query_one("SELECT * FROM service_jobs WHERE id = ?", (job_id,))
    if job is None:
        return redirect("/services", error="That job does not exist.")
    parts = db.query(
        """
        SELECT p.*, pr.sku, pr.name AS product_name, pr.unit
          FROM service_job_parts p JOIN products pr ON pr.id = p.product_id
         WHERE p.job_id = ?
         ORDER BY p.id
        """,
        (job_id,),
    )
    return render(request, "services/detail.html", job=job, parts=parts)


@router.post("")
def create_job(
    request: Request,
    job_date: str = Form(""),
    customer_name: str = Form(""),
    customer_phone: str = Form(""),
    customer_address: str = Form(""),
    customer_state_code: str = Form(""),
    description: str = Form(""),
    amount: str = Form("0"),
    sac_code: str = Form(""),
    gst_rate: str = Form("0"),
    status: str = Form("pending"),
    notes: str = Form(""),
    parts_json: str = Form("[]"),
):
    user = require_user(request)
    try:
        parts = (
            documents.parse_lines(parts_json, price_field="unit_price", require_price=False)
            if parts_json.strip() not in {"", "[]"}
            else []
        )
        result = documents.create_service_job(
            job_date=job_date,
            customer_name=customer_name,
            customer_phone=customer_phone,
            customer_address=customer_address,
            customer_state_code=customer_state_code,
            description=description,
            amount_paise=money.paise(amount, field="Amount charged"),
            sac_code=sac_code,
            gst_rate_bp=money.parse_rate_bp(gst_rate, field="Service GST rate"),
            status=status,
            notes=notes,
            parts=parts,
            user_id=user.id,
        )
    except (documents.DocumentError, money.MoneyError, repo.StockError, ValueError) as exc:
        return redirect("/services/new", error=str(exc))
    return redirect(f"/services/{result['job_id']}", ok=f"Job {result['job_number']} saved.")


@router.post("/{job_id}/status")
def set_status(request: Request, job_id: int, status: str = Form("")):
    user = require_user(request)
    try:
        documents.set_service_status(job_id, status, user_id=user.id)
    except (documents.DocumentError, repo.StockError) as exc:
        return redirect(f"/services/{job_id}", error=str(exc))
    return redirect(f"/services/{job_id}", ok=f"Job marked {status}.")


@router.post("/{job_id}/notes")
def update_notes(request: Request, job_id: int, notes: str = Form("")):
    require_user(request)
    if db.query_one("SELECT id FROM service_jobs WHERE id = ?", (job_id,)) is None:
        return redirect("/services", error="That job does not exist.")
    with db.transaction():
        db.execute("UPDATE service_jobs SET notes = ? WHERE id = ?", (notes.strip(), job_id))
    return redirect(f"/services/{job_id}", ok="Notes saved.")
