"""Tally XML export: the file must at least be well-formed and every voucher must balance.

This does NOT confirm Tally itself accepts the file — that needs a real install, see the
module docstring in app/tally_export.py. What it does pin down is the arithmetic: for any
sale or purchase, the ledger-entry amounts in one voucher must sum to zero, because that
is the one property Tally enforces unconditionally on import.
"""

from __future__ import annotations

import datetime as dt
import unittest
import xml.etree.ElementTree as ET

from app import documents, repo, tally_export
from app.routers.reports import _tally_export_data

from tests.support import ShopTestCase

TODAY = dt.date.today().isoformat()


class TallyExportTests(ShopTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.set_shop_state("27")  # Maharashtra
        self.product_id = self.make_product(sku="TLY-1", name="Test Widget", gst_rate_bp=1800)
        self.dealer_id = self.make_dealer(name="Test Dealer Co", gstin="27AAAAA0000A1Z5")

    def _line(self, **kw):
        return documents.LineInput(
            product_id=self.product_id,
            qty=kw.pop("qty", 2),
            unit_price_paise=kw.pop("unit_price_paise", 100_000),
            gst_rate_bp=1800,
            hsn_code="8473",
            description="Test Widget",
            **kw,
        )

    def _make_local_sale(self):
        return documents.create_sale(
            invoice_date=TODAY,
            customer=documents.CustomerInput(name="Walk-in", state_code=""),
            lines=[self._line()],
        )

    def _make_interstate_sale(self):
        return documents.create_sale(
            invoice_date=TODAY,
            customer=documents.CustomerInput(
                name="Registered Buyer", gstin="29BBBBB0000B1Z1", state_code="29"
            ),
            lines=[self._line(qty=1)],
        )

    def _make_purchase(self):
        return documents.create_purchase(
            dealer_id=self.dealer_id,
            bill_number="BILL-1",
            bill_date=TODAY,
            lines=[self._line(qty=5)],
            taxes=documents.PurchaseTaxInput(
                taxable_paise=500_000, cgst_paise=45_000, sgst_paise=45_000
            ),
        )

    def _voucher_amounts(self, xml_text: str) -> list[list[float]]:
        """One list of ledger-entry amounts per <VOUCHER>."""
        root = ET.fromstring(xml_text)
        out = []
        for voucher in root.iter("VOUCHER"):
            amounts = [
                float(entry.findtext("AMOUNT", "0"))
                for entry in voucher.findall("ALLLEDGERENTRIES.LIST")
            ]
            out.append(amounts)
        return out

    def test_masters_and_vouchers_are_well_formed_xml(self):
        self._make_local_sale()
        self._make_interstate_sale()
        self._make_purchase()

        data = _tally_export_data(TODAY, TODAY)
        shop = repo.get_shop_settings()
        masters_xml = tally_export.build_masters_xml(
            company_name=shop.get("trade_name") or "Test Co",
            products=data["products"],
            sales=data["sales"],
            purchases=data["purchases"],
            dealers_by_id=data["dealers_by_id"],
            sale_rate_bps=data["sale_rate_bps"],
            purchase_rate_bps=data["purchase_rate_bps"],
        )
        vouchers_xml = tally_export.build_vouchers_xml(
            company_name=shop.get("trade_name") or "Test Co",
            sales=data["sales"],
            sale_items_by_sale=data["sale_items_by_sale"],
            purchases=data["purchases"],
            purchase_items_by_purchase=data["purchase_items_by_purchase"],
            dealers_by_id=data["dealers_by_id"],
        )

        # Raises ParseError if malformed — that's the well-formedness check.
        ET.fromstring(masters_xml)
        ET.fromstring(vouchers_xml)

        self.assertIn("Test Widget", masters_xml)
        self.assertIn("Output CGST", masters_xml)
        self.assertIn("Output IGST", masters_xml)
        self.assertIn("Input CGST", masters_xml)

        # Three vouchers went in (2 sales + 1 purchase) — three should come out.
        voucher_amounts = self._voucher_amounts(vouchers_xml)
        self.assertEqual(len(voucher_amounts), 3)
        for amounts in voucher_amounts:
            self.assertAlmostEqual(sum(amounts), 0.0, places=2, msg=f"unbalanced voucher: {amounts}")

    def test_empty_range_produces_valid_but_empty_vouchers(self):
        data = _tally_export_data("2000-01-01", "2000-01-02")
        xml_text = tally_export.build_vouchers_xml(
            company_name="Test Co",
            sales=data["sales"],
            sale_items_by_sale=data["sale_items_by_sale"],
            purchases=data["purchases"],
            purchase_items_by_purchase=data["purchase_items_by_purchase"],
            dealers_by_id=data["dealers_by_id"],
        )
        root = ET.fromstring(xml_text)
        self.assertEqual(len(list(root.iter("VOUCHER"))), 0)


if __name__ == "__main__":
    unittest.main()
