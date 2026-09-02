"""GST rules: GSTIN validity, place of supply, and the per-line tax split.

These are the numbers that go on a tax invoice and into GSTR-1, so the cases here are
written from the rules rather than from the code: a wrong figure here is a wrong return.
"""

from __future__ import annotations

import datetime as dt
import unittest

from app.gst import (
    HOME_STATE_CODE,
    compute_line,
    financial_year,
    fy_bounds,
    fy_label,
    gstin_check_digit,
    hsn_summary,
    is_interstate,
    normalise_gstin,
    resolve_state_code,
    state_code_from_gstin,
    state_label,
    total_lines,
    validate_gstin,
)

# Structurally valid GSTINs (checksums verified against the mod-36 algorithm).
MH_GSTIN = "27BXZPS5663N1Z2"  # Maharashtra — the shop's own state
KA_GSTIN = "29AAGCB1286Q1Z0"  # Karnataka — the inter-state case
DL_GSTIN = "07AAACT2727Q1ZY"


class GstinTests(unittest.TestCase):
    def test_known_good_gstins_accepted(self):
        for gstin in (MH_GSTIN, KA_GSTIN, DL_GSTIN):
            with self.subTest(gstin=gstin):
                self.assertEqual(validate_gstin(gstin), (True, ""))

    def test_blank_is_valid_because_gstin_is_optional(self):
        # Most walk-in customers are unregistered; the field has to accept nothing.
        self.assertEqual(validate_gstin(""), (True, ""))
        self.assertEqual(validate_gstin(None), (True, ""))
        self.assertEqual(validate_gstin("   "), (True, ""))

    def test_normalisation_before_validation(self):
        self.assertEqual(normalise_gstin(" 27bxzps5663n1z2 "), MH_GSTIN)
        self.assertTrue(validate_gstin("27bxzps5663n1z2")[0])
        self.assertTrue(validate_gstin("27 BXZPS 5663 N1Z2")[0])

    def test_single_character_typo_is_caught_by_the_checksum(self):
        # The whole point of the 15th digit: a mistyped GSTIN on an invoice is a real
        # problem for the buyer's input credit, so it must not pass silently.
        ok, message = validate_gstin("27BXZPS5663N1Z3")
        self.assertFalse(ok)
        self.assertIn("checksum", message)

    def test_wrong_length(self):
        ok, message = validate_gstin("27BXZPS5663N1Z")
        self.assertFalse(ok)
        self.assertIn("15", message)

    def test_wrong_shape(self):
        ok, message = validate_gstin("271XZPS5663N1Z2")
        self.assertFalse(ok)
        self.assertIn("format", message)

    def test_impossible_state_code(self):
        ok, message = validate_gstin("99BXZPS5663N1Z2")
        self.assertFalse(ok)
        self.assertIn("state code", message)

    def test_check_digit_is_self_consistent(self):
        for base in ("27BXZPS5663N1Z", "29AAGCB1286Q1Z", "07AAACT2727Q1Z"):
            with self.subTest(base=base):
                self.assertTrue(validate_gstin(base + gstin_check_digit(base))[0])


class PlaceOfSupplyTests(unittest.TestCase):
    def test_state_extracted_from_gstin(self):
        self.assertEqual(state_code_from_gstin(KA_GSTIN), "29")
        self.assertEqual(state_code_from_gstin(""), "")
        self.assertEqual(state_code_from_gstin("99AAGCB1286Q1Z0"), "")

    def test_explicit_state_beats_the_gstin_prefix(self):
        # A buyer can be registered in one state and take delivery in another; whoever
        # is at the counter chose the place of supply deliberately, so it wins.
        self.assertEqual(resolve_state_code("33", KA_GSTIN, "27"), "33")

    def test_falls_back_to_gstin_then_to_the_shop(self):
        self.assertEqual(resolve_state_code("", KA_GSTIN, "27"), "29")
        self.assertEqual(resolve_state_code("", "", "27"), "27")
        self.assertEqual(resolve_state_code("99", "", "27"), "27")

    def test_local_walk_in_customer_is_never_interstate(self):
        # A cash sale with no name and no state is a local sale. Guessing IGST here
        # would put the tax under the wrong head on every counter sale of the day.
        self.assertFalse(is_interstate("27", ""))
        self.assertFalse(is_interstate("27", None))
        self.assertFalse(is_interstate("27", "   "))

    def test_same_state_is_cgst_sgst_other_state_is_igst(self):
        self.assertFalse(is_interstate("27", "27"))
        self.assertTrue(is_interstate("27", "29"))

    def test_shop_state_defaults_to_home_when_unset(self):
        self.assertEqual(HOME_STATE_CODE, "27")
        self.assertFalse(is_interstate("", "27"))
        self.assertTrue(is_interstate("", "29"))

    def test_state_label(self):
        self.assertEqual(state_label("27"), "27 — Maharashtra")
        self.assertEqual(state_label("29"), "29 — Karnataka")
        self.assertEqual(state_label(""), "—")


class ComputeLineTests(unittest.TestCase):
    def test_intra_state_splits_into_equal_halves(self):
        # ₹1,550 at 18% -> CGST ₹139.50 + SGST ₹139.50 -> ₹1,829.00.
        line = compute_line(
            qty=1, unit_price_paise=155_000, rate_bp=1800, interstate=False
        )
        self.assertEqual(line.taxable_paise, 155_000)
        self.assertEqual(line.cgst_paise, 13_950)
        self.assertEqual(line.sgst_paise, 13_950)
        self.assertEqual(line.igst_paise, 0)
        self.assertEqual(line.total_paise, 182_900)

    def test_inter_state_charges_the_full_rate_as_igst(self):
        line = compute_line(qty=1, unit_price_paise=155_000, rate_bp=1800, interstate=True)
        self.assertEqual(line.igst_paise, 27_900)
        self.assertEqual(line.cgst_paise, 0)
        self.assertEqual(line.sgst_paise, 0)
        self.assertEqual(line.total_paise, 182_900)

    def test_halves_are_exactly_equal_for_every_slab_and_odd_amount(self):
        # A tax invoice showing CGST ₹4.51 against SGST ₹4.50 is malformed. Because both
        # halves come from the same half-rate computation they cannot diverge.
        for rate_bp in (250, 500, 1200, 1800, 2800):
            for amount in (1, 3, 7, 99, 333, 12_345, 99_999):
                with self.subTest(rate_bp=rate_bp, amount=amount):
                    line = compute_line(
                        qty=1, unit_price_paise=amount, rate_bp=rate_bp, interstate=False
                    )
                    self.assertEqual(line.cgst_paise, line.sgst_paise)

    def test_zero_rated_line_carries_no_tax(self):
        for interstate in (False, True):
            with self.subTest(interstate=interstate):
                line = compute_line(
                    qty=2, unit_price_paise=50_000, rate_bp=0, interstate=interstate
                )
                self.assertEqual(line.taxable_paise, 100_000)
                self.assertEqual(line.tax_paise, 0)
                self.assertEqual(line.total_paise, 100_000)

    def test_quantity_multiplies_the_unit_price(self):
        line = compute_line(qty=3, unit_price_paise=45_000, rate_bp=1200, interstate=False)
        self.assertEqual(line.taxable_paise, 135_000)
        self.assertEqual(line.cgst_paise, 8_100)  # 6% of ₹1350
        self.assertEqual(line.sgst_paise, 8_100)

    def test_discount_comes_off_before_tax(self):
        line = compute_line(
            qty=2,
            unit_price_paise=100_000,
            rate_bp=1800,
            interstate=False,
            discount_paise=50_000,
        )
        self.assertEqual(line.taxable_paise, 150_000)
        self.assertEqual(line.cgst_paise, 13_500)

    def test_negative_discount_is_ignored_not_added(self):
        plain = compute_line(qty=1, unit_price_paise=100_000, rate_bp=1800, interstate=False)
        odd = compute_line(
            qty=1,
            unit_price_paise=100_000,
            rate_bp=1800,
            interstate=False,
            discount_paise=-5_000,
        )
        self.assertEqual(odd.taxable_paise, plain.taxable_paise)

    def test_gst_inclusive_price_is_back_calculated_to_the_entered_gross(self):
        # The counter quotes "₹1,180 all-in". The invoice must show ₹1,000 + ₹180 tax
        # and still total exactly ₹1,180 — the customer was quoted that figure.
        line = compute_line(
            qty=1,
            unit_price_paise=118_000,
            rate_bp=1800,
            interstate=False,
            price_includes_gst=True,
        )
        self.assertEqual(line.taxable_paise, 100_000)
        self.assertEqual(line.cgst_paise, 9_000)
        self.assertEqual(line.sgst_paise, 9_000)
        self.assertEqual(line.total_paise, 118_000)

    def test_inclusive_flag_is_inert_at_zero_rate(self):
        line = compute_line(
            qty=1,
            unit_price_paise=100_000,
            rate_bp=0,
            interstate=False,
            price_includes_gst=True,
        )
        self.assertEqual(line.taxable_paise, 100_000)

    def test_rejects_nonsense_lines(self):
        with self.assertRaises(ValueError):
            compute_line(qty=0, unit_price_paise=100, rate_bp=1800, interstate=False)
        with self.assertRaises(ValueError):
            compute_line(qty=-1, unit_price_paise=100, rate_bp=1800, interstate=False)
        with self.assertRaises(ValueError):
            compute_line(qty=1, unit_price_paise=100, rate_bp=-100, interstate=False)
        with self.assertRaises(ValueError):
            compute_line(
                qty=1,
                unit_price_paise=100,
                rate_bp=1800,
                interstate=False,
                discount_paise=500,
            )

    def test_hsn_code_is_trimmed_and_kept(self):
        line = compute_line(
            qty=1,
            unit_price_paise=100,
            rate_bp=1800,
            interstate=False,
            hsn_code="  8473  ",
        )
        self.assertEqual(line.hsn_code, "8473")


class TotalsTests(unittest.TestCase):
    def _lines(self):
        return [
            compute_line(qty=1, unit_price_paise=155_000, rate_bp=1800, interstate=False),
            compute_line(qty=2, unit_price_paise=45_000, rate_bp=1200, interstate=False),
        ]

    def test_totals_add_up_head_by_head(self):
        totals = total_lines(self._lines())
        self.assertEqual(totals.taxable_paise, 155_000 + 90_000)
        self.assertEqual(totals.cgst_paise, 13_950 + 5_400)
        self.assertEqual(totals.sgst_paise, totals.cgst_paise)
        self.assertEqual(totals.igst_paise, 0)
        self.assertEqual(totals.tax_paise, totals.cgst_paise + totals.sgst_paise)
        self.assertEqual(totals.subtotal_paise, 245_000 + 38_700)

    def test_no_round_off_unless_asked(self):
        totals = total_lines(self._lines())
        self.assertEqual(totals.round_off_paise, 0)
        self.assertEqual(totals.grand_total_paise, totals.subtotal_paise)

    def test_round_off_to_the_nearest_rupee(self):
        def grand(unit_price_paise):
            line = compute_line(
                qty=1, unit_price_paise=unit_price_paise, rate_bp=0, interstate=False
            )
            return total_lines([line], round_to_rupee=True)

        below = grand(10_037)  # 37 paise over — comes down
        self.assertEqual(below.round_off_paise, -37)
        self.assertEqual(below.grand_total_paise, 10_000)

        above = grand(10_077)  # 77 paise over — goes up
        self.assertEqual(above.round_off_paise, 23)
        self.assertEqual(above.grand_total_paise, 10_100)

        half = grand(10_050)  # exactly 50 paise — rounds up
        self.assertEqual(half.grand_total_paise, 10_100)

        exact = grand(10_000)  # already whole rupees — untouched
        self.assertEqual(exact.round_off_paise, 0)
        self.assertEqual(exact.grand_total_paise, 10_000)

    def test_round_off_never_moves_more_than_fifty_paise(self):
        for extra in range(0, 100):
            with self.subTest(extra=extra):
                line = compute_line(
                    qty=1, unit_price_paise=100_000 + extra, rate_bp=0, interstate=False
                )
                totals = total_lines([line], round_to_rupee=True)
                self.assertLessEqual(abs(totals.round_off_paise), 50)
                self.assertEqual(totals.grand_total_paise % 100, 0)

    def test_empty_invoice_totals_to_zero(self):
        totals = total_lines([], round_to_rupee=True)
        self.assertEqual(totals.grand_total_paise, 0)


class HsnSummaryTests(unittest.TestCase):
    def test_same_hsn_and_rate_merge(self):
        lines = [
            compute_line(
                qty=1, unit_price_paise=100_000, rate_bp=1800, interstate=False,
                hsn_code="8473",
            ),
            compute_line(
                qty=2, unit_price_paise=50_000, rate_bp=1800, interstate=False,
                hsn_code="8473",
            ),
        ]
        rows = hsn_summary(lines)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["qty"], 3)
        self.assertEqual(rows[0]["taxable_paise"], 200_000)
        self.assertEqual(rows[0]["cgst_paise"], 18_000)

    def test_same_hsn_different_rate_stays_separate(self):
        # GSTR-1's HSN table is keyed by HSN *and* rate; merging them would misreport.
        lines = [
            compute_line(
                qty=1, unit_price_paise=100_000, rate_bp=1800, interstate=False,
                hsn_code="8473",
            ),
            compute_line(
                qty=1, unit_price_paise=100_000, rate_bp=1200, interstate=False,
                hsn_code="8473",
            ),
        ]
        rows = hsn_summary(lines)
        self.assertEqual(len(rows), 2)
        self.assertEqual([r["rate_bp"] for r in rows], [1200, 1800])

    def test_missing_hsn_is_bucketed_not_dropped(self):
        lines = [compute_line(qty=1, unit_price_paise=100, rate_bp=1800, interstate=False)]
        rows = hsn_summary(lines)
        self.assertEqual(rows[0]["hsn_code"], "—")

    def test_summary_reconciles_with_the_invoice_totals(self):
        lines = [
            compute_line(
                qty=1, unit_price_paise=155_000, rate_bp=1800, interstate=True,
                hsn_code="8473",
            ),
            compute_line(
                qty=3, unit_price_paise=20_000, rate_bp=500, interstate=True,
                hsn_code="8544",
            ),
        ]
        totals = total_lines(lines)
        rows = hsn_summary(lines)
        self.assertEqual(sum(int(r["taxable_paise"]) for r in rows), totals.taxable_paise)
        self.assertEqual(sum(int(r["igst_paise"]) for r in rows), totals.igst_paise)


class FinancialYearTests(unittest.TestCase):
    def test_indian_fy_starts_in_april(self):
        self.assertEqual(financial_year(dt.date(2025, 8, 28)), (2025, 2026))
        self.assertEqual(financial_year(dt.date(2025, 4, 1)), (2025, 2026))
        self.assertEqual(financial_year(dt.date(2025, 3, 31)), (2024, 2025))

    def test_labels_used_in_the_invoice_series(self):
        self.assertEqual(fy_label(dt.date(2025, 8, 28)), "2025-26")
        self.assertEqual(fy_label(dt.date(2026, 3, 31)), "2025-26")
        self.assertEqual(fy_label(dt.date(2026, 4, 1)), "2026-27")

    def test_bounds_round_trip_with_the_label(self):
        start, end = fy_bounds("2025-26")
        self.assertEqual(start, dt.date(2025, 4, 1))
        self.assertEqual(end, dt.date(2026, 3, 31))
        self.assertEqual(fy_label(start), "2025-26")
        self.assertEqual(fy_label(end), "2025-26")


if __name__ == "__main__":
    unittest.main()
