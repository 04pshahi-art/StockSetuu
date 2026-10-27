"""Stock: the ledger is the truth, the cached quantity is a convenience.

``products.quantity`` exists so the product list can be rendered without summing a
movement table on every row. It is a cache. ``stock_movements`` is the record. Every code
path that changes stock has to write both, and replaying the ledger has to reproduce the
cache exactly — otherwise the number on the shelf label and the number in the audit trail
drift apart, and there is no way afterwards to tell which one was ever right.

That invariant is what most of this file asserts, across every route stock can move:
purchase in, sale out, service job out, and each of their cancellations. The rest is about
refusing to go negative, because a shop cannot sell four of something it has two of, and a
silently negative count is how phantom stock gets ordered against.
"""

from __future__ import annotations

import datetime as dt
import unittest

from app import db, documents, repo

from tests.support import ShopTestCase

TODAY = dt.date.today().isoformat()


class MovementTests(ShopTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.product = self.make_product(sku="ST-1", name="Cabinet", quantity=0)

    def move(self, delta: int, **kw) -> int:
        return repo.apply_stock_movement(
            product_id=kw.pop("product_id", self.product),
            delta=delta,
            ref_type=kw.pop("ref_type", "manual"),
            ref_id=kw.pop("ref_id", None),
            **kw,
        )

    def quantity(self, product_id: int | None = None) -> int:
        return db.scalar(
            "SELECT quantity FROM products WHERE id = ?", (product_id or self.product,)
        )

    def test_a_movement_updates_the_cache_and_writes_the_ledger(self):
        with db.transaction():
            self.move(10, note="Opening")
        self.assertEqual(self.quantity(), 10)
        self.assertEqual(db.scalar("SELECT COUNT(*) FROM stock_movements"), 1)
        self.assertStockLedgerAgrees(self.product)

    def test_movements_accumulate_in_both_places(self):
        with db.transaction():
            self.move(10)
            self.move(-3)
            self.move(5)
            self.move(-2)
        self.assertEqual(self.quantity(), 10)
        self.assertStockLedgerAgrees(self.product)
        self.assertEqual(
            [r["delta"] for r in db.query("SELECT delta FROM stock_movements ORDER BY id")],
            [10, -3, 5, -2],
        )

    def test_a_zero_delta_writes_nothing(self):
        # Saving a line with quantity zero should not leave a meaningless ledger entry that
        # someone later has to interpret.
        with db.transaction():
            self.assertEqual(self.move(0), 0)
        self.assertEqual(db.scalar("SELECT COUNT(*) FROM stock_movements"), 0)

    def test_going_negative_is_refused_and_says_what_is_short(self):
        with db.transaction():
            self.move(2)
        with self.assertRaises(repo.StockError) as caught:
            with db.transaction():
                self.move(-5)
        message = str(caught.exception)
        self.assertIn("Cabinet", message)
        self.assertIn("2", message)
        self.assertIn("5", message)

    def test_a_refused_movement_changes_nothing(self):
        with db.transaction():
            self.move(2)
        with self.assertRaises(repo.StockError):
            with db.transaction():
                self.move(-5)
        self.assertEqual(self.quantity(), 2)
        self.assertEqual(db.scalar("SELECT COUNT(*) FROM stock_movements"), 1)
        self.assertStockLedgerAgrees(self.product)

    def test_going_to_exactly_zero_is_allowed(self):
        # The boundary: selling the last one is normal, not an error.
        with db.transaction():
            self.move(3)
            self.move(-3)
        self.assertEqual(self.quantity(), 0)
        self.assertStockLedgerAgrees(self.product)

    def test_negative_is_allowed_only_when_explicitly_asked_for(self):
        # A stock-take correcting a count downwards may legitimately land below zero if the
        # shelf was wrong; that is the one case that opts in.
        with db.transaction():
            repo.apply_stock_movement(
                product_id=self.product,
                delta=-4,
                ref_type="adjustment",
                ref_id=None,
                allow_negative=True,
            )
        self.assertEqual(self.quantity(), -4)
        self.assertStockLedgerAgrees(self.product)

    def test_a_movement_against_a_missing_product_is_refused(self):
        with self.assertRaises(repo.StockError) as caught:
            with db.transaction():
                self.move(1, product_id=999_999)
        self.assertIn("999999", str(caught.exception).replace(",", ""))

    def test_the_reason_and_reference_are_recorded(self):
        # A bare number in the ledger is not an audit trail; it has to say where it came
        # from, or a discrepancy months later cannot be traced to a document.
        with db.transaction():
            self.move(6, ref_type="purchase", ref_id=42, note="Bill 991")
        row = db.query_one("SELECT * FROM stock_movements ORDER BY id DESC LIMIT 1")
        self.assertEqual(row["ref_type"], "purchase")
        self.assertEqual(row["ref_id"], 42)
        self.assertEqual(row["note"], "Bill 991")


class SetStockTests(ShopTestCase):
    """A stock-take: state the count you counted, and let the app work out the delta."""

    def setUp(self) -> None:
        super().setUp()
        self.product = self.make_product(sku="STK-1", name="Keyboard", quantity=0)

    def test_setting_a_count_writes_the_difference_to_the_ledger(self):
        repo.set_stock(product_id=self.product, new_quantity=25, note="Opening count")
        self.assertEqual(
            db.scalar("SELECT quantity FROM products WHERE id = ?", (self.product,)), 25
        )
        self.assertStockLedgerAgrees(self.product)

        repo.set_stock(product_id=self.product, new_quantity=18, note="Counted again")
        self.assertEqual(
            db.scalar("SELECT quantity FROM products WHERE id = ?", (self.product,)), 18
        )
        self.assertStockLedgerAgrees(self.product)
        # The second correction is a -7 movement, not a rewritten 18.
        self.assertEqual(
            [r["delta"] for r in db.query("SELECT delta FROM stock_movements ORDER BY id")],
            [25, -7],
        )

    def test_setting_the_same_count_is_not_recorded_as_a_change(self):
        repo.set_stock(product_id=self.product, new_quantity=5, note="First")
        repo.set_stock(product_id=self.product, new_quantity=5, note="No change")
        self.assertEqual(db.scalar("SELECT COUNT(*) FROM stock_movements"), 1)
        self.assertStockLedgerAgrees(self.product)

    def test_a_stock_take_may_correct_downwards_past_zero(self):
        repo.set_stock(product_id=self.product, new_quantity=-2, note="Shelf was wrong")
        self.assertEqual(
            db.scalar("SELECT quantity FROM products WHERE id = ?", (self.product,)), -2
        )
        self.assertStockLedgerAgrees(self.product)


class PurchaseStockTests(ShopTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.dealer = self.make_dealer(name="Nehru Place Traders")
        self.product = self.make_product(
            sku="HDD-1", name="1TB HDD", quantity=0, cost_price_paise=350_000
        )

    def purchase(self, **kw):
        qty = kw.pop("qty", 10)
        cost = kw.pop("unit_cost_paise", 320_000)
        taxable = qty * cost
        return documents.create_purchase(
            dealer_id=kw.pop("dealer_id", self.dealer),
            bill_number=kw.pop("bill_number", "NPT/2026/117"),
            bill_date=kw.pop("bill_date", TODAY),
            lines=kw.pop(
                "lines",
                [
                    documents.LineInput(
                        product_id=self.product,
                        qty=qty,
                        unit_price_paise=cost,
                        gst_rate_bp=1800,
                        hsn_code="8471",
                        description="1TB HDD",
                    )
                ],
            ),
            taxes=kw.pop(
                "taxes",
                documents.PurchaseTaxInput(
                    taxable_paise=taxable,
                    cgst_paise=taxable * 9 // 100,
                    sgst_paise=taxable * 9 // 100,
                    total_paise=taxable + 2 * (taxable * 9 // 100),
                ),
            ),
            **kw,
        )

    def test_a_purchase_increases_stock_through_the_ledger(self):
        self.purchase(qty=10)
        self.assertEqual(
            db.scalar("SELECT quantity FROM products WHERE id = ?", (self.product,)), 10
        )
        self.assertStockLedgerAgrees(self.product)
        row = db.query_one(
            "SELECT ref_type, delta FROM stock_movements WHERE product_id = ?", (self.product,)
        )
        self.assertEqual(row["ref_type"], "purchase")
        self.assertEqual(row["delta"], 10)

    def test_the_movement_points_at_the_purchase_it_came_from(self):
        result = self.purchase(qty=4)
        purchase_id = result["purchase_id"]
        row = db.query_one(
            "SELECT ref_id FROM stock_movements WHERE product_id = ?", (self.product,)
        )
        self.assertEqual(row["ref_id"], purchase_id)

    def test_the_same_bill_number_from_the_same_dealer_is_refused(self):
        # Entering the same dealer bill twice is the easiest way to double the stock and
        # the ITC. The guard is case-insensitive because nobody types a bill number the
        # same way twice.
        self.purchase(bill_number="NPT/2026/500")
        with self.assertRaises(documents.DocumentError) as caught:
            self.purchase(bill_number="npt/2026/500")
        self.assertIn("already entered", str(caught.exception).lower())
        self.assertEqual(
            db.scalar("SELECT quantity FROM products WHERE id = ?", (self.product,)), 10
        )
        self.assertStockLedgerAgrees(self.product)

    def test_the_same_bill_number_from_a_different_dealer_is_fine(self):
        # Two dealers can each have a bill numbered 1. The guard is per dealer.
        other = self.make_dealer(name="Lamington Road Supply")
        self.purchase(bill_number="1")
        self.purchase(bill_number="1", dealer_id=other)
        self.assertEqual(
            db.scalar("SELECT quantity FROM products WHERE id = ?", (self.product,)), 20
        )
        self.assertStockLedgerAgrees(self.product)

    def test_a_purchase_updates_the_cost_price_when_asked(self):
        self.purchase(unit_cost_paise=299_000)
        self.assertEqual(
            db.scalar("SELECT cost_price_paise FROM products WHERE id = ?", (self.product,)),
            299_000,
        )

    def test_a_purchase_can_leave_the_cost_price_alone(self):
        # A one-off small-quantity buy at a bad price should not reprice the catalogue.
        self.purchase(unit_cost_paise=990_000, update_cost_prices=False)
        self.assertEqual(
            db.scalar("SELECT cost_price_paise FROM products WHERE id = ?", (self.product,)),
            350_000,
        )

    def test_dealer_gst_is_stored_as_printed_not_recalculated(self):
        # The purchase register has to mirror the dealer's physical bill. If the dealer's
        # arithmetic is a rupee out, the register shows the dealer's number — recomputing
        # it would make the register disagree with the paper it is evidence for.
        odd = documents.PurchaseTaxInput(
            taxable_paise=100_000,
            cgst_paise=9_001,  # deliberately not exactly 9%
            sgst_paise=8_999,
            round_off_paise=0,
            total_paise=118_000,
        )
        self.purchase(qty=1, unit_cost_paise=100_000, taxes=odd, bill_number="ODD/1")
        row = db.query_one(
            "SELECT cgst_paise, sgst_paise, total_paise FROM purchases "
            "WHERE bill_number = 'ODD/1'"
        )
        self.assertEqual(row["cgst_paise"], 9_001)
        self.assertEqual(row["sgst_paise"], 8_999)
        self.assertEqual(row["total_paise"], 118_000)

    def test_cancelling_a_purchase_takes_the_stock_back_out(self):
        result = self.purchase(qty=6)
        purchase_id = result["purchase_id"]

        documents.void_purchase(purchase_id, reason="Dealer took it back")
        self.assertEqual(
            db.scalar("SELECT quantity FROM products WHERE id = ?", (self.product,)), 0
        )
        self.assertStockLedgerAgrees(self.product)
        self.assertEqual(
            [r["delta"] for r in db.query("SELECT delta FROM stock_movements ORDER BY id")],
            [6, -6],
        )


class ServiceJobStockTests(ShopTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.product = self.make_product(sku="SMPS-1", name="SMPS 450W", quantity=0)
        repo.set_stock(product_id=self.product, new_quantity=8, note="Opening")

    def job(self, **kw):
        return documents.create_service_job(
            job_date=kw.pop("job_date", TODAY),
            customer_name=kw.pop("customer_name", "Mr Kulkarni"),
            description=kw.pop("description", "CCTV DVR not powering on"),
            amount_paise=kw.pop("amount_paise", 80_000),
            gst_rate_bp=kw.pop("gst_rate_bp", 1800),
            parts=kw.pop(
                "parts",
                [
                    documents.LineInput(
                        product_id=self.product,
                        qty=1,
                        unit_price_paise=120_000,
                        gst_rate_bp=1800,
                        hsn_code="8504",
                        description="SMPS 450W",
                    )
                ],
            ),
            **kw,
        )

    def quantity(self) -> int:
        return db.scalar("SELECT quantity FROM products WHERE id = ?", (self.product,))

    def test_parts_used_on_a_job_come_out_of_stock(self):
        self.job()
        self.assertEqual(self.quantity(), 7)
        self.assertStockLedgerAgrees(self.product)
        self.assertEqual(
            db.scalar(
                "SELECT ref_type FROM stock_movements WHERE product_id = ? "
                "ORDER BY id DESC LIMIT 1",
                (self.product,),
                default="",
            ),
            "service",
        )

    def test_a_job_with_no_parts_moves_no_stock(self):
        # Labour-only jobs are common — a site visit that fixes a loose cable.
        self.job(parts=[])
        self.assertEqual(self.quantity(), 8)
        self.assertStockLedgerAgrees(self.product)

    def test_cancelling_a_job_returns_its_parts(self):
        result = self.job()
        job_id = result["job_id"]
        self.assertEqual(self.quantity(), 7)

        documents.set_service_status(job_id, "cancelled")
        self.assertEqual(self.quantity(), 8)
        self.assertStockLedgerAgrees(self.product)

    def test_completing_a_job_does_not_move_stock_again(self):
        # The parts left the shelf when the job was created. Marking it done must not
        # deduct them a second time.
        result = self.job()
        job_id = result["job_id"]

        documents.set_service_status(job_id, "completed")
        self.assertEqual(self.quantity(), 7)
        self.assertStockLedgerAgrees(self.product)

    def test_a_job_cannot_use_parts_that_are_not_in_stock(self):
        with self.assertRaises(documents.DocumentError):
            self.job(
                parts=[
                    documents.LineInput(
                        product_id=self.product,
                        qty=99,
                        unit_price_paise=120_000,
                        gst_rate_bp=1800,
                        hsn_code="8504",
                        description="SMPS 450W",
                    )
                ]
            )
        self.assertEqual(self.quantity(), 8)
        self.assertStockLedgerAgrees(self.product)

    def test_an_unknown_status_is_refused(self):
        with self.assertRaises(documents.DocumentError):
            self.job(status="finished-ish")


class EndToEndLedgerTests(ShopTestCase):
    """One product through every path stock can take, checking the invariant each step."""

    def test_the_ledger_survives_a_full_working_day(self):
        dealer = self.make_dealer(name="Day Dealer")
        product = self.make_product(sku="DAY-1", name="Mouse", quantity=0)

        def check(expected: int, label: str) -> None:
            self.assertEqual(
                db.scalar("SELECT quantity FROM products WHERE id = ?", (product,)),
                expected,
                f"wrong quantity after {label}",
            )
            self.assertStockLedgerAgrees(product)

        line = documents.LineInput(
            product_id=product,
            qty=20,
            unit_price_paise=25_000,
            gst_rate_bp=1800,
            hsn_code="8471",
            description="Mouse",
        )
        purchase = documents.create_purchase(
            dealer_id=dealer,
            bill_number="DAY/1",
            bill_date=TODAY,
            lines=[line],
            taxes=documents.PurchaseTaxInput(taxable_paise=500_000, total_paise=590_000),
        )
        check(20, "purchase of 20")

        sale = documents.create_sale(
            invoice_date=TODAY,
            customer=documents.CustomerInput(name="Walk-in"),
            lines=[
                documents.LineInput(
                    product_id=product,
                    qty=3,
                    unit_price_paise=40_000,
                    gst_rate_bp=1800,
                    hsn_code="8471",
                    description="Mouse",
                )
            ],
        )
        check(17, "sale of 3")

        job = documents.create_service_job(
            job_date=TODAY,
            customer_name="Service customer",
            description="Replacement mouse fitted",
            amount_paise=0,
            parts=[
                documents.LineInput(
                    product_id=product,
                    qty=2,
                    unit_price_paise=40_000,
                    gst_rate_bp=1800,
                    hsn_code="8471",
                    description="Mouse",
                )
            ],
        )
        check(15, "service job using 2")

        repo.set_stock(product_id=product, new_quantity=14, note="One found broken")
        check(14, "stock-take to 14")

        documents.void_sale(sale["sale_id"], reason="Customer returned it")
        check(17, "voiding the sale")

        documents.set_service_status(job["job_id"], "cancelled")
        check(19, "cancelling the service job")

        # The stock-take consumed one of the twenty, so the batch can no longer be handed
        # back whole — and cancelling anyway would drive the shelf negative. Refusing is
        # the right answer: the paper trail has to be corrected the other way round.
        with self.assertRaises(documents.DocumentError) as caught:
            documents.void_purchase(
                purchase["purchase_id"], reason="Dealer recalled the batch"
            )
        self.assertIn("Mouse", str(caught.exception))
        check(19, "a refused purchase cancellation")

        # Six movements — purchase, sale, service, stock-take, sale void, service void —
        # and replaying them still equals the cache, which is the point of the exercise.
        self.assertEqual(
            db.scalar("SELECT COUNT(*) FROM stock_movements WHERE product_id = ?", (product,)),
            6,
        )
        self.assertEqual(
            [
                r["ref_type"]
                for r in db.query(
                    "SELECT ref_type FROM stock_movements WHERE product_id = ? ORDER BY id",
                    (product,),
                )
            ],
            ["purchase", "sale", "service", "adjustment", "sale_void", "service_void"],
        )


if __name__ == "__main__":
    unittest.main()
