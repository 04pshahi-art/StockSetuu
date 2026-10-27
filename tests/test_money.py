"""Integer money arithmetic — the foundation every tax figure sits on."""

from __future__ import annotations

import unittest
from decimal import Decimal

from app.money import (
    MoneyError,
    amount_in_words,
    fmt_money,
    fmt_rate,
    mul_div_round,
    paise,
    parse_qty,
    parse_rate_bp,
    rupees,
)


class ParsePaiseTests(unittest.TestCase):
    def test_accepts_what_a_shopkeeper_actually_types(self):
        self.assertEqual(paise("1,234.50"), 123450)
        self.assertEqual(paise("1234.5"), 123450)
        self.assertEqual(paise("₹1234"), 123400)
        self.assertEqual(paise(" 1234 "), 123400)
        self.assertEqual(paise(1234), 123400)
        self.assertEqual(paise(Decimal("1234.50")), 123450)

    def test_blank_is_zero_so_optional_fields_can_be_left_empty(self):
        self.assertEqual(paise(""), 0)
        self.assertEqual(paise("   "), 0)
        self.assertEqual(paise(None), 0)

    def test_rounds_half_away_from_zero(self):
        # Third decimal place is not representable in paise; a half rounds outward,
        # which is what a counter expects when it reads a printed dealer bill.
        self.assertEqual(paise("0.005"), 1)
        self.assertEqual(paise("0.004"), 0)
        self.assertEqual(paise("-0.005", allow_negative=True), -1)

    def test_negative_rejected_unless_allowed(self):
        with self.assertRaises(MoneyError):
            paise("-5")
        self.assertEqual(paise("-5", allow_negative=True), -500)

    def test_garbage_is_reported_with_the_field_name(self):
        with self.assertRaises(MoneyError) as caught:
            paise("abc", field="Unit price")
        self.assertIn("Unit price", str(caught.exception))

    def test_round_trip_through_rupees(self):
        self.assertEqual(rupees(123450), Decimal("1234.50"))
        self.assertEqual(rupees(-50), Decimal("-0.50"))


class FormattingTests(unittest.TestCase):
    def test_indian_digit_grouping(self):
        self.assertEqual(fmt_money(123456789), "12,34,567.89")
        self.assertEqual(fmt_money(100000), "1,000.00")
        self.assertEqual(fmt_money(999_00), "999.00")
        self.assertEqual(fmt_money(1234567800), "1,23,45,678.00")

    def test_edges(self):
        self.assertEqual(fmt_money(None), "0.00")
        self.assertEqual(fmt_money(0), "0.00")
        self.assertEqual(fmt_money(-50), "-0.50")
        self.assertEqual(fmt_money(150000, symbol=True), "₹1,500.00")

    def test_rate_labels(self):
        self.assertEqual(fmt_rate(1800), "18%")
        self.assertEqual(fmt_rate(250), "2.5%")
        self.assertEqual(fmt_rate(0), "0%")
        self.assertEqual(fmt_rate(None), "0%")
        self.assertEqual(fmt_rate(900), "9%")  # the CGST half of 18%
        self.assertEqual(fmt_rate(125), "1.25%")


class ParseRateTests(unittest.TestCase):
    def test_percent_to_basis_points(self):
        self.assertEqual(parse_rate_bp("18"), 1800)
        self.assertEqual(parse_rate_bp("18%"), 1800)
        self.assertEqual(parse_rate_bp("18.0"), 1800)
        self.assertEqual(parse_rate_bp("2.5"), 250)
        self.assertEqual(parse_rate_bp(""), 0)
        self.assertEqual(parse_rate_bp(None), 0)

    def test_out_of_range_rejected(self):
        for bad in ("-1", "101", "abc"):
            with self.subTest(bad=bad), self.assertRaises(MoneyError):
                parse_rate_bp(bad)


class ParseQtyTests(unittest.TestCase):
    def test_whole_units_only(self):
        self.assertEqual(parse_qty("3"), 3)
        self.assertEqual(parse_qty(" 3 "), 3)
        self.assertEqual(parse_qty("3.9"), 3)  # stock is pieces; fractions truncate

    def test_minimum_enforced(self):
        with self.assertRaises(MoneyError):
            parse_qty("0")
        self.assertEqual(parse_qty("0", minimum=0), 0)

    def test_required_and_non_numeric(self):
        with self.assertRaises(MoneyError):
            parse_qty("")
        with self.assertRaises(MoneyError):
            parse_qty("12 Nos")


class MulDivRoundTests(unittest.TestCase):
    """The single rounding primitive under every GST figure the app prints."""

    def test_exact_cases(self):
        self.assertEqual(mul_div_round(100_000, 1800, 10_000), 18_000)  # 18% of ₹1000
        self.assertEqual(mul_div_round(100_000, 1800, 20_000), 9_000)  # the CGST half
        self.assertEqual(mul_div_round(0, 1800, 10_000), 0)

    def test_half_rounds_up_not_to_even(self):
        self.assertEqual(mul_div_round(1, 1, 2), 1)  # 0.5 -> 1
        self.assertEqual(mul_div_round(3, 1, 2), 2)  # 1.5 -> 2, not 2-is-even luck
        self.assertEqual(mul_div_round(5, 1, 2), 3)  # 2.5 -> 3, banker's would give 2

    def test_below_and_above_half(self):
        self.assertEqual(mul_div_round(1, 1, 3), 0)  # 0.33
        self.assertEqual(mul_div_round(2, 1, 3), 1)  # 0.67

    def test_negative_amounts_round_away_from_zero(self):
        # Credit notes and round-off adjustments go negative; the magnitude must round
        # the same way as the positive case or a reversal will not cancel its original.
        self.assertEqual(mul_div_round(-1, 1, 2), -1)
        self.assertEqual(mul_div_round(-3, 1, 2), -2)
        self.assertEqual(mul_div_round(-100_000, 1800, 10_000), -18_000)

    def test_reversal_cancels_exactly(self):
        for amount in (1, 7, 99, 12_345, 999_999):
            with self.subTest(amount=amount):
                self.assertEqual(
                    mul_div_round(amount, 1800, 10_000) + mul_div_round(-amount, 1800, 10_000),
                    0,
                )

    def test_zero_denominator(self):
        with self.assertRaises(ZeroDivisionError):
            mul_div_round(100, 1, 0)


class AmountInWordsTests(unittest.TestCase):
    def test_invoice_examples(self):
        self.assertEqual(amount_in_words(0), "Rupees Zero Only")
        self.assertEqual(
            amount_in_words(182900), "Rupees One Thousand Eight Hundred Twenty Nine Only"
        )
        self.assertEqual(
            amount_in_words(13950), "Rupees One Hundred Thirty Nine and Fifty Paise Only"
        )

    def test_indian_grouping_in_words(self):
        self.assertEqual(
            amount_in_words(1234567800),
            "Rupees One Crore Twenty Three Lakh Forty Five Thousand "
            "Six Hundred Seventy Eight Only",
        )

    def test_paise_only_and_negative(self):
        self.assertEqual(amount_in_words(50), "Rupees Zero and Fifty Paise Only")
        self.assertEqual(amount_in_words(-50000), "Minus Rupees Five Hundred Only")


if __name__ == "__main__":
    unittest.main()
