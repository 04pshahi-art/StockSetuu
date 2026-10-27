"""Sales invoicing: the GST split, and the invoice series.

Two things in this file matter more than the rest of the app's arithmetic put together.

**The tax split.** A sale to a Maharashtra customer is CGST + SGST at half the rate each;
a sale to anyone else is IGST at the full rate. Getting this backwards does not produce a
crash, it produces a return that has to be amended. A blank state code means a walk-in at
the counter, which is local.

**The series.** GST requires invoice numbers to be sequential with no gaps. That is why
``create_sale`` allocates the number *inside* the same transaction as the insert: if a sale
fails halfway — out of stock, a bad serial — the rollback must take the number back with
it. And it is why voiding keeps the number instead of deleting the row, because deleting
the last invoice of the day would leave exactly the gap the rule forbids.

The per-rupee arithmetic itself is pinned in test_gst.py; these tests are about the
document.
"""

from __future__ import annotations

import datetime as dt
import unittest

from app import db, documents, gst, repo

from tests.support import ShopTestCase

TODAY = dt.date.today().isoformat()

MAHARASHTRA = "27"
KARNATAKA = "29"


def line(product_id: int, **kw) -> documents.LineInput:
    """A one-unit ₹1,000 line at 18%, unless overridden."""
    return documents.LineInput(
        product_id=product_id,
        qty=kw.pop("qty", 1),
        unit_price_paise=kw.pop("unit_price_paise", 100_000),
        gst_rate_bp=kw.pop("gst_rate_bp", 1800),
        hsn_code=kw.pop("hsn_code", "8473"),
        description=kw.pop("description", "Test item"),
        **kw,
    )


def customer(**kw) -> documents.CustomerInput:
    return documents.CustomerInput(
        name=kw.pop("name", "Walk-in"),
        phone=kw.pop("phone", ""),
        gstin=kw.pop("gstin", ""),
        state_code=kw.pop("state_code", ""),
        address=kw.pop("address", ""),
    )


class SaleBase(ShopTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.set_shop_state(MAHARASHTRA)
        self.product = self.stocked_product(sku="MB-1", name="Motherboard", quantity=100)

    def stocked_product(self, *, quantity: int = 0, **kw) -> int:
        """A product whose opening stock went through the movement ledger.

        ``make_product(quantity=N)`` writes ``products.quantity`` straight into the row,
        which is fine for a fixture but leaves the cache and the ledger disagreeing before
        the test has done anything. Anything asserting ``assertStockLedgerAgrees`` has to
        book its opening stock the way the app does.
        """
        product_id = self.make_product(quantity=0, **kw)
        if quantity:
            repo.set_stock(
                product_id=product_id, new_quantity=quantity, note="Opening stock (fixture)"
            )
        return product_id

    def sale(self, **kw):
        return documents.create_sale(
            invoice_date=kw.pop("invoice_date", TODAY),
            customer=kw.pop("customer", customer()),
            lines=kw.pop("lines", [line(self.product)]),
            **kw,
        )

    def sale_row(self, sale_id: int) -> db.Row:
        row = db.query_one("SELECT * FROM sales WHERE id = ?", (sale_id,))
        assert row is not None
        return row


class TaxSplitTests(SaleBase):
    def test_blank_state_is_treated_as_local(self):
        # The counter case: no GSTIN, no state asked for. Must not become IGST.
        result = self.sale(customer=customer(name="Cash customer"))
        row = self.sale_row(result["sale_id"])

        self.assertEqual(row["interstate"], 0)
        self.assertEqual(row["igst_paise"], 0)
        self.assertGreater(row["cgst_paise"], 0)
        self.assertEqual(row["cgst_paise"], row["sgst_paise"])
        self.assertFalse(result["interstate"])

    def test_own_state_code_is_local(self):
        row = self.sale_row(self.sale(customer=customer(state_code=MAHARASHTRA))["sale_id"])
        self.assertEqual(row["interstate"], 0)
        self.assertEqual(row["igst_paise"], 0)
        self.assertEqual(row["cgst_paise"], row["sgst_paise"])

    def test_other_state_is_igst_only(self):
        result = self.sale(customer=customer(name="Bengaluru buyer", state_code=KARNATAKA))
        row = self.sale_row(result["sale_id"])

        self.assertEqual(row["interstate"], 1)
        self.assertEqual(row["cgst_paise"], 0)
        self.assertEqual(row["sgst_paise"], 0)
        self.assertGreater(row["igst_paise"], 0)
        self.assertTrue(result["interstate"])

    def test_gstin_decides_the_state_when_no_state_is_typed(self):
        # A GSTIN carries its state in the first two digits. An out-of-state GSTIN with a
        # blank state field must still be IGST — otherwise a B2B interstate sale silently
        # goes out as CGST/SGST.
        local = self._gstin_for(MAHARASHTRA)
        away = self._gstin_for(KARNATAKA)

        self.assertEqual(
            self.sale_row(self.sale(customer=customer(gstin=local))["sale_id"])["interstate"], 0
        )
        self.assertEqual(
            self.sale_row(self.sale(customer=customer(gstin=away))["sale_id"])["interstate"], 1
        )

    def test_shop_in_another_state_flips_which_customers_are_local(self):
        # Nothing about the shop's own state is hardcoded, so a shop registered in
        # Karnataka must treat Karnataka as local and Maharashtra as interstate.
        self.set_shop_state(KARNATAKA)
        self.assertEqual(
            self.sale_row(self.sale(customer=customer(state_code=KARNATAKA))["sale_id"])[
                "interstate"
            ],
            0,
        )
        self.assertEqual(
            self.sale_row(self.sale(customer=customer(state_code=MAHARASHTRA))["sale_id"])[
                "interstate"
            ],
            1,
        )

    def test_cgst_always_equals_sgst_across_rates_and_quantities(self):
        # The half-and-half split is the only part of the local case that is guaranteed.
        # cgst + sgst == igst is NOT universally true once rounding lands on an odd paisa,
        # which is why this asserts the halves against each other and nothing more.
        for rate_bp in (0, 500, 1200, 1800, 2800):
            for qty, price in ((1, 99_999), (3, 33_333), (7, 100_001)):
                with self.subTest(rate_bp=rate_bp, qty=qty, price=price):
                    row = self.sale_row(
                        self.sale(
                            lines=[
                                line(
                                    self.product,
                                    qty=qty,
                                    unit_price_paise=price,
                                    gst_rate_bp=rate_bp,
                                )
                            ]
                        )["sale_id"]
                    )
                    self.assertEqual(row["cgst_paise"], row["sgst_paise"])
                    self.assertEqual(row["igst_paise"], 0)

    def test_stored_header_totals_match_the_stored_lines(self):
        # The header is a summary of the lines; a report reads one and the invoice shows
        # the other, so a disagreement here is a mismatch the shop would find at filing.
        second = self.make_product(sku="RAM-1", name="RAM 8GB", quantity=50, gst_rate_bp=2800)
        result = self.sale(
            lines=[
                line(self.product, qty=2, unit_price_paise=145_500),
                line(second, qty=3, unit_price_paise=88_800, gst_rate_bp=2800),
            ]
        )
        row = self.sale_row(result["sale_id"])
        items = db.query("SELECT * FROM sale_items WHERE sale_id = ?", (result["sale_id"],))

        self.assertEqual(len(items), 2)
        for column in ("taxable_paise", "cgst_paise", "sgst_paise", "igst_paise"):
            self.assertEqual(
                row[column],
                sum(i[column] for i in items),
                f"header {column} does not add up from the lines",
            )
        self.assertEqual(
            row["total_paise"],
            sum(i["total_paise"] for i in items) + row["round_off_paise"],
        )
        self.assertEqual(result["total_paise"], row["total_paise"])

    def test_rounding_to_the_rupee_leaves_a_whole_rupee_total(self):
        row = self.sale_row(
            self.sale(
                lines=[line(self.product, unit_price_paise=99_999)], round_to_rupee=True
            )["sale_id"]
        )
        self.assertEqual(row["total_paise"] % 100, 0)
        # The round-off is disclosed as its own field, not folded silently into the tax.
        self.assertEqual(
            row["total_paise"] - row["round_off_paise"],
            row["taxable_paise"] + row["cgst_paise"] + row["sgst_paise"] + row["igst_paise"],
        )

    def test_not_rounding_keeps_the_exact_paise(self):
        row = self.sale_row(
            self.sale(
                lines=[line(self.product, unit_price_paise=99_999)], round_to_rupee=False
            )["sale_id"]
        )
        self.assertEqual(row["round_off_paise"], 0)
        self.assertEqual(
            row["total_paise"],
            row["taxable_paise"] + row["cgst_paise"] + row["sgst_paise"] + row["igst_paise"],
        )

    def _gstin_for(self, state_code: str) -> str:
        body = f"{state_code}BXZPS5663N1Z"
        return body + gst.gstin_check_digit(body[:14])


class InvoiceSeriesTests(SaleBase):
    def test_numbers_are_sequential_and_gap_free(self):
        numbers = [self.sale()["invoice_number"] for _ in range(5)]
        sequences = [
            r["invoice_seq"] for r in db.query("SELECT invoice_seq FROM sales ORDER BY id")
        ]
        self.assertEqual(sequences, [1, 2, 3, 4, 5])
        self.assertEqual(len(set(numbers)), 5)

    def test_number_carries_the_prefix_and_the_financial_year(self):
        number = self.sale()["invoice_number"]
        prefix, label, seq = number.split("/")
        self.assertEqual(label, gst.fy_label(dt.date.fromisoformat(TODAY)))
        self.assertEqual(seq, "0001")
        self.assertTrue(prefix)

    def test_the_prefix_comes_from_settings_not_from_the_code(self):
        from app import repo

        repo.update_shop_settings({"invoice_prefix": "PCS"})
        self.assertTrue(self.sale()["invoice_number"].startswith("PCS/"))

    def test_a_failed_sale_does_not_burn_a_number(self):
        # The whole reason the counter is allocated inside the transaction. Sale 1 succeeds,
        # sale 2 fails on stock, sale 3 must be number 2 — not number 3 with a hole at 2.
        self.assertEqual(self.sale()["invoice_number"].split("/")[-1], "0001")

        short = self.make_product(sku="OUT-1", name="Out of stock", quantity=0)
        with self.assertRaises(documents.DocumentError):
            self.sale(lines=[line(short)])

        self.assertEqual(self.sale()["invoice_number"].split("/")[-1], "0002")
        self.assertEqual(db.scalar("SELECT COUNT(*) FROM sales"), 2)

    def test_a_failed_sale_leaves_no_partial_row_or_stock_movement(self):
        good = self.stocked_product(sku="GOOD-1", name="In stock", quantity=5)
        short = self.make_product(sku="SHORT-1", name="Not in stock", quantity=0)

        with self.assertRaises(documents.DocumentError):
            self.sale(lines=[line(good), line(short)])

        self.assertEqual(db.scalar("SELECT COUNT(*) FROM sales"), 0)
        self.assertEqual(db.scalar("SELECT COUNT(*) FROM sale_items"), 0)
        # The first line had already moved stock before the second one failed.
        self.assertEqual(db.scalar("SELECT quantity FROM products WHERE id = ?", (good,)), 5)
        self.assertEqual(
            db.scalar("SELECT COUNT(*) FROM stock_movements WHERE ref_type = 'sale'"), 0
        )
        self.assertStockLedgerAgrees(good)

    def test_the_service_series_is_separate_from_the_invoice_series(self):
        # A job card must not consume an invoice number, or the sales register develops a
        # gap that nothing in the sales data explains.
        self.sale()
        documents.create_service_job(
            job_date=TODAY,
            customer_name="Service customer",
            description="Desktop PC — no display",
            amount_paise=50_000,
        )
        self.assertEqual(self.sale()["invoice_number"].split("/")[-1], "0002")
        self.assertEqual(db.scalar("SELECT COUNT(*) FROM service_jobs"), 1)

    def test_next_invoice_number_consumes_the_number_it_returns(self):
        # Pinning the real contract, which is the opposite of what it looks like: this
        # function does not "peek" at the next number, it *takes* it. Two calls give two
        # different numbers. That is correct — but it means it is only ever safe to call
        # inside the transaction that writes the sale.
        conn = db.get_connection()
        first, seq, label = documents.next_invoice_number(dt.date.fromisoformat(TODAY), conn)
        second, seq2, _ = documents.next_invoice_number(dt.date.fromisoformat(TODAY), conn)

        self.assertEqual((seq, seq2), (1, 2))
        self.assertNotEqual(first, second)
        self.assertEqual(label, gst.fy_label(dt.date.fromisoformat(TODAY)))

    def test_nothing_allocates_an_invoice_number_outside_a_save(self):
        # The companion to the test above, and the reason it is worth writing. If someone
        # adds a "show the next invoice number" preview to the new-sale form, every time
        # the counter opens that form and does not save, the series gains a gap — which is
        # precisely what GST forbids. A grep is the only way to catch that before it ships.
        import pathlib
        import re

        root = pathlib.Path(__file__).resolve().parent.parent
        allowed = {"app/documents.py"}
        offenders = []
        for path in sorted(root.glob("app/**/*.py")):
            relative = path.relative_to(root).as_posix()
            if relative in allowed:
                continue
            for lineno, text in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if text.lstrip().startswith("#"):
                    continue
                if re.search(r"\bnext_(invoice|job)_number\s*\(", text):
                    offenders.append(f"{relative}:{lineno}: {text.strip()}")
        self.assertEqual(
            offenders,
            [],
            "these allocate a document number outside documents.py, where it cannot be "
            "rolled back with the save — every unsaved form would leave a gap in the "
            "series:\n  " + "\n  ".join(offenders),
        )


class VoidSaleTests(SaleBase):
    def test_voiding_keeps_the_number_and_the_row(self):
        result = self.sale()
        documents.void_sale(result["sale_id"], reason="Customer changed their mind")

        row = self.sale_row(result["sale_id"])
        self.assertEqual(row["is_void"], 1)
        self.assertEqual(row["invoice_number"], result["invoice_number"])
        self.assertEqual(row["void_reason"], "Customer changed their mind")
        self.assertIsNotNone(row["voided_at"])
        # The tax figures survive, because the register still has to show the cancelled
        # invoice rather than pretend it never existed.
        self.assertGreater(row["total_paise"], 0)

    def test_voiding_does_not_create_a_gap_in_the_series(self):
        first = self.sale()
        documents.void_sale(first["sale_id"], reason="Wrong item billed")
        second = self.sale()

        self.assertEqual(first["invoice_number"].split("/")[-1], "0001")
        self.assertEqual(second["invoice_number"].split("/")[-1], "0002")
        self.assertEqual(
            [r["invoice_seq"] for r in db.query("SELECT invoice_seq FROM sales ORDER BY id")],
            [1, 2],
        )

    def test_voiding_returns_the_stock(self):
        before = db.scalar("SELECT quantity FROM products WHERE id = ?", (self.product,))
        result = self.sale(lines=[line(self.product, qty=4)])
        self.assertEqual(
            db.scalar("SELECT quantity FROM products WHERE id = ?", (self.product,)),
            before - 4,
        )

        documents.void_sale(result["sale_id"], reason="Returned unopened")
        self.assertEqual(
            db.scalar("SELECT quantity FROM products WHERE id = ?", (self.product,)), before
        )
        self.assertStockLedgerAgrees(self.product)

    def test_the_return_is_a_new_movement_not_an_erased_one(self):
        # An audit has to be able to see that stock went out and came back, so the void
        # writes a second movement rather than deleting the first.
        result = self.sale(lines=[line(self.product, qty=2)])
        documents.void_sale(result["sale_id"], reason="Cancelled")

        moves = db.query(
            "SELECT delta, ref_type FROM stock_movements "
            "WHERE product_id = ? AND ref_type IN ('sale', 'sale_void') ORDER BY id",
            (self.product,),
        )
        self.assertEqual([m["delta"] for m in moves], [-2, 2])
        self.assertEqual([m["ref_type"] for m in moves], ["sale", "sale_void"])

    def test_voiding_twice_does_not_return_the_stock_twice(self):
        before = db.scalar("SELECT quantity FROM products WHERE id = ?", (self.product,))
        result = self.sale(lines=[line(self.product, qty=3)])
        documents.void_sale(result["sale_id"], reason="First")
        try:
            documents.void_sale(result["sale_id"], reason="Second")
        except documents.DocumentError:
            pass  # Refusing is the better answer; either way the stock must be right.

        self.assertEqual(
            db.scalar("SELECT quantity FROM products WHERE id = ?", (self.product,)), before
        )
        self.assertStockLedgerAgrees(self.product)


class SaleValidationTests(SaleBase):
    def test_future_dated_invoice_is_refused(self):
        tomorrow = (dt.date.today() + dt.timedelta(days=1)).isoformat()
        with self.assertRaises(documents.DocumentError) as caught:
            self.sale(invoice_date=tomorrow)
        self.assertIn("future", str(caught.exception).lower())

    def test_a_sale_with_no_lines_is_refused(self):
        with self.assertRaises(documents.DocumentError):
            self.sale(lines=[])

    def test_an_invalid_gstin_is_refused_before_a_number_is_taken(self):
        with self.assertRaises(documents.DocumentError):
            self.sale(customer=customer(gstin="27ABCDE1234F1Z9"))
        self.assertEqual(db.scalar("SELECT COUNT(*) FROM sales"), 0)
        # And the next real sale still starts at 1.
        self.assertEqual(self.sale()["invoice_number"].split("/")[-1], "0001")

    def test_selling_more_than_is_in_stock_is_refused_with_the_numbers(self):
        thin = self.make_product(sku="THIN-1", name="Nearly gone", quantity=2)
        with self.assertRaises(documents.DocumentError) as caught:
            self.sale(lines=[line(thin, qty=5)])
        message = str(caught.exception)
        self.assertIn("Nearly gone", message)
        self.assertIn("2", message)


class ParseLinesTests(ShopTestCase):
    """The line editor posts JSON; this is the boundary that has to distrust it."""

    def setUp(self) -> None:
        super().setUp()
        self.product = self.make_product(sku="P-1", name="Parsed item", quantity=10)

    def parse(self, payload: str, **kw):
        return documents.parse_lines(payload, price_field=kw.pop("price_field", "unit_price"), **kw)

    def good_payload(self, **over) -> str:
        import json

        row = {
            "product_id": self.product,
            "qty": 1,
            "unit_price": "1000",
            "gst_rate_bp": 1800,
            "hsn_code": "8473",
            "description": "Parsed item",
        }
        row.update(over)
        return json.dumps([row])

    def test_a_well_formed_payload_parses(self):
        lines = self.parse(self.good_payload())
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0].product_id, self.product)
        self.assertEqual(lines[0].qty, 1)

    def test_malformed_json_is_a_message_not_a_traceback(self):
        with self.assertRaises(documents.DocumentError) as caught:
            self.parse("{not json")
        self.assertIn("line items", str(caught.exception).lower())

    def test_an_empty_list_is_refused(self):
        with self.assertRaises(documents.DocumentError):
            self.parse("[]")

    def test_a_product_that_no_longer_exists_is_refused(self):
        with self.assertRaises(documents.DocumentError) as caught:
            self.parse(self.good_payload(product_id=999_999))
        self.assertIn("999999", str(caught.exception).replace(",", ""))

    def test_a_zero_price_is_refused(self):
        with self.assertRaises(documents.DocumentError):
            self.parse(self.good_payload(unit_price="0"))

    def test_a_discount_larger_than_the_line_is_refused(self):
        with self.assertRaises(documents.DocumentError) as caught:
            self.parse(self.good_payload(unit_price="1000", discount="5000"))
        self.assertIn("discount", str(caught.exception).lower())

    def test_an_impossible_gst_rate_is_refused(self):
        # Basis points, not percent: 1800 is 18%. Anything below 0 or above 10000 is not a
        # GST rate that exists.
        for rate_bp in (-500, 15_000):
            with self.subTest(rate_bp=rate_bp):
                with self.assertRaises(documents.DocumentError):
                    self.parse(self.good_payload(gst_rate_bp=rate_bp))

    def test_a_blank_gst_rate_falls_back_to_the_product(self):
        # The line editor leaves the field empty unless it is overridden, so the product's
        # own slab has to be what gets used rather than zero.
        lines = self.parse(self.good_payload(gst_rate_bp=""))
        self.assertEqual(lines[0].gst_rate_bp, 1800)

    def test_serials_on_a_product_that_is_not_serial_tracked_are_refused(self):
        with self.assertRaises(documents.DocumentError) as caught:
            self.parse(self.good_payload(serials="SN-1"))
        self.assertIn("serial", str(caught.exception).lower())

    def test_the_price_field_name_differs_between_sales_and_purchases(self):
        # Purchases post "unit_cost", sales post "unit_price". Reading the wrong key would
        # silently produce a zero price rather than an error.
        import json

        payload = json.dumps(
            [
                {
                    "product_id": self.product,
                    "qty": 1,
                    "unit_cost": "800",
                    "gst_rate_bp": 1800,
                    "hsn_code": "8473",
                    "description": "Parsed item",
                }
            ]
        )
        lines = self.parse(payload, price_field="unit_cost")
        self.assertEqual(lines[0].unit_price_paise, 80_000)
        with self.assertRaises(documents.DocumentError):
            self.parse(payload, price_field="unit_price")


if __name__ == "__main__":
    unittest.main()
