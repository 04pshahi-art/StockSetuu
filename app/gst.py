"""GST rules: place of supply, CGST/SGST vs IGST, per-line tax computation.

This is the one module in the app that must be right rather than merely convenient, so
it is pure (no database, no request context) and covered by tests in ``tests/test_gst.py``.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field

from .money import mul_div_round

# GST state codes. The first two digits of a GSTIN are the state code, and comparing the
# buyer's code with the shop's decides intra-state (CGST+SGST) vs inter-state (IGST).
INDIAN_STATES: tuple[tuple[str, str], ...] = (
    ("01", "Jammu and Kashmir"),
    ("02", "Himachal Pradesh"),
    ("03", "Punjab"),
    ("04", "Chandigarh"),
    ("05", "Uttarakhand"),
    ("06", "Haryana"),
    ("07", "Delhi"),
    ("08", "Rajasthan"),
    ("09", "Uttar Pradesh"),
    ("10", "Bihar"),
    ("11", "Sikkim"),
    ("12", "Arunachal Pradesh"),
    ("13", "Nagaland"),
    ("14", "Manipur"),
    ("15", "Mizoram"),
    ("16", "Tripura"),
    ("17", "Meghalaya"),
    ("18", "Assam"),
    ("19", "West Bengal"),
    ("20", "Jharkhand"),
    ("21", "Odisha"),
    ("22", "Chhattisgarh"),
    ("23", "Madhya Pradesh"),
    ("24", "Gujarat"),
    ("26", "Dadra and Nagar Haveli and Daman and Diu"),
    ("27", "Maharashtra"),
    ("29", "Karnataka"),
    ("30", "Goa"),
    ("31", "Lakshadweep"),
    ("32", "Kerala"),
    ("33", "Tamil Nadu"),
    ("34", "Puducherry"),
    ("35", "Andaman and Nicobar Islands"),
    ("36", "Telangana"),
    ("37", "Andhra Pradesh"),
    ("38", "Ladakh"),
    ("97", "Other Territory"),
)

STATE_NAME_BY_CODE = dict(INDIAN_STATES)
STATE_CODE_BY_NAME = {name.casefold(): code for code, name in INDIAN_STATES}

HOME_STATE_CODE = "27"  # Maharashtra; the shop's own state, overridable in settings

_GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9A-Z][Z][0-9A-Z]$")
_GSTIN_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def normalise_gstin(gstin: str | None) -> str:
    return (gstin or "").strip().upper().replace(" ", "")


def gstin_check_digit(first_fourteen: str) -> str:
    """Compute the 15th GSTIN character using the official mod-36 algorithm."""
    factor = 2
    total = 0
    modulus = len(_GSTIN_ALPHABET)
    for char in reversed(first_fourteen):
        value = factor * _GSTIN_ALPHABET.index(char)
        factor = 1 if factor == 2 else 2
        total += (value // modulus) + (value % modulus)
    return _GSTIN_ALPHABET[(modulus - (total % modulus)) % modulus]


def validate_gstin(gstin: str | None) -> tuple[bool, str]:
    """Structural + checksum validation. Blank is valid (GSTIN is optional)."""
    value = normalise_gstin(gstin)
    if not value:
        return True, ""
    if len(value) != 15:
        return False, "GSTIN must be exactly 15 characters"
    if not _GSTIN_RE.match(value):
        return False, "GSTIN format looks wrong (expected e.g. 27BXZPS5663N1Z2)"
    if value[:2] not in STATE_NAME_BY_CODE:
        return False, f"'{value[:2]}' is not a valid GST state code"
    if gstin_check_digit(value[:14]) != value[14]:
        return False, "GSTIN checksum failed — please re-check for a typo"
    return True, ""


def state_code_from_gstin(gstin: str | None) -> str:
    value = normalise_gstin(gstin)
    return value[:2] if len(value) >= 2 and value[:2] in STATE_NAME_BY_CODE else ""


def state_label(code: str | None) -> str:
    code = (code or "").strip()
    name = STATE_NAME_BY_CODE.get(code)
    return f"{code} — {name}" if name else (code or "—")


def resolve_state_code(state_code: str | None, gstin: str | None, default: str) -> str:
    """Pick the effective state for a party.

    An explicitly chosen state wins; otherwise the GSTIN prefix is used; otherwise the
    shop's own state, which is the correct assumption for an unregistered walk-in buyer.
    """
    explicit = (state_code or "").strip()
    if explicit in STATE_NAME_BY_CODE:
        return explicit
    from_gstin = state_code_from_gstin(gstin)
    if from_gstin:
        return from_gstin
    return default


def is_interstate(shop_state_code: str, party_state_code: str | None) -> bool:
    """True when IGST applies. A blank party state means a local walk-in customer."""
    party = (party_state_code or "").strip()
    if not party:
        return False
    return party != (shop_state_code or HOME_STATE_CODE).strip()


@dataclass(slots=True)
class TaxedLine:
    """One invoice line with GST resolved. All amounts are integer paise."""

    qty: int
    unit_price_paise: int
    rate_bp: int
    taxable_paise: int
    cgst_paise: int = 0
    sgst_paise: int = 0
    igst_paise: int = 0
    hsn_code: str = ""
    interstate: bool = False

    @property
    def tax_paise(self) -> int:
        return self.cgst_paise + self.sgst_paise + self.igst_paise

    @property
    def total_paise(self) -> int:
        return self.taxable_paise + self.tax_paise


def compute_line(
    *,
    qty: int,
    unit_price_paise: int,
    rate_bp: int,
    interstate: bool,
    price_includes_gst: bool = False,
    hsn_code: str = "",
    discount_paise: int = 0,
) -> TaxedLine:
    """Split one line into taxable value and GST.

    When ``price_includes_gst`` the entered price is treated as the gross amount and the
    taxable value is back-calculated, which is how a retail counter usually quotes.
    CGST and SGST are each computed from half the rate so the two halves are always
    exactly equal, as a tax invoice requires.
    """
    if qty <= 0:
        raise ValueError("quantity must be positive")
    if rate_bp < 0:
        raise ValueError("GST rate cannot be negative")

    gross_or_net = qty * unit_price_paise - max(0, discount_paise)
    if gross_or_net < 0:
        raise ValueError("discount cannot exceed the line amount")

    if price_includes_gst and rate_bp:
        taxable = mul_div_round(gross_or_net, 10_000, 10_000 + rate_bp)
    else:
        taxable = gross_or_net

    line = TaxedLine(
        qty=qty,
        unit_price_paise=unit_price_paise,
        rate_bp=rate_bp,
        taxable_paise=taxable,
        hsn_code=(hsn_code or "").strip(),
        interstate=interstate,
    )
    if not rate_bp:
        return line
    if interstate:
        line.igst_paise = mul_div_round(taxable, rate_bp, 10_000)
    else:
        half = mul_div_round(taxable, rate_bp, 20_000)
        line.cgst_paise = half
        line.sgst_paise = half
    return line


@dataclass(slots=True)
class TaxTotals:
    taxable_paise: int = 0
    cgst_paise: int = 0
    sgst_paise: int = 0
    igst_paise: int = 0
    round_off_paise: int = 0
    lines: list[TaxedLine] = field(default_factory=list)

    @property
    def tax_paise(self) -> int:
        return self.cgst_paise + self.sgst_paise + self.igst_paise

    @property
    def subtotal_paise(self) -> int:
        return self.taxable_paise + self.tax_paise

    @property
    def grand_total_paise(self) -> int:
        return self.subtotal_paise + self.round_off_paise


def total_lines(lines: list[TaxedLine], *, round_to_rupee: bool = False) -> TaxTotals:
    """Sum taxed lines, optionally rounding the grand total to the nearest rupee."""
    totals = TaxTotals(lines=list(lines))
    for line in lines:
        totals.taxable_paise += line.taxable_paise
        totals.cgst_paise += line.cgst_paise
        totals.sgst_paise += line.sgst_paise
        totals.igst_paise += line.igst_paise
    if round_to_rupee:
        remainder = totals.subtotal_paise % 100
        totals.round_off_paise = -remainder if remainder < 50 else (100 - remainder)
    return totals


def hsn_summary(lines: list[TaxedLine]) -> list[dict[str, object]]:
    """Group lines by HSN + rate — the shape GSTR-1's HSN table expects."""
    buckets: dict[tuple[str, int], dict[str, object]] = {}
    for line in lines:
        key = (line.hsn_code or "—", line.rate_bp)
        bucket = buckets.setdefault(
            key,
            {
                "hsn_code": key[0],
                "rate_bp": key[1],
                "qty": 0,
                "taxable_paise": 0,
                "cgst_paise": 0,
                "sgst_paise": 0,
                "igst_paise": 0,
            },
        )
        bucket["qty"] = int(bucket["qty"]) + line.qty
        bucket["taxable_paise"] = int(bucket["taxable_paise"]) + line.taxable_paise
        bucket["cgst_paise"] = int(bucket["cgst_paise"]) + line.cgst_paise
        bucket["sgst_paise"] = int(bucket["sgst_paise"]) + line.sgst_paise
        bucket["igst_paise"] = int(bucket["igst_paise"]) + line.igst_paise
    return sorted(buckets.values(), key=lambda b: (str(b["hsn_code"]), int(b["rate_bp"])))


# -- Indian financial year ---------------------------------------------------


def financial_year(on: dt.date) -> tuple[int, int]:
    """Return the FY as ``(start_year, end_year)``. The Indian FY starts on 1 April."""
    return (on.year, on.year + 1) if on.month >= 4 else (on.year - 1, on.year)


def fy_label(on: dt.date) -> str:
    """``date(2025, 8, 28)`` -> ``'2025-26'`` — used in the invoice number series."""
    start, end = financial_year(on)
    return f"{start}-{end % 100:02d}"


def fy_bounds(label: str) -> tuple[dt.date, dt.date]:
    """``'2025-26'`` -> (2025-04-01, 2026-03-31)."""
    start_year = int(label.split("-")[0])
    return dt.date(start_year, 4, 1), dt.date(start_year + 1, 3, 31)
