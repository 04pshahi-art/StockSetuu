"""Export sales/purchases as Tally-importable XML (Gateway of Tally > Import Data).

Two files are produced, deliberately separate:

  1. Masters  — stock items, ledgers (sales/purchase accounts, tax ledgers per rate,
     one ledger per customer-with-GSTIN / dealer, plus one shared walk-in ledger).
  2. Vouchers — one Sales or Purchase voucher per invoice/bill in the chosen date range.

Masters must be imported first (Gateway of Tally > Import Data > Masters), then the
vouchers file (> Vouchers) — importing vouchers before their ledgers exist is the most
common reason a Tally import fails.

Sign convention used below (matches Tally's own XML exports): a debit entry has
ISDEEMEDPOSITIVE="Yes" and a NEGATIVE amount; a credit entry has ISDEEMEDPOSITIVE="No"
and a POSITIVE amount. Every voucher's ledger-entry amounts sum to zero.

IMPORTANT: this has not been round-tripped through a real Tally install by us. Before
trusting it against a live company file, import into a throwaway/test company first and
check: (a) the Dr/Cr direction as shown on the ledger looks right, (b) stock quantity
moves the correct direction (down on a sale, up on a purchase), (c) the GST return does
not choke on the tax ledger names chosen here — a CA can rename ledgers in Tally after
import if their chart of accounts uses different names.
"""

from __future__ import annotations

import datetime as dt
from xml.sax.saxutils import escape

from . import gst, money


def _x(value: object) -> str:
    return escape(str(value if value is not None else ""))


def _amount(paise: int) -> str:
    return f"{money.rupees(int(paise)):.2f}"


def _tally_date(iso_date: str) -> str:
    """'2026-04-15' -> '20260415'."""
    return dt.date.fromisoformat(iso_date).strftime("%Y%m%d")


def _rate_pct(rate_bp: int) -> str:
    """1800 basis points -> '18', 500 -> '5', 250 -> '2.5' (no trailing .0)."""
    pct = rate_bp / 100
    return f"{pct:g}"


def _envelope(report_name: str, company_name: str, body: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<ENVELOPE>\n"
        "  <HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>\n"
        "  <BODY>\n"
        "    <IMPORTDATA>\n"
        "      <REQUESTDESC>\n"
        f"        <REPORTNAME>{_x(report_name)}</REPORTNAME>\n"
        "        <STATICVARIABLES>\n"
        f"          <SVCURRENTCOMPANY>{_x(company_name)}</SVCURRENTCOMPANY>\n"
        "        </STATICVARIABLES>\n"
        "      </REQUESTDESC>\n"
        f"      <REQUESTDATA>\n{body}      </REQUESTDATA>\n"
        "    </IMPORTDATA>\n"
        "  </BODY>\n"
        "</ENVELOPE>\n"
    )


def stock_item_name(product_row: dict) -> str:
    """Name + SKU keeps it unique even when two products share a display name."""
    return f"{product_row['name']} ({product_row['sku']})"


def walkin_ledger_name() -> str:
    return "Cash Sales - Walk-in Customers"


def customer_ledger_name(sale_row: dict) -> str:
    gstin = (sale_row.get("customer_gstin") or "").strip()
    if not gstin:
        return walkin_ledger_name()
    name = (sale_row.get("customer_name") or "").strip() or "Unnamed Customer"
    return f"{name} ({gstin})"


def dealer_ledger_name(dealer_row: dict) -> str:
    name = (dealer_row.get("name") or "").strip() or f"Dealer #{dealer_row.get('id')}"
    gstin = (dealer_row.get("gstin") or "").strip()
    return f"{name} ({gstin})" if gstin else name


def output_tax_ledger(kind: str, rate_bp: int) -> str:
    """kind is 'CGST' | 'SGST' | 'IGST'. Half-rate for CGST/SGST, full for IGST."""
    pct = _rate_pct(rate_bp / 2) if kind in ("CGST", "SGST") else _rate_pct(rate_bp)
    return f"Output {kind} @{pct}%"


def input_tax_ledger(kind: str, rate_bp: int) -> str:
    pct = _rate_pct(rate_bp / 2) if kind in ("CGST", "SGST") else _rate_pct(rate_bp)
    return f"Input {kind} @{pct}%"


# -- masters -------------------------------------------------------------------------


def build_masters_xml(
    *,
    company_name: str,
    products: list[dict],
    sales: list[dict],
    purchases: list[dict],
    dealers_by_id: dict[int, dict],
    sale_rate_bps: set[int],
    purchase_rate_bps: set[int],
) -> str:
    parts: list[str] = []

    # Fixed accounting ledgers.
    parts.append(
        "      <TALLYMESSAGE xmlns:UDF=\"TallyUDF\">\n"
        "        <LEDGER NAME=\"Sales Accounts\" ACTION=\"Create\">\n"
        "          <PARENT>Sales Accounts</PARENT>\n"
        "          <ISBILLWISEON>No</ISBILLWISEON>\n"
        "        </LEDGER>\n"
        "      </TALLYMESSAGE>\n"
    )
    parts.append(
        "      <TALLYMESSAGE xmlns:UDF=\"TallyUDF\">\n"
        "        <LEDGER NAME=\"Purchase Accounts\" ACTION=\"Create\">\n"
        "          <PARENT>Purchase Accounts</PARENT>\n"
        "          <ISBILLWISEON>No</ISBILLWISEON>\n"
        "        </LEDGER>\n"
        "      </TALLYMESSAGE>\n"
    )

    # Tax ledgers, one per rate actually used, output (sales) and input (purchases).
    seen_tax_ledgers: set[str] = set()
    for rate_bp in sorted(sale_rate_bps):
        if rate_bp <= 0:
            continue
        for kind in ("CGST", "SGST", "IGST"):
            name = output_tax_ledger(kind, rate_bp)
            if name in seen_tax_ledgers:
                continue
            seen_tax_ledgers.add(name)
            parts.append(
                "      <TALLYMESSAGE xmlns:UDF=\"TallyUDF\">\n"
                f"        <LEDGER NAME=\"{_x(name)}\" ACTION=\"Create\">\n"
                "          <PARENT>Duties &amp; Taxes</PARENT>\n"
                "          <TAXTYPE>Others</TAXTYPE>\n"
                "          <ISBILLWISEON>No</ISBILLWISEON>\n"
                "        </LEDGER>\n"
                "      </TALLYMESSAGE>\n"
            )
    for rate_bp in sorted(purchase_rate_bps):
        if rate_bp <= 0:
            continue
        for kind in ("CGST", "SGST", "IGST"):
            name = input_tax_ledger(kind, rate_bp)
            if name in seen_tax_ledgers:
                continue
            seen_tax_ledgers.add(name)
            parts.append(
                "      <TALLYMESSAGE xmlns:UDF=\"TallyUDF\">\n"
                f"        <LEDGER NAME=\"{_x(name)}\" ACTION=\"Create\">\n"
                "          <PARENT>Duties &amp; Taxes</PARENT>\n"
                "          <TAXTYPE>Others</TAXTYPE>\n"
                "          <ISBILLWISEON>No</ISBILLWISEON>\n"
                "        </LEDGER>\n"
                "      </TALLYMESSAGE>\n"
            )

    # Customer ledgers (Sundry Debtors) — one per registered (GSTIN) buyer, plus the
    # single shared walk-in ledger if any B2C sale is present.
    seen_party: set[str] = set()
    needs_walkin = False
    for sale in sales:
        name = customer_ledger_name(sale)
        if name == walkin_ledger_name():
            needs_walkin = True
            continue
        if name in seen_party:
            continue
        seen_party.add(name)
        gstin = (sale.get("customer_gstin") or "").strip()
        state = gst.state_label(sale.get("customer_state_code") or "")
        parts.append(
            "      <TALLYMESSAGE xmlns:UDF=\"TallyUDF\">\n"
            f"        <LEDGER NAME=\"{_x(name)}\" ACTION=\"Create\">\n"
            "          <PARENT>Sundry Debtors</PARENT>\n"
            f"          <PARTYGSTIN>{_x(gstin)}</PARTYGSTIN>\n"
            f"          <LEDSTATENAME>{_x(state)}</LEDSTATENAME>\n"
            "          <ISBILLWISEON>Yes</ISBILLWISEON>\n"
            "        </LEDGER>\n"
            "      </TALLYMESSAGE>\n"
        )
    if needs_walkin:
        parts.append(
            "      <TALLYMESSAGE xmlns:UDF=\"TallyUDF\">\n"
            f"        <LEDGER NAME=\"{_x(walkin_ledger_name())}\" ACTION=\"Create\">\n"
            "          <PARENT>Sundry Debtors</PARENT>\n"
            "          <ISBILLWISEON>No</ISBILLWISEON>\n"
            "        </LEDGER>\n"
            "      </TALLYMESSAGE>\n"
        )

    # Dealer ledgers (Sundry Creditors).
    seen_dealers: set[int] = set()
    for purchase in purchases:
        dealer_id = int(purchase["dealer_id"])
        if dealer_id in seen_dealers:
            continue
        seen_dealers.add(dealer_id)
        dealer = dealers_by_id.get(dealer_id, {})
        name = dealer_ledger_name(dealer)
        gstin = (dealer.get("gstin") or "").strip()
        state = gst.state_label(dealer.get("state_code") or "")
        parts.append(
            "      <TALLYMESSAGE xmlns:UDF=\"TallyUDF\">\n"
            f"        <LEDGER NAME=\"{_x(name)}\" ACTION=\"Create\">\n"
            "          <PARENT>Sundry Creditors</PARENT>\n"
            f"          <PARTYGSTIN>{_x(gstin)}</PARTYGSTIN>\n"
            f"          <LEDSTATENAME>{_x(state)}</LEDSTATENAME>\n"
            "          <ISBILLWISEON>Yes</ISBILLWISEON>\n"
            "        </LEDGER>\n"
            "      </TALLYMESSAGE>\n"
        )

    # Stock items — one per product that appears in this export's date range.
    for product in products:
        parts.append(
            "      <TALLYMESSAGE xmlns:UDF=\"TallyUDF\">\n"
            f"        <STOCKITEM NAME=\"{_x(stock_item_name(product))}\" ACTION=\"Create\">\n"
            "          <STOCKITEMNAME>" + _x(stock_item_name(product)) + "</STOCKITEMNAME>\n"
            f"          <BASEUNITS>{_x(product.get('unit') or 'Nos')}</BASEUNITS>\n"
            f"          <GSTHSNCODE>{_x(product.get('hsn_code') or '')}</GSTHSNCODE>\n"
            "        </STOCKITEM>\n"
            "      </TALLYMESSAGE>\n"
        )

    return _envelope("All Masters", company_name, "".join(parts))


# -- vouchers --------------------------------------------------------------------------


def _sale_voucher(sale: dict, items: list[dict]) -> str:
    total = int(sale["total_paise"])
    taxable = int(sale["taxable_paise"])
    cgst, sgst, igst = int(sale["cgst_paise"]), int(sale["sgst_paise"]), int(sale["igst_paise"])
    round_off = int(sale["round_off_paise"])
    party = customer_ledger_name(sale)
    interstate = bool(int(sale["interstate"]))

    inventory_lines = []
    for item in items:
        inventory_lines.append(
            "            <ALLINVENTORYENTRIES.LIST>\n"
            f"              <STOCKITEMNAME>{_x(item['_stock_name'])}</STOCKITEMNAME>\n"
            "              <ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>\n"
            f"              <ACTUALQTY>{item['qty']} {_x(item.get('_unit') or 'Nos')}</ACTUALQTY>\n"
            f"              <BILLEDQTY>{item['qty']} {_x(item.get('_unit') or 'Nos')}</BILLEDQTY>\n"
            f"              <AMOUNT>{_amount(int(item['taxable_paise']))}</AMOUNT>\n"
            "            </ALLINVENTORYENTRIES.LIST>\n"
        )

    ledger_entries = [
        "          <ALLLEDGERENTRIES.LIST>\n"
        f"            <LEDGERNAME>{_x(party)}</LEDGERNAME>\n"
        "            <ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>\n"
        f"            <AMOUNT>{_amount(-total)}</AMOUNT>\n"
        "          </ALLLEDGERENTRIES.LIST>\n",
        "          <ALLLEDGERENTRIES.LIST>\n"
        "            <LEDGERNAME>Sales Accounts</LEDGERNAME>\n"
        "            <ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>\n"
        f"            <AMOUNT>{_amount(taxable)}</AMOUNT>\n"
        + "".join(inventory_lines)
        + "          </ALLLEDGERENTRIES.LIST>\n",
    ]
    for kind, amt in (("CGST", cgst), ("SGST", sgst), ("IGST", igst)):
        if amt == 0:
            continue
        rate_bp = int(items[0]["gst_rate_bp"]) if items else 0
        ledger_entries.append(
            "          <ALLLEDGERENTRIES.LIST>\n"
            f"            <LEDGERNAME>{_x(output_tax_ledger(kind, rate_bp))}</LEDGERNAME>\n"
            "            <ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>\n"
            f"            <AMOUNT>{_amount(amt)}</AMOUNT>\n"
            "          </ALLLEDGERENTRIES.LIST>\n"
        )
    if round_off:
        # total = taxable + cgst + sgst + igst + round_off_paise, so the entry needed to
        # zero the voucher out is exactly round_off_paise on the credit side (same sign
        # convention as Sales Accounts / the tax ledgers above).
        ledger_entries.append(
            "          <ALLLEDGERENTRIES.LIST>\n"
            "            <LEDGERNAME>Round Off</LEDGERNAME>\n"
            f"            <ISDEEMEDPOSITIVE>{'Yes' if round_off < 0 else 'No'}</ISDEEMEDPOSITIVE>\n"
            f"            <AMOUNT>{_amount(round_off)}</AMOUNT>\n"
            "          </ALLLEDGERENTRIES.LIST>\n"
        )

    return (
        "      <TALLYMESSAGE xmlns:UDF=\"TallyUDF\">\n"
        "        <VOUCHER VCHTYPE=\"Sales\" ACTION=\"Create\" OBJVIEW=\"Invoice Voucher View\">\n"
        f"          <DATE>{_tally_date(sale['invoice_date'])}</DATE>\n"
        "          <VOUCHERTYPENAME>Sales</VOUCHERTYPENAME>\n"
        f"          <VOUCHERNUMBER>{_x(sale['invoice_number'])}</VOUCHERNUMBER>\n"
        f"          <PARTYLEDGERNAME>{_x(party)}</PARTYLEDGERNAME>\n"
        f"          <PARTYGSTIN>{_x(sale.get('customer_gstin') or '')}</PARTYGSTIN>\n"
        f"          <PLACEOFSUPPLY>{_x(gst.state_label(sale.get('customer_state_code') or ''))}</PLACEOFSUPPLY>\n"
        f"          <ISINVOICE>Yes</ISINVOICE>\n"
        f"          <NARRATION>{_x(sale.get('notes') or '')}</NARRATION>\n"
        + "".join(ledger_entries)
        + "        </VOUCHER>\n"
        "      </TALLYMESSAGE>\n"
    )


def _purchase_voucher(purchase: dict, items: list[dict], dealer: dict) -> str:
    total = int(purchase["total_paise"])
    taxable = int(purchase["taxable_paise"])
    cgst, sgst, igst = int(purchase["cgst_paise"]), int(purchase["sgst_paise"]), int(purchase["igst_paise"])
    round_off = int(purchase["round_off_paise"])
    party = dealer_ledger_name(dealer)

    inventory_lines = []
    for item in items:
        inventory_lines.append(
            "            <ALLINVENTORYENTRIES.LIST>\n"
            f"              <STOCKITEMNAME>{_x(item['_stock_name'])}</STOCKITEMNAME>\n"
            "              <ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>\n"
            f"              <ACTUALQTY>{item['qty']} {_x(item.get('_unit') or 'Nos')}</ACTUALQTY>\n"
            f"              <BILLEDQTY>{item['qty']} {_x(item.get('_unit') or 'Nos')}</BILLEDQTY>\n"
            f"              <AMOUNT>{_amount(-int(item['taxable_paise']))}</AMOUNT>\n"
            "            </ALLINVENTORYENTRIES.LIST>\n"
        )

    ledger_entries = [
        "          <ALLLEDGERENTRIES.LIST>\n"
        f"            <LEDGERNAME>{_x(party)}</LEDGERNAME>\n"
        "            <ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>\n"
        f"            <AMOUNT>{_amount(total)}</AMOUNT>\n"
        "          </ALLLEDGERENTRIES.LIST>\n",
        "          <ALLLEDGERENTRIES.LIST>\n"
        "            <LEDGERNAME>Purchase Accounts</LEDGERNAME>\n"
        "            <ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>\n"
        f"            <AMOUNT>{_amount(-taxable)}</AMOUNT>\n"
        + "".join(inventory_lines)
        + "          </ALLLEDGERENTRIES.LIST>\n",
    ]
    for kind, amt in (("CGST", cgst), ("SGST", sgst), ("IGST", igst)):
        if amt == 0:
            continue
        rate_bp = int(items[0]["gst_rate_bp"]) if items else 0
        ledger_entries.append(
            "          <ALLLEDGERENTRIES.LIST>\n"
            f"            <LEDGERNAME>{_x(input_tax_ledger(kind, rate_bp))}</LEDGERNAME>\n"
            "            <ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>\n"
            f"            <AMOUNT>{_amount(-amt)}</AMOUNT>\n"
            "          </ALLLEDGERENTRIES.LIST>\n"
        )
    if round_off:
        ledger_entries.append(
            "          <ALLLEDGERENTRIES.LIST>\n"
            "            <LEDGERNAME>Round Off</LEDGERNAME>\n"
            f"            <ISDEEMEDPOSITIVE>{'Yes' if round_off > 0 else 'No'}</ISDEEMEDPOSITIVE>\n"
            f"            <AMOUNT>{_amount(-round_off)}</AMOUNT>\n"
            "          </ALLLEDGERENTRIES.LIST>\n"
        )

    bill_number = purchase.get("bill_number") or f"P-{purchase['id']}"
    return (
        "      <TALLYMESSAGE xmlns:UDF=\"TallyUDF\">\n"
        "        <VOUCHER VCHTYPE=\"Purchase\" ACTION=\"Create\" OBJVIEW=\"Invoice Voucher View\">\n"
        f"          <DATE>{_tally_date(purchase['bill_date'])}</DATE>\n"
        "          <VOUCHERTYPENAME>Purchase</VOUCHERTYPENAME>\n"
        f"          <VOUCHERNUMBER>{_x(bill_number)}</VOUCHERNUMBER>\n"
        f"          <PARTYLEDGERNAME>{_x(party)}</PARTYLEDGERNAME>\n"
        f"          <PARTYGSTIN>{_x(dealer.get('gstin') or '')}</PARTYGSTIN>\n"
        f"          <NARRATION>{_x(purchase.get('notes') or '')}</NARRATION>\n"
        + "".join(ledger_entries)
        + "        </VOUCHER>\n"
        "      </TALLYMESSAGE>\n"
    )


def build_vouchers_xml(
    *,
    company_name: str,
    sales: list[dict],
    sale_items_by_sale: dict[int, list[dict]],
    purchases: list[dict],
    purchase_items_by_purchase: dict[int, list[dict]],
    dealers_by_id: dict[int, dict],
) -> str:
    parts: list[str] = []
    for sale in sales:
        if int(sale.get("is_void") or 0):
            continue
        parts.append(_sale_voucher(sale, sale_items_by_sale.get(int(sale["id"]), [])))
    for purchase in purchases:
        if int(purchase.get("is_void") or 0):
            continue
        dealer = dealers_by_id.get(int(purchase["dealer_id"]), {})
        parts.append(_purchase_voucher(purchase, purchase_items_by_purchase.get(int(purchase["id"]), []), dealer))
    return _envelope("Vouchers", company_name, "".join(parts))
