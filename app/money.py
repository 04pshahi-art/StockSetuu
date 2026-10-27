"""Integer money and quantity handling.

Every monetary amount in this app is an ``int`` number of paise. Floats are never used
for money: 0.1 + 0.2 problems turn into GST returns that do not reconcile.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

# GST rates are stored as integer basis points: 100 bp == 1%, so 18% == 1800.
# This keeps the CGST/SGST split exact for every standard slab (5/12/18/28).
GST_SLABS_BP = (0, 250, 500, 1200, 1800, 2800)

SLAB_LABELS = {
    0: "0%",
    250: "2.5%",
    500: "5%",
    1200: "12%",
    1800: "18%",
    2800: "28%",
}


class MoneyError(ValueError):
    """Raised when user input cannot be read as an amount."""


def paise(value: object, *, field: str = "amount", allow_negative: bool = False) -> int:
    """Parse a user-entered rupee amount into paise.

    Accepts ``"1,234.50"``, ``"1234.5"``, ``1234``, ``Decimal("1234.50")``.
    Blank input is 0 so that optional form fields can be left empty.
    """
    if value is None:
        return 0
    if isinstance(value, int) and not isinstance(value, bool):
        amount = Decimal(value)
    elif isinstance(value, Decimal):
        amount = value
    else:
        text = str(value).strip().replace(",", "").replace("₹", "")
        if not text:
            return 0
        try:
            amount = Decimal(text)
        except InvalidOperation as exc:
            raise MoneyError(f"{field}: {value!r} is not a valid amount") from exc
    if not allow_negative and amount < 0:
        raise MoneyError(f"{field} cannot be negative")
    # Quantize to paise, rounding half away from zero (what a shopkeeper expects).
    scaled = amount * 100
    integral = int(scaled)
    remainder = scaled - integral
    if remainder >= Decimal("0.5"):
        integral += 1
    elif remainder <= Decimal("-0.5"):
        integral -= 1
    return integral


def rupees(amount_paise: int) -> Decimal:
    """Paise -> Decimal rupees, for display and CSV export."""
    return (Decimal(int(amount_paise)) / 100).quantize(Decimal("0.01"))


def fmt_money(amount_paise: int | None, *, symbol: bool = False) -> str:
    """Format paise using the Indian digit grouping (12,34,567.89)."""
    if amount_paise is None:
        amount_paise = 0
    negative = amount_paise < 0
    whole, frac = divmod(abs(int(amount_paise)), 100)
    digits = str(whole)
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        digits = ",".join(groups + [tail])
    out = f"{digits}.{frac:02d}"
    if symbol:
        out = "₹" + out
    return "-" + out if negative else out


def fmt_rate(rate_bp: int | None) -> str:
    """Basis points -> a human rate label, e.g. 1800 -> '18%'."""
    if rate_bp is None:
        return "0%"
    rate_bp = int(rate_bp)
    if rate_bp in SLAB_LABELS:
        return SLAB_LABELS[rate_bp]
    whole, frac = divmod(rate_bp, 100)
    return f"{whole}%" if frac == 0 else f"{whole}.{frac:02d}".rstrip("0") + "%"


def parse_rate_bp(value: object, *, field: str = "GST rate") -> int:
    """Parse a percentage (``18``, ``18.0``, ``18%``) into basis points."""
    if value is None:
        return 0
    text = str(value).strip().rstrip("%").strip()
    if not text:
        return 0
    try:
        pct = Decimal(text)
    except InvalidOperation as exc:
        raise MoneyError(f"{field}: {value!r} is not a valid percentage") from exc
    if pct < 0 or pct > 100:
        raise MoneyError(f"{field} must be between 0 and 100")
    return int((pct * 100).to_integral_value())


def parse_qty(value: object, *, field: str = "quantity", minimum: int = 1) -> int:
    """Parse a whole-unit quantity. Stock is counted in pieces, never fractions."""
    text = str(value if value is not None else "").strip()
    if not text:
        raise MoneyError(f"{field} is required")
    try:
        qty = int(Decimal(text))
    except (InvalidOperation, ValueError) as exc:
        raise MoneyError(f"{field}: {value!r} is not a whole number") from exc
    if qty < minimum:
        raise MoneyError(f"{field} must be at least {minimum}")
    return qty


def mul_div_round(amount: int, numerator: int, denominator: int) -> int:
    """``amount * numerator / denominator`` rounded half-up, in pure integers."""
    if denominator == 0:
        raise ZeroDivisionError("denominator must be non-zero")
    total = amount * numerator
    if total < 0:
        return -((-total * 2 + denominator) // (2 * denominator))
    return (total * 2 + denominator) // (2 * denominator)


_ONES = (
    "Zero One Two Three Four Five Six Seven Eight Nine Ten Eleven Twelve Thirteen "
    "Fourteen Fifteen Sixteen Seventeen Eighteen Nineteen"
).split()
_TENS = "  Twenty Thirty Forty Fifty Sixty Seventy Eighty Ninety".split(" ")


def _two_digit_words(n: int) -> str:
    if n < 20:
        return _ONES[n]
    tens, ones = divmod(n, 10)
    return _TENS[tens] + (f" {_ONES[ones]}" if ones else "")


def _under_thousand_words(n: int) -> str:
    hundreds, rest = divmod(n, 100)
    parts = []
    if hundreds:
        parts.append(f"{_ONES[hundreds]} Hundred")
    if rest:
        parts.append(_two_digit_words(rest))
    return " ".join(parts)


def amount_in_words(amount_paise: int) -> str:
    """Indian-numbering amount in words, as required on a tax invoice."""
    amount_paise = int(amount_paise)
    sign = "Minus " if amount_paise < 0 else ""
    whole, frac = divmod(abs(amount_paise), 100)
    if whole == 0 and frac == 0:
        return "Rupees Zero Only"
    groups = [
        (whole // 10_000_000, "Crore"),
        (whole // 100_000 % 100, "Lakh"),
        (whole // 1_000 % 100, "Thousand"),
        (whole % 1_000, ""),
    ]
    words = []
    for value, label in groups[:3]:
        if value:
            words.append(f"{_two_digit_words(value)} {label}")
    if groups[3][0]:
        words.append(_under_thousand_words(groups[3][0]))
    out = f"{sign}Rupees {' '.join(words)}".strip() if words else f"{sign}Rupees Zero"
    if frac:
        out += f" and {_two_digit_words(frac)} Paise"
    return out + " Only"
