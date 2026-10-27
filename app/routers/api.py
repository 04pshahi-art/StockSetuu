"""Small JSON endpoints used by the barcode/scan behaviour on the entry screens."""

from __future__ import annotations

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse

from .. import db, repo
from ..deps import require_user

router = APIRouter(prefix="/api")


@router.get("/lookup")
def lookup(request: Request, sku: str = "", q: str = ""):
    """Resolve a scanned barcode or typed code to a single product.

    The USB scanner behaves as a keyboard and ends each scan with Enter, so the entry
    screens simply post whatever landed in the focused field here.
    """
    require_user(request)
    term = (sku or q or "").strip()
    if not term:
        return JSONResponse({"found": False, "reason": "empty"}, status_code=400)

    exact = repo.get_product_by_sku(term)
    if exact is not None:
        return JSONResponse({"found": True, "exact": True, "product": repo.product_payload(exact)})

    matches = repo.search_products(term, limit=8)
    if len(matches) == 1:
        return JSONResponse({"found": True, "exact": False, "product": repo.product_payload(matches[0])})
    return JSONResponse(
        {
            "found": False,
            "reason": "ambiguous" if matches else "unknown",
            "term": term,
            "candidates": [repo.product_payload(row) for row in matches],
        }
    )


@router.get("/products/search")
def product_search(request: Request, q: str = "", limit: int = 15):
    require_user(request)
    rows = repo.search_products(q, limit=max(1, min(50, limit)))
    return JSONResponse({"results": [repo.product_payload(row) for row in rows]})


@router.get("/serials/available")
def available_serials(request: Request, product_id: int, q: str = "", limit: int = 50):
    """In-stock serials for a product, so a sale can pick rather than retype them."""
    require_user(request)
    params: list[object] = [product_id]
    clause = ""
    if q.strip():
        clause = " AND serial_no LIKE ?"
        params.append(f"%{q.strip()}%")
    params.append(max(1, min(200, limit)))
    rows = db.query(
        f"""
        SELECT serial_no, warranty_months, warranty_expiry, purchase_date
          FROM serials
         WHERE product_id = ? AND status = 'in_stock'{clause}
         ORDER BY serial_no
         LIMIT ?
        """,
        params,
    )
    return JSONResponse(
        {
            "results": [
                {
                    "serial_no": r["serial_no"],
                    "warranty_months": int(r["warranty_months"] or 0),
                    "warranty_expiry": r["warranty_expiry"],
                    "purchase_date": r["purchase_date"],
                }
                for r in rows
            ]
        }
    )


@router.get("/serials/lookup")
def serial_lookup(request: Request, serial: str = ""):
    """Resolve one serial number, for the warranty-lookup box and sale entry."""
    require_user(request)
    value = (serial or "").strip()
    if not value:
        return JSONResponse({"found": False}, status_code=400)
    row = db.query_one(
        """
        SELECT s.*, p.name AS product_name, p.sku, p.id AS pid
          FROM serials s JOIN products p ON p.id = s.product_id
         WHERE s.serial_no = ? COLLATE NOCASE
        """,
        (value,),
    )
    if row is None:
        return JSONResponse({"found": False, "serial": value})
    return JSONResponse(
        {
            "found": True,
            "serial_no": row["serial_no"],
            "status": row["status"],
            "product_id": int(row["pid"]),
            "product_name": row["product_name"],
            "sku": row["sku"],
            "warranty_months": int(row["warranty_months"] or 0),
            "warranty_expiry": row["warranty_expiry"],
        }
    )
