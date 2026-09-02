"""Serial numbers and warranty expiry.

A serial-tracked item has a life: it arrives on a dealer's bill, sits in stock, goes out on
an invoice, and carries a warranty that expires on a computed date. The shop's whole answer
to "is this still under warranty?" months later rests on that date being right and on the
serial having exactly one status at a time.

Two decisions shape everything here:

**Where the warranty starts.** Configurable in settings, because it is a commercial choice,
not a technical one. ``warranty_basis = 'sale'`` starts the clock when the customer buys it,
which is what a customer expects and what most manufacturers of finished goods do.
``'purchase'`` starts it when the shop bought it, which is what a dealer's own warranty
follows. Getting this wrong does not error — it quietly honours or refuses claims by the
wrong number of months.

**Month arithmetic is calendar arithmetic.** A 12-month warranty on a 31 January purchase
does not expire on 31 February. ``repo.add_months`` clamps to the end of a short month, and
that behaviour is pinned below because "add 365 days" would be wrong across a leap year and
"same day next month" crashes four times a year.
"""

from __future__ import annotations

import datetime as dt
import unittest

from app import db, documents, repo

from tests.support import ShopTestCase

TODAY = dt.date.today()
TODAY_ISO = TODAY.isoformat()


class AddMonthsTests(unittest.TestCase):
    """Pure arithmetic, no database. The dates a warranty claim is decided on."""

    def test_a_plain_month_addition(self):
        self.assertEqual(repo.add_months(dt.date(2026, 3, 15), 12), dt.date(2027, 3, 15))

    def test_zero_months_returns_the_same_day(self):
        # A product with no warranty must not get an expiry date at all; this is the guard
        # that keeps "0 months" from meaning "expires today".
        self.assertEqual(repo.add_months(dt.date(2026, 8, 30), 0), dt.date(2026, 8, 30))

    def test_the_end_of_a_short_month_is_clamped_not_overflowed(self):
        # 31 January + 1 month is 28 February, not 3 March and not a crash.
        self.assertEqual(repo.add_months(dt.date(2026, 1, 31), 1), dt.date(2026, 2, 28))
        self.assertEqual(repo.add_months(dt.date(2026, 3, 31), 1), dt.date(2026, 4, 30))
        self.assertEqual(repo.add_months(dt.date(2026, 5, 31), 1), dt.date(2026, 6, 30))

    def test_a_leap_year_gains_the_extra_day(self):
        self.assertEqual(repo.add_months(dt.date(2028, 1, 31), 1), dt.date(2028, 2, 29))
        self.assertEqual(repo.add_months(dt.date(2027, 2, 28), 12), dt.date(2028, 2, 28))

    def test_crossing_a_year_boundary(self):
        self.assertEqual(repo.add_months(dt.date(2026, 11, 10), 3), dt.date(2027, 2, 10))
        self.assertEqual(repo.add_months(dt.date(2026, 12, 31), 1), dt.date(2027, 1, 31))

    def test_common_warranty_lengths_land_on_the_right_year(self):
        start = dt.date(2026, 8, 30)
        for months, expected in (
            (6, dt.date(2027, 2, 28)),
            (12, dt.date(2027, 8, 30)),
            (24, dt.date(2028, 8, 30)),
            (36, dt.date(2029, 8, 30)),
        ):
            with self.subTest(months=months):
                self.assertEqual(repo.add_months(start, months), expected)

    def test_many_months_stays_consistent(self):
        # 60 months is five years, not four years and eleven months.
        self.assertEqual(repo.add_months(dt.date(2026, 1, 1), 60), dt.date(2031, 1, 1))


class SerialBase(ShopTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.set_shop_state("27")
        self.dealer = self.make_dealer(name="Serial Dealer")
        self.product = self.make_product(
            sku="CAM-1",
            name="CCTV Dome Camera",
            quantity=0,
            is_serialized=1,
            warranty_months=24,
        )

    def buy(self, serials: list[str], **kw) -> dict:
        """Bring serial-tracked stock in on a dealer's bill."""
        product_id = kw.pop("product_id", self.product)
        return documents.create_purchase(
            dealer_id=self.dealer,
            bill_number=kw.pop("bill_number", "SD/2026/1"),
            bill_date=kw.pop("bill_date", TODAY_ISO),
            lines=[
                documents.LineInput(
                    product_id=product_id,
                    qty=len(serials),
                    unit_price_paise=kw.pop("unit_price_paise", 250_000),
                    gst_rate_bp=1800,
                    hsn_code="8525",
                    description="CCTV Dome Camera",
                    serials=serials,
                    warranty_months=kw.pop("warranty_months", 24),
                )
            ],
            taxes=documents.PurchaseTaxInput(taxable_paise=250_000 * len(serials)),
            **kw,
        )

    def sell(self, serials: list[str], **kw) -> dict:
        product_id = kw.pop("product_id", self.product)
        return documents.create_sale(
            invoice_date=kw.pop("invoice_date", TODAY_ISO),
            customer=documents.CustomerInput(
                name=kw.pop("customer_name", "Mr Deshpande"),
                phone=kw.pop("phone", "9876543210"),
            ),
            lines=[
                documents.LineInput(
                    product_id=product_id,
                    qty=len(serials),
                    unit_price_paise=kw.pop("unit_price_paise", 350_000),
                    gst_rate_bp=1800,
                    hsn_code="8525",
                    description="CCTV Dome Camera",
                    serials=serials,
                )
            ],
            **kw,
        )

    def serial(self, serial_no: str) -> db.Row:
        row = db.query_one("SELECT * FROM serials WHERE serial_no = ?", (serial_no,))
        assert row is not None, f"serial {serial_no} was not recorded at all"
        return row


class RegisterOnPurchaseTests(SerialBase):
    def test_serials_arrive_in_stock_with_their_purchase_details(self):
        self.buy(["SN-A1", "SN-A2"])
        for serial_no in ("SN-A1", "SN-A2"):
            row = self.serial(serial_no)
            self.assertEqual(row["status"], "in_stock")
            self.assertEqual(row["product_id"], self.product)
            self.assertEqual(row["purchase_date"], TODAY_ISO)
            self.assertEqual(row["warranty_months"], 24)
            self.assertIsNotNone(row["purchase_id"])
            self.assertIsNone(row["sale_id"])

    def test_the_serial_count_and_the_quantity_are_reconciled_at_the_form_boundary(self):
        # Worth being precise about where this is enforced: the "one serial per unit" rule
        # lives in ``parse_lines``, which is what every route posts through. Calling
        # ``create_purchase`` with hand-built LineInputs skips it. That is fine today
        # because nothing else calls it — but it is the reason this test goes through the
        # payload rather than through the document.
        import json

        with self.assertRaises(documents.DocumentError) as caught:
            documents.parse_lines(
                json.dumps(
                    [
                        {
                            "product_id": self.product,
                            "qty": 3,
                            "unit_cost": "2500",
                            "gst_rate_bp": 1800,
                            "hsn_code": "8525",
                            "description": "CCTV Dome Camera",
                            "serials": "SN-1, SN-2",
                        }
                    ]
                ),
                price_field="unit_cost",
            )
        self.assertIn("serial", str(caught.exception).lower())
        self.assertEqual(db.scalar("SELECT COUNT(*) FROM serials"), 0)

    def test_the_same_serial_twice_on_one_line_is_refused(self):
        # Reaching ``create_purchase`` directly, the duplicate is caught when the second
        # registration finds the first already on file. Through the web form it is caught
        # earlier, by the de-duplication in ``parse_lines``.
        with self.assertRaises(documents.DocumentError) as caught:
            self.buy(["SN-DUP", "SN-DUP"])
        self.assertIn("already recorded", str(caught.exception).lower())
        self.assertEqual(db.scalar("SELECT COUNT(*) FROM serials"), 0)

    def test_a_serial_already_on_file_is_refused(self):
        self.buy(["SN-B1"])
        with self.assertRaises(documents.DocumentError) as caught:
            self.buy(["SN-B1"], bill_number="SD/2026/2")
        self.assertIn("already recorded", str(caught.exception).lower())
        self.assertEqual(db.scalar("SELECT COUNT(*) FROM serials"), 1)

    def test_serial_matching_ignores_case(self):
        # Scanners and handwriting disagree about case. "sn-c1" and "SN-C1" are the same
        # physical camera, and letting both in would mean two warranty records for one box.
        self.buy(["SN-C1"])
        with self.assertRaises(documents.DocumentError):
            self.buy(["sn-c1"], bill_number="SD/2026/3")
        self.assertEqual(db.scalar("SELECT COUNT(*) FROM serials"), 1)

    def test_serials_on_a_product_not_marked_serial_tracked_are_refused(self):
        # Also a ``parse_lines`` rule rather than a document one.
        import json

        plain = self.make_product(sku="CABLE-1", name="CAT6 cable", quantity=0, is_serialized=0)
        with self.assertRaises(documents.DocumentError) as caught:
            documents.parse_lines(
                json.dumps(
                    [
                        {
                            "product_id": plain,
                            "qty": 1,
                            "unit_cost": "500",
                            "gst_rate_bp": 1800,
                            "hsn_code": "8544",
                            "description": "CAT6 cable",
                            "serials": "SN-NOPE",
                        }
                    ]
                ),
                price_field="unit_cost",
            )
        self.assertIn("serial-tracked", str(caught.exception).lower())

    def test_no_warranty_months_means_no_expiry_date_at_all(self):
        # An empty expiry is honest; today's date would read as "expired the day it arrived".
        no_warranty = self.make_product(
            sku="PSU-1", name="Power supply", quantity=0, is_serialized=1, warranty_months=0
        )
        self.buy(["SN-NOWARR"], product_id=no_warranty, bill_number="SD/NW", warranty_months=0)
        self.assertIsNone(self.serial("SN-NOWARR")["warranty_expiry"])


class WarrantyBasisTests(SerialBase):
    def test_purchase_basis_dates_the_warranty_from_the_dealer_bill(self):
        repo.update_shop_settings({"warranty_basis": "purchase"})
        bill_date = (TODAY - dt.timedelta(days=200)).isoformat()
        self.buy(["SN-P1"], bill_date=bill_date)

        expected = repo.add_months(dt.date.fromisoformat(bill_date), 24)
        self.assertEqual(self.serial("SN-P1")["warranty_expiry"], expected.isoformat())

    def test_purchase_basis_expiry_does_not_move_when_the_item_is_sold(self):
        repo.update_shop_settings({"warranty_basis": "purchase"})
        bill_date = (TODAY - dt.timedelta(days=200)).isoformat()
        self.buy(["SN-P2"], bill_date=bill_date)
        before = self.serial("SN-P2")["warranty_expiry"]

        self.sell(["SN-P2"])
        self.assertEqual(self.serial("SN-P2")["warranty_expiry"], before)

    def test_sale_basis_restarts_the_clock_when_the_customer_buys_it(self):
        # The default, and the one a customer would recognise: an item that sat in stock for
        # six months still gets its full 24 months from the day it was sold.
        repo.update_shop_settings({"warranty_basis": "sale"})
        bill_date = (TODAY - dt.timedelta(days=180)).isoformat()
        self.buy(["SN-S1"], bill_date=bill_date)

        from_purchase = repo.add_months(dt.date.fromisoformat(bill_date), 24)
        self.assertEqual(self.serial("SN-S1")["warranty_expiry"], from_purchase.isoformat())

        self.sell(["SN-S1"])
        from_sale = repo.add_months(TODAY, 24)
        self.assertEqual(self.serial("SN-S1")["warranty_expiry"], from_sale.isoformat())
        self.assertGreater(from_sale, from_purchase)

    def test_sale_basis_uses_the_invoice_date_not_the_day_it_was_typed(self):
        # A sale entered two days late must date the warranty from the invoice, or every
        # backdated entry shortchanges or overpays the customer.
        repo.update_shop_settings({"warranty_basis": "sale"})
        self.buy(["SN-S2"])
        invoice_date = (TODAY - dt.timedelta(days=2)).isoformat()
        self.sell(["SN-S2"], invoice_date=invoice_date)

        expected = repo.add_months(dt.date.fromisoformat(invoice_date), 24)
        self.assertEqual(self.serial("SN-S2")["warranty_expiry"], expected.isoformat())

    def test_the_default_basis_is_the_sale(self):
        self.assertEqual(
            db.scalar("SELECT warranty_basis FROM shop_settings WHERE id = 1", default=""),
            "sale",
        )


class SellSerialsTests(SerialBase):
    def test_selling_marks_the_serial_sold_and_links_the_invoice(self):
        self.buy(["SN-D1"])
        result = self.sell(["SN-D1"])

        row = self.serial("SN-D1")
        self.assertEqual(row["status"], "sold")
        self.assertEqual(row["sale_id"], result["sale_id"])
        self.assertIsNotNone(row["sale_item_id"])
        self.assertIsNotNone(row["sold_at"])

    def test_a_serial_that_was_never_purchased_cannot_be_sold(self):
        # Stops a typo at the counter from inventing a warranty record for a camera the
        # shop never bought. Stock is deliberately available here, so it is the serial
        # check that refuses rather than the stock check.
        self.buy(["SN-REAL1", "SN-REAL2"])
        with self.assertRaises(documents.DocumentError) as caught:
            self.sell(["SN-GHOST"])
        self.assertIn("not in stock", str(caught.exception).lower())
        self.assertEqual(db.scalar("SELECT COUNT(*) FROM sales"), 0)
        self.assertEqual(db.scalar("SELECT COUNT(*) FROM serials WHERE status = 'sold'"), 0)

    def test_an_unknown_serial_with_no_stock_is_refused_by_the_stock_check_first(self):
        # Documenting the order, because the message the counter sees differs: with nothing
        # on the shelf the quantity check fires before the serial is ever looked up. Both
        # refuse the sale, which is what matters.
        with self.assertRaises(documents.DocumentError) as caught:
            self.sell(["SN-GHOST"])
        self.assertIn("not enough stock", str(caught.exception).lower())
        self.assertEqual(db.scalar("SELECT COUNT(*) FROM sales"), 0)

    def test_the_same_serial_cannot_be_sold_twice(self):
        self.buy(["SN-D2", "SN-D3"])  # a spare, so stock is not what refuses the second sale
        self.sell(["SN-D2"])
        with self.assertRaises(documents.DocumentError) as caught:
            self.sell(["SN-D2"])
        self.assertIn("already", str(caught.exception).lower())
        self.assertEqual(
            db.scalar("SELECT COUNT(*) FROM sales WHERE is_void = 0"),
            1,
            "the second sale must not have been recorded",
        )

    def test_a_serial_belonging_to_another_product_is_refused(self):
        other = self.make_product(
            sku="NVR-1", name="8ch NVR", quantity=0, is_serialized=1, warranty_months=12
        )
        self.buy(["SN-CAM"], bill_number="SD/CAM")
        self.buy(["SN-NVR"], product_id=other, bill_number="SD/NVR", warranty_months=12)

        with self.assertRaises(documents.DocumentError) as caught:
            self.sell(["SN-NVR"])  # NVR serial on a camera line
        message = str(caught.exception)
        self.assertIn("8ch NVR", message)
        self.assertEqual(self.serial("SN-NVR")["status"], "in_stock")

    def test_selling_is_case_insensitive_about_the_serial(self):
        self.buy(["SN-E1"])
        self.sell(["sn-e1"])
        self.assertEqual(self.serial("SN-E1")["status"], "sold")

    def test_the_same_serial_on_two_lines_of_one_invoice_is_refused(self):
        self.buy(["SN-F1", "SN-F2"])
        with self.assertRaises(documents.DocumentError) as caught:
            documents.create_sale(
                invoice_date=TODAY_ISO,
                customer=documents.CustomerInput(name="Double biller"),
                lines=[
                    documents.LineInput(
                        product_id=self.product,
                        qty=1,
                        unit_price_paise=350_000,
                        gst_rate_bp=1800,
                        hsn_code="8525",
                        description="Camera",
                        serials=["SN-F1"],
                    ),
                    documents.LineInput(
                        product_id=self.product,
                        qty=1,
                        unit_price_paise=350_000,
                        gst_rate_bp=1800,
                        hsn_code="8525",
                        description="Camera",
                        serials=["SN-F1"],
                    ),
                ],
            )
        # Reached this way, the second line finds the serial already sold by the first, and
        # the whole invoice rolls back. Through the form, ``parse_lines`` catches it up
        # front with "repeated across lines" — either way it cannot be double-billed.
        self.assertIn("already marked", str(caught.exception).lower())
        self.assertEqual(self.serial("SN-F1")["status"], "in_stock")

    def test_a_failed_serial_sale_rolls_the_whole_invoice_back(self):
        # Two units in stock so qty=2 passes the stock check and the bad serial on the
        # second unit is what fails — otherwise this would only re-test the stock guard.
        self.buy(["SN-G1", "SN-G2"])
        with self.assertRaises(documents.DocumentError):
            documents.create_sale(
                invoice_date=TODAY_ISO,
                customer=documents.CustomerInput(name="Partial"),
                lines=[
                    documents.LineInput(
                        product_id=self.product,
                        qty=2,
                        unit_price_paise=350_000,
                        gst_rate_bp=1800,
                        hsn_code="8525",
                        description="Camera",
                        serials=["SN-G1", "SN-NOT-REAL"],
                    )
                ],
            )
        self.assertEqual(db.scalar("SELECT COUNT(*) FROM sales"), 0)
        self.assertEqual(self.serial("SN-G1")["status"], "in_stock")
        self.assertIsNone(self.serial("SN-G1")["sale_id"])
        self.assertStockLedgerAgrees(self.product)

    def test_selling_serials_also_moves_the_stock(self):
        self.buy(["SN-H1", "SN-H2", "SN-H3"])
        self.assertEqual(
            db.scalar("SELECT quantity FROM products WHERE id = ?", (self.product,)), 3
        )
        self.sell(["SN-H1", "SN-H2"])
        self.assertEqual(
            db.scalar("SELECT quantity FROM products WHERE id = ?", (self.product,)), 1
        )
        self.assertStockLedgerAgrees(self.product)
        self.assertEqual(
            db.scalar("SELECT COUNT(*) FROM serials WHERE status = 'in_stock'"), 1
        )


class SerialsAfterVoidTests(SerialBase):
    def test_voiding_a_sale_puts_the_serial_back_in_stock(self):
        self.buy(["SN-V1"])
        result = self.sell(["SN-V1"])
        documents.void_sale(result["sale_id"], reason="Customer returned it")

        row = self.serial("SN-V1")
        self.assertEqual(row["status"], "in_stock")
        self.assertIsNone(row["sale_id"])
        self.assertStockLedgerAgrees(self.product)

    def test_a_returned_serial_can_be_sold_again(self):
        self.buy(["SN-V2"])
        first = self.sell(["SN-V2"])
        documents.void_sale(first["sale_id"], reason="Wrong model supplied")

        second = self.sell(["SN-V2"], customer_name="Second buyer")
        self.assertEqual(self.serial("SN-V2")["sale_id"], second["sale_id"])
        self.assertEqual(self.serial("SN-V2")["status"], "sold")

    def test_voiding_recomputes_the_expiry_from_the_purchase_again(self):
        # Under sale-basis the sale had moved the expiry forward. Undoing the sale has to
        # undo that too, or a cancelled invoice leaves the item with a warranty it has not
        # earned.
        repo.update_shop_settings({"warranty_basis": "sale"})
        bill_date = (TODAY - dt.timedelta(days=150)).isoformat()
        self.buy(["SN-V3"], bill_date=bill_date)
        from_purchase = repo.add_months(dt.date.fromisoformat(bill_date), 24).isoformat()

        result = self.sell(["SN-V3"])
        self.assertNotEqual(self.serial("SN-V3")["warranty_expiry"], from_purchase)

        documents.void_sale(result["sale_id"], reason="Cancelled")
        self.assertEqual(self.serial("SN-V3")["warranty_expiry"], from_purchase)

    def test_a_purchase_can_be_cancelled_while_its_serials_are_still_in_stock(self):
        result = self.buy(["SN-W1", "SN-W2"])
        documents.void_purchase(result["purchase_id"], reason="Dealer sent the wrong model")

        self.assertEqual(db.scalar("SELECT COUNT(*) FROM serials"), 0)
        self.assertEqual(
            db.scalar("SELECT quantity FROM products WHERE id = ?", (self.product,)), 0
        )
        self.assertStockLedgerAgrees(self.product)

    def test_a_purchase_cannot_be_cancelled_once_a_serial_has_been_sold(self):
        # Deleting the serial would erase the warranty record for a camera a customer is
        # holding. Refusing, and saying which serial, is the only safe answer.
        result = self.buy(["SN-W3", "SN-W4"])
        self.sell(["SN-W3"])

        with self.assertRaises(documents.DocumentError) as caught:
            documents.void_purchase(result["purchase_id"], reason="Too late")
        message = str(caught.exception)
        self.assertIn("SN-W3", message.upper())
        self.assertIn("cancel the sale", message.lower())

        # Nothing was half-undone.
        self.assertEqual(db.scalar("SELECT COUNT(*) FROM serials"), 2)
        self.assertEqual(self.serial("SN-W4")["status"], "in_stock")
        self.assertStockLedgerAgrees(self.product)


class WarrantyLookupTests(SerialBase):
    """What the counter actually asks: is this box still covered, and who bought it?"""

    def setUp(self) -> None:
        super().setUp()
        repo.update_shop_settings({"warranty_basis": "sale"})
        self.buy(["SN-L1", "SN-L2"])

    def test_a_sold_serial_can_be_found_by_its_number(self):
        self.sell(["SN-L1"], customer_name="Mrs Joshi", phone="9812345678")
        row = db.query_one(
            """
            SELECT s.serial_no, s.status, s.warranty_expiry,
                   sa.invoice_number, sa.customer_name, sa.customer_phone
            FROM serials s
            LEFT JOIN sales sa ON sa.id = s.sale_id
            WHERE s.serial_no = ?
            """,
            ("SN-L1",),
        )
        self.assertIsNotNone(row)
        self.assertEqual(row["customer_name"], "Mrs Joshi")
        self.assertEqual(row["customer_phone"], "9812345678")
        self.assertTrue(row["invoice_number"])
        self.assertEqual(row["warranty_expiry"], repo.add_months(TODAY, 24).isoformat())

    def test_lookup_by_serial_ignores_case(self):
        # The scanner and the sticker do not always agree, and neither does handwriting.
        self.sell(["SN-L1"])
        row = db.query_one("SELECT status FROM serials WHERE serial_no = ?", ("sn-l1",))
        self.assertIsNotNone(row, "a lowercase serial must still find the record")
        self.assertEqual(row["status"], "sold")

    def test_a_customer_can_be_found_by_name_and_lists_their_serials(self):
        self.sell(["SN-L1"], customer_name="Mr Patil")
        self.sell(["SN-L2"], customer_name="mr patil")  # same person, typed differently

        rows = db.query(
            """
            SELECT s.serial_no FROM serials s
            JOIN sales sa ON sa.id = s.sale_id
            WHERE sa.customer_name = ? COLLATE NOCASE
            ORDER BY s.serial_no
            """,
            ("Mr Patil",),
        )
        self.assertEqual([r["serial_no"] for r in rows], ["SN-L1", "SN-L2"])

    def test_an_unsold_serial_has_no_customer_but_still_has_an_expiry(self):
        row = self.serial("SN-L2")
        self.assertEqual(row["status"], "in_stock")
        self.assertIsNone(row["sale_id"])
        self.assertIsNotNone(row["warranty_expiry"])

    def test_expiry_dates_sort_and_compare_as_dates(self):
        # Stored as ISO text so that a plain SQL comparison answers "expired?" correctly.
        # Any other format silently breaks the in-warranty filter.
        self.sell(["SN-L1"])
        expiry = self.serial("SN-L1")["warranty_expiry"]
        self.assertRegex(expiry, r"^\d{4}-\d{2}-\d{2}$")

        still_covered = db.scalar(
            "SELECT COUNT(*) FROM serials WHERE warranty_expiry >= ?", (TODAY_ISO,)
        )
        self.assertEqual(still_covered, 2)
        expired = db.scalar(
            "SELECT COUNT(*) FROM serials WHERE warranty_expiry < ?", (TODAY_ISO,)
        )
        self.assertEqual(expired, 0)


if __name__ == "__main__":
    unittest.main()
