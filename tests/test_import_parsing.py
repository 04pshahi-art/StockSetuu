"""Reading a Tally export and working out what it would do to the catalogue.

This is the feature most likely to quietly ruin the catalogue, because the input is not
under the shop's control. Tally's output changes between versions and configurations, the
owner may re-save it through Excel on the way, and every value arrives as free text: a
quantity is "12 Nos", a price is "1,234.50 Cr", a GST rate might be 18, 18% or 0.18. There
is no schema to validate against — only guesses, and guesses have to fail visibly.

Three things are pinned here because getting them wrong is expensive and silent:

**Header detection.** A Tally stock summary has a title block above the real header — the
shop's name, the report name, a date range. Pick the wrong row and every column is
mislabelled, the mapping guesses land on nothing, and the owner is asked to map "Column 3"
onto "Column 7". Worse, a sparse row in an .xlsx must not shift the columns left; a gap has
to come back as an empty cell, or values silently move into the wrong field.

**GST rate parsing.** A rate that is misread does not error — it prices the product wrong on
every future invoice. So a rate that cannot be understood must produce a *warning and the
default*, never a silent zero, and a rate outside the standard slabs must be flagged even
when it parses cleanly.

**Existing-SKU handling.** The import must know the difference between a product it is
creating and one it is touching, before anything is written. That decision is what the dry
run shows the owner, and it is what these tests exercise.

Scope, stated honestly: ``_analyse`` is the whole decision layer — it is what the preview
screen renders and what ``commit`` re-runs before writing, so create/update/skip and every
warning is covered below. The ``UPDATE products`` statement inside ``commit`` itself, and
the ``quantity_mode`` stock handling around it, are reachable only through an authenticated
POST, and this suite has no HTTP client. Those remain covered by the manual UI walkthrough,
not by this file.
"""

from __future__ import annotations

import io
import unittest
import zipfile
from xml.sax.saxutils import escape

from app import repo, tabular
from app.routers import tally_import as imp

from tests.support import ShopTestCase

SHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def make_xlsx(rows: list[list], *, inline: bool = False) -> bytes:
    """A minimal but genuinely valid-enough .xlsx for the reader under test.

    ``None`` in a row means "no cell at all at this position" — which is what Excel writes
    for an untouched cell, and the case that shifts columns if handled carelessly.
    Numbers are written as numeric cells so the reader sees what Excel really stores.
    """
    shared: list[str] = []
    xml_rows: list[str] = []
    for r, row in enumerate(rows, start=1):
        cells: list[str] = []
        for c, value in enumerate(row):
            if value is None:
                continue
            ref = f"{chr(65 + c)}{r}"
            if isinstance(value, (int, float)):
                cells.append(f'<c r="{ref}"><v>{value}</v></c>')
            elif inline:
                cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{escape(value)}</t></is></c>')
            else:
                if value not in shared:
                    shared.append(value)
                cells.append(f'<c r="{ref}" t="s"><v>{shared.index(value)}</v></c>')
        xml_rows.append(f'<row r="{r}">{"".join(cells)}</row>')

    sheet = (
        f'<?xml version="1.0"?><worksheet xmlns="{SHEET_NS}">'
        f'<sheetData>{"".join(xml_rows)}</sheetData></worksheet>'
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("xl/worksheets/sheet1.xml", sheet)
        if shared:
            items = "".join(f"<si><t>{escape(s)}</t></si>" for s in shared)
            archive.writestr(
                "xl/sharedStrings.xml",
                f'<?xml version="1.0"?><sst xmlns="{SHEET_NS}">{items}</sst>',
            )
    return buffer.getvalue()


class ReadCsvTests(unittest.TestCase):
    """Whatever separator and encoding the file arrived with, the rows must come out."""

    def test_a_plain_comma_file(self):
        rows = tabular.read_csv(b"sku,name,qty\nM-1,Mouse,10\nK-1,Keyboard,4\n")
        self.assertEqual(rows[0], ["sku", "name", "qty"])
        self.assertEqual(rows[1], ["M-1", "Mouse", "10"])
        self.assertEqual(len(rows), 3)

    def test_semicolons_tabs_and_pipes_are_all_recognised(self):
        # Excel on a machine with a comma decimal separator writes semicolons; Tally can be
        # told to write tabs. Guessing wrong turns every row into one long single column.
        for delimiter in (";", "\t", "|"):
            with self.subTest(delimiter=repr(delimiter)):
                text = delimiter.join(["sku", "name", "qty"]) + "\n"
                text += delimiter.join(["M-1", "Mouse", "10"]) + "\n"
                text += delimiter.join(["K-1", "Keyboard", "4"]) + "\n"
                rows = tabular.read_csv(text.encode())
                self.assertEqual(rows[0], ["sku", "name", "qty"])
                self.assertEqual(rows[1], ["M-1", "Mouse", "10"])

    def test_an_excel_utf8_bom_does_not_end_up_in_the_first_header(self):
        # This one is nasty: with the BOM left in, the first header is "﻿sku", the
        # auto-mapping fails to recognise it, and the owner cannot see why.
        rows = tabular.read_csv(b"\xef\xbb\xbfsku,name\nM-1,Mouse\n")
        self.assertEqual(rows[0], ["sku", "name"])
        self.assertNotIn("﻿", rows[0][0])

    def test_a_windows_cp1252_file_is_read_rather_than_rejected(self):
        # Tally on Windows writes cp1252. 0x96 is an en dash there and invalid UTF-8.
        raw = "sku,name\nM-1,Mouse – wireless\n".encode("cp1252")
        rows = tabular.read_csv(raw)
        self.assertEqual(rows[1], ["M-1", "Mouse – wireless"])

    def test_windows_line_endings(self):
        rows = tabular.read_csv(b"sku,name\r\nM-1,Mouse\r\n")
        self.assertEqual(rows, [["sku", "name"], ["M-1", "Mouse"]])

    def test_cells_are_stripped_of_padding(self):
        rows = tabular.read_csv(b"sku , name\n  M-1 ,  Mouse  \n")
        self.assertEqual(rows[0], ["sku", "name"])
        self.assertEqual(rows[1], ["M-1", "Mouse"])

    def test_a_quoted_field_may_contain_the_delimiter(self):
        rows = tabular.read_csv(b'sku,name\nM-1,"Mouse, wireless"\n')
        self.assertEqual(rows[1], ["M-1", "Mouse, wireless"])

    def test_a_single_column_file_does_not_crash_the_sniffer(self):
        # csv.Sniffer raises on files with no delimiter at all; the fallback must cope.
        rows = tabular.read_csv(b"Stock Item\nMouse\nKeyboard\n")
        self.assertEqual(rows[0], ["Stock Item"])
        self.assertEqual(rows[1], ["Mouse"])

    def test_an_empty_file_is_refused_with_a_readable_message(self):
        for raw in (b"", b"   \n  \n"):
            with self.subTest(raw=raw):
                with self.assertRaises(tabular.TableError) as caught:
                    tabular.read_csv(raw)
                self.assertIn("empty", str(caught.exception).lower())

    def test_absurdly_wide_rows_are_cut_to_the_column_limit(self):
        header = ",".join(f"c{i}" for i in range(70)).encode()
        rows = tabular.read_csv(header + b"\n")
        self.assertEqual(len(rows[0]), tabular.MAX_COLUMNS)


class ReadXlsxTests(unittest.TestCase):
    def test_shared_strings_and_numbers_both_come_back_as_text(self):
        raw = make_xlsx([["sku", "name", "qty"], ["M-1", "Mouse", 10], ["K-1", "Keyboard", 4.5]])
        rows = tabular.read_xlsx(raw)
        self.assertEqual(rows[0], ["sku", "name", "qty"])
        self.assertEqual(rows[1], ["M-1", "Mouse", "10"])
        self.assertEqual(rows[2], ["K-1", "Keyboard", "4.5"])

    def test_a_gap_in_a_row_stays_a_gap_instead_of_shifting_the_columns(self):
        # The column-shift bug: if the missing B cell collapsed, "8471" would be read as
        # the product name and the whole row would be wrong in a way nobody notices.
        raw = make_xlsx([["sku", "name", "hsn"], ["M-1", None, "8471"]])
        rows = tabular.read_xlsx(raw)
        self.assertEqual(rows[1], ["M-1", "", "8471"])

    def test_inline_strings_are_read_too(self):
        # Some exporters write inlineStr instead of the shared-string table.
        raw = make_xlsx([["sku", "name"], ["M-1", "Mouse"]], inline=True)
        self.assertEqual(tabular.read_xlsx(raw), [["sku", "name"], ["M-1", "Mouse"]])

    def test_a_shared_string_split_into_runs_is_joined(self):
        # Excel splits a cell into <r> runs when part of the text is styled differently.
        sheet = (
            f'<?xml version="1.0"?><worksheet xmlns="{SHEET_NS}"><sheetData>'
            '<row r="1"><c r="A1" t="s"><v>0</v></c></row>'
            "</sheetData></worksheet>"
        )
        sst = (
            f'<?xml version="1.0"?><sst xmlns="{SHEET_NS}">'
            "<si><r><t>Mouse </t></r><r><t>(wireless)</t></r></si></sst>"
        )
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("xl/worksheets/sheet1.xml", sheet)
            archive.writestr("xl/sharedStrings.xml", sst)
        self.assertEqual(tabular.read_xlsx(buffer.getvalue()), [["Mouse (wireless)"]])

    def test_something_that_is_not_a_zip_gets_the_old_xls_advice(self):
        with self.assertRaises(tabular.TableError) as caught:
            tabular.read_xlsx(b"\xd0\xcf\x11\xe0not a zip at all")
        message = str(caught.exception)
        self.assertIn(".xlsx", message)
        self.assertIn("re-save", message.lower())

    def test_a_zip_with_no_worksheet_is_refused(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("hello.txt", "not a workbook")
        with self.assertRaises(tabular.TableError) as caught:
            tabular.read_xlsx(buffer.getvalue())
        self.assertIn("worksheet", str(caught.exception).lower())


class ReadTableDispatchTests(unittest.TestCase):
    def test_an_xlsx_extension_goes_to_the_xlsx_reader(self):
        raw = make_xlsx([["sku", "name"], ["M-1", "Mouse"]])
        self.assertEqual(tabular.read_table("Stock Items.xlsx", raw), [["sku", "name"], ["M-1", "Mouse"]])

    def test_a_workbook_is_detected_by_its_magic_bytes_without_an_extension(self):
        raw = make_xlsx([["sku", "name"], ["M-1", "Mouse"]])
        self.assertEqual(tabular.read_table("export", raw), [["sku", "name"], ["M-1", "Mouse"]])

    def test_an_old_xls_is_refused_with_instructions_rather_than_a_parse_error(self):
        with self.assertRaises(tabular.TableError) as caught:
            tabular.read_table("Stock Items.xls", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
        self.assertIn("CSV", str(caught.exception))

    def test_anything_else_is_treated_as_csv(self):
        self.assertEqual(tabular.read_table("export.txt", b"sku,name\nM-1,Mouse\n")[1], ["M-1", "Mouse"])


class FindHeaderTests(unittest.TestCase):
    def test_the_tally_title_block_is_skipped(self):
        rows = [
            ["PRATYUSH COMPUTER SERVICES", "", "", ""],
            ["Stock Summary", "", "", ""],
            ["1-Apr-2026 to 30-Aug-2026", "", "", ""],
            ["Particulars", "Quantity", "Rate", "Value"],
            ["Wireless Mouse", "10 Nos", "250.00", "2,500.00"],
            ["USB Keyboard", "4 Nos", "450.00", "1,800.00"],
        ]
        self.assertEqual(tabular.find_header(rows), 3)

    def test_a_file_that_starts_at_the_header_returns_row_zero(self):
        rows = [["sku", "name", "qty"], ["M-1", "Mouse", "10"], ["K-1", "Keyboard", "4"]]
        self.assertEqual(tabular.find_header(rows), 0)

    def test_the_earliest_equally_good_row_wins(self):
        # Two candidate rows scoring the same must resolve to the first, because that is
        # the header and the second is data.
        rows = [["a", "b"], ["c", "d"], ["e", "f"]]
        self.assertEqual(tabular.find_header(rows), 0)

    def test_a_trailing_header_with_no_data_under_it_scores_lower_than_a_real_one(self):
        rows = [
            ["Particulars", "Quantity", "Rate"],
            ["Wireless Mouse", "10 Nos", "250.00"],
            ["Grand Total", "14", ""],
        ]
        self.assertEqual(tabular.find_header(rows), 0)


class NormaliseTests(unittest.TestCase):
    def test_headers_and_data_are_split_at_the_header_row(self):
        rows = [["junk", "", ""], ["sku", "name", "qty"], ["M-1", "Mouse", "10"]]
        headers, data = tabular.normalise(rows, 1)
        self.assertEqual(headers, ["sku", "name", "qty"])
        self.assertEqual(data, [["M-1", "Mouse", "10"]])

    def test_a_blank_header_cell_gets_a_positional_name(self):
        # The mapping screen needs something clickable for every column.
        headers, _ = tabular.normalise([["sku", "", "qty"], ["M-1", "x", "10"]], 0)
        self.assertEqual(headers, ["sku", "Column 2", "qty"])

    def test_header_whitespace_is_collapsed(self):
        headers, _ = tabular.normalise([["  Item   Code ", "Stock\tItem"], ["M-1", "Mouse"]], 0)
        self.assertEqual(headers, ["Item Code", "Stock Item"])

    def test_short_rows_are_padded_and_long_rows_trimmed_to_the_header_width(self):
        rows = [["sku", "name", "qty"], ["M-1"], ["K-1", "Keyboard", "4", "extra", "more"]]
        _, data = tabular.normalise(rows, 0)
        self.assertEqual(data[0], ["M-1", "", ""])
        self.assertEqual(data[1], ["K-1", "Keyboard", "4"])

    def test_completely_blank_rows_are_dropped(self):
        rows = [["sku", "name"], ["M-1", "Mouse"], ["", ""], ["   ", ""], ["K-1", "Keyboard"]]
        _, data = tabular.normalise(rows, 0)
        self.assertEqual(data, [["M-1", "Mouse"], ["K-1", "Keyboard"]])

    def test_a_header_index_past_the_end_is_clamped_rather_than_crashing(self):
        headers, data = tabular.normalise([["sku", "name"], ["M-1", "Mouse"]], 99)
        self.assertEqual(headers, ["M-1", "Mouse"])
        self.assertEqual(data, [])

    def test_no_rows_at_all_is_refused(self):
        with self.assertRaises(tabular.TableError):
            tabular.normalise([], 0)


class NumberParsingTests(unittest.TestCase):
    """Every numeric column in a Tally export is really a string with decoration."""

    def test_the_shapes_tally_actually_writes(self):
        cases = {
            "250": 250,
            "1,234.50": 1234.5,
            "12 Nos": 12,
            "12.000 PCS": 12,
            "₹450": 450,
            "Rs.450": 450,
            "  99  ": 99,
            "1,234.50 Dr": 1234.5,
            "(1,234.50)": -1234.5,
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                value = imp._number(raw)
                self.assertIsNotNone(value, f"{raw!r} was not understood at all")
                self.assertEqual(float(value), expected)

    def test_a_credit_suffix_is_stripped_without_flipping_the_sign(self):
        # Worth pinning explicitly: 'Cr' is dropped, not treated as a negative. For a stock
        # quantity or a rate that is right; it is only balances where Cr means the other way.
        self.assertEqual(float(imp._number("1,234.50 Cr")), 1234.5)

    def test_things_that_are_not_numbers_come_back_as_none(self):
        for raw in ("", "   ", "-", ".", "N/A", "abc", "Nos", "%"):
            with self.subTest(raw=raw):
                self.assertIsNone(imp._number(raw))

    def test_whole_numbers_round_half_up(self):
        self.assertEqual(imp._int_or_none("2.4"), 2)
        self.assertEqual(imp._int_or_none("2.5"), 3)
        self.assertEqual(imp._int_or_none("12.000 Nos"), 12)

    def test_a_negative_count_is_rejected_unless_it_is_allowed(self):
        self.assertIsNone(imp._int_or_none("(5)"))
        self.assertEqual(imp._int_or_none("(5)", allow_negative=True), -5)

    def test_a_blank_count_is_none_not_zero(self):
        # The difference matters: None means "the file said nothing", 0 means "the file
        # said none in stock". Only the second should ever zero out a quantity.
        self.assertIsNone(imp._int_or_none(""))


class RateParsingTests(unittest.TestCase):
    def test_the_three_ways_a_gst_rate_gets_written(self):
        for raw in ("18", "18%", "0.18", " 18 % "):
            with self.subTest(raw=raw):
                self.assertEqual(imp._rate_bp(raw), 1800)

    def test_other_slabs(self):
        self.assertEqual(imp._rate_bp("5"), 500)
        self.assertEqual(imp._rate_bp("12"), 1200)
        self.assertEqual(imp._rate_bp("28"), 2800)

    def test_zero_percent_is_zero_and_not_missing(self):
        # Exempt goods really are 0%, and that must survive as 0 rather than falling back
        # to the 18% default.
        self.assertEqual(imp._rate_bp("0"), 0)
        self.assertEqual(imp._rate_bp("0%"), 0)

    def test_a_fractional_rate_keeps_its_paise(self):
        self.assertEqual(imp._rate_bp("12.5"), 1250)

    def test_an_unreadable_rate_is_none_so_the_caller_can_warn(self):
        for raw in ("", "abc", "N/A", "-"):
            with self.subTest(raw=raw):
                self.assertIsNone(imp._rate_bp(raw))


class GuessMappingTests(unittest.TestCase):
    """Only pre-selects dropdowns, but a good guess is the difference between a
    two-click import and thirteen dropdowns."""

    def test_a_tally_stock_summary_header(self):
        headers = ["Particulars", "Item Code", "Stock Group", "Closing Balance", "Rate", "Value"]
        mapping = imp._guess_mapping(headers)
        self.assertEqual(headers[mapping["name"]], "Particulars")
        self.assertEqual(headers[mapping["sku"]], "Item Code")
        self.assertEqual(headers[mapping["category"]], "Stock Group")
        self.assertEqual(headers[mapping["quantity"]], "Closing Balance")

    def test_a_hand_written_spreadsheet_header(self):
        headers = ["SKU", "Name", "HSN", "GST Rate", "Purchase Rate", "MRP", "Qty", "Warranty"]
        mapping = imp._guess_mapping(headers)
        self.assertEqual(headers[mapping["sku"]], "SKU")
        self.assertEqual(headers[mapping["name"]], "Name")
        self.assertEqual(headers[mapping["hsn_code"]], "HSN")
        self.assertEqual(headers[mapping["gst_rate"]], "GST Rate")
        self.assertEqual(headers[mapping["cost_price"]], "Purchase Rate")
        self.assertEqual(headers[mapping["sale_price"]], "MRP")
        self.assertEqual(headers[mapping["quantity"]], "Qty")
        self.assertEqual(headers[mapping["warranty_months"]], "Warranty")

    def test_the_longer_keyword_wins_so_purchase_rate_is_not_just_rate(self):
        headers = ["Item Code", "Particulars", "Purchase Rate", "Sales Rate"]
        mapping = imp._guess_mapping(headers)
        self.assertEqual(headers[mapping["cost_price"]], "Purchase Rate")
        self.assertEqual(headers[mapping["sale_price"]], "Sales Rate")

    def test_no_column_is_guessed_onto_two_fields(self):
        headers = ["Code", "Description", "Rate", "Quantity", "HSN Code"]
        mapping = imp._guess_mapping(headers)
        self.assertEqual(len(set(mapping.values())), len(mapping))

    def test_an_unrecognised_header_is_left_unmapped_rather_than_guessed_wrong(self):
        mapping = imp._guess_mapping(["Column 1", "Column 2", "Column 3"])
        self.assertEqual(mapping, {})


class OptionTests(ShopTestCase):
    def test_sensible_defaults_from_an_empty_form(self):
        options = imp._read_options({})
        self.assertEqual(options["default_gst_rate_bp"], 1800)
        self.assertEqual(options["default_low_stock"], 2)
        self.assertEqual(options["on_existing"], "update")
        self.assertEqual(options["quantity_mode"], "new_only")

    def test_a_junk_gst_rate_falls_back_to_eighteen_percent(self):
        self.assertEqual(imp._read_options({"default_gst_rate": "banana"})["default_gst_rate_bp"], 1800)
        self.assertEqual(imp._read_options({"default_gst_rate": "12"})["default_gst_rate_bp"], 1200)

    def test_a_junk_low_stock_level_falls_back_and_never_goes_negative(self):
        self.assertEqual(imp._read_options({"default_low_stock": "nine"})["default_low_stock"], 2)
        self.assertEqual(imp._read_options({"default_low_stock": "-4"})["default_low_stock"], 0)
        self.assertEqual(imp._read_options({"default_low_stock": "7"})["default_low_stock"], 7)

    def test_an_unknown_mode_cannot_be_smuggled_in_through_the_form(self):
        # These come straight off a POST, so an unexpected value must not reach the commit
        # and be compared as an unknown string there.
        self.assertEqual(imp._read_options({"on_existing": "delete"})["on_existing"], "update")
        self.assertEqual(imp._read_options({"quantity_mode": "wipe"})["quantity_mode"], "new_only")
        self.assertEqual(imp._read_options({"quantity_mode": "set"})["quantity_mode"], "set")
        self.assertEqual(imp._read_options({"on_existing": "skip"})["on_existing"], "skip")

    def test_the_default_category_has_to_be_a_real_category(self):
        repo.ensure_category("Cameras")
        self.assertEqual(imp._read_options({"default_category": "Cameras"})["default_category"], "Cameras")
        chosen = imp._read_options({"default_category": "Nonsense"})["default_category"]
        self.assertIn(chosen, repo.list_categories() or ["Other"])


class AnalyseBase(ShopTestCase):
    HEADERS = [
        "Item Code", "Particulars", "Stock Group", "HSN", "GST Rate",
        "Purchase Rate", "Sale Rate", "Closing Balance",
    ]
    MAPPING = {
        "sku": 0, "name": 1, "category": 2, "hsn_code": 3, "gst_rate": 4,
        "cost_price": 5, "sale_price": 6, "quantity": 7,
    }

    def options(self, **kw) -> dict:
        base = {
            "default_category": "Accessories",
            "default_gst_rate_bp": 1800,
            "default_low_stock": 2,
            "on_existing": "update",
            "quantity_mode": "new_only",
        }
        base.update(kw)
        return base

    def analyse(self, rows: list[list[str]], *, mapping: dict | None = None, **option_kw) -> dict:
        job = {
            "filename": "Stock Items.csv",
            "headers": self.HEADERS,
            "rows": rows,
            "mapping": self.MAPPING if mapping is None else mapping,
        }
        return imp._analyse(job, self.options(**option_kw))

    def one(self, row: list[str], **option_kw) -> dict:
        analysis = self.analyse([row], **option_kw)
        self.assertEqual(analysis["total"], 1, "expected exactly one analysed row")
        return analysis["results"][0]


class AnalyseRowTests(AnalyseBase):
    def test_a_clean_row_becomes_a_create_with_every_value_converted(self):
        record = self.one(["M-1", "Wireless Mouse", "Accessories", "8471", "18", "250.00", "450.00", "10 Nos"])
        self.assertEqual(record["action"], "create")
        self.assertEqual(record["errors"], [])
        self.assertEqual(record["warnings"], [])
        values = record["values"]
        self.assertEqual(values["sku"], "M-1")
        self.assertEqual(values["name"], "Wireless Mouse")
        self.assertEqual(values["hsn_code"], "8471")
        self.assertEqual(values["gst_rate_bp"], 1800)
        self.assertEqual(values["cost_price_paise"], 25_000)
        self.assertEqual(values["sale_price_paise"], 45_000)
        self.assertEqual(values["quantity"], 10)
        self.assertEqual(values["low_stock_threshold"], 2)

    def test_a_row_with_no_sku_is_an_error_not_a_silent_create(self):
        record = self.one(["", "Wireless Mouse", "", "", "", "", "", ""])
        self.assertEqual(record["action"], "error")
        self.assertIn("No SKU", record["errors"])

    def test_a_row_with_no_name_is_an_error(self):
        record = self.one(["M-1", "", "", "", "", "", "", ""])
        self.assertEqual(record["action"], "error")
        self.assertIn("No product name", record["errors"])

    def test_a_completely_blank_row_is_not_counted_at_all(self):
        # Tally pads reports with filler rows; they are not errors to be reported, they
        # simply are not rows.
        analysis = self.analyse([
            ["M-1", "Wireless Mouse", "", "", "", "", "", ""],
            ["", "", "", "", "", "", "", ""],
            ["K-1", "USB Keyboard", "", "", "", "", "", ""],
        ])
        self.assertEqual(analysis["total"], 2)
        self.assertEqual(analysis["counts"]["error"], 0)

    def test_an_over_long_sku_is_refused(self):
        record = self.one(["X" * 65, "Wireless Mouse", "", "", "", "", "", ""])
        self.assertEqual(record["action"], "error")
        self.assertIn("64", " ".join(record["errors"]))

    def test_the_same_sku_twice_in_one_file_names_the_earlier_row(self):
        # Without this the second row would overwrite the first inside a single import and
        # the owner would never know which one won.
        analysis = self.analyse([
            ["M-1", "Wireless Mouse", "", "", "", "", "", ""],
            ["m-1", "Mouse (duplicate)", "", "", "", "", "", ""],
        ])
        self.assertEqual(analysis["results"][0]["action"], "create")
        self.assertEqual(analysis["results"][1]["action"], "error")
        self.assertIn("repeats row 1", " ".join(analysis["results"][1]["errors"]))

    def test_an_unreadable_gst_rate_warns_and_uses_the_default(self):
        record = self.one(["M-1", "Mouse", "", "", "eighteen", "", "", ""], default_gst_rate_bp=1200)
        self.assertEqual(record["values"]["gst_rate_bp"], 1200)
        self.assertTrue(any("not understood" in w for w in record["warnings"]))
        self.assertEqual(record["action"], "create", "a bad rate is a warning, not an error")

    def test_a_missing_gst_rate_takes_the_default_without_complaining(self):
        record = self.one(["M-1", "Mouse", "", "", "", "", "", ""], default_gst_rate_bp=500)
        self.assertEqual(record["values"]["gst_rate_bp"], 500)
        self.assertEqual(record["warnings"], [])

    def test_a_rate_outside_the_standard_slabs_is_flagged_but_kept(self):
        # 17% parses perfectly and is still almost certainly a typo, so it is surfaced
        # rather than corrected.
        record = self.one(["M-1", "Mouse", "", "", "17", "", "", ""])
        self.assertEqual(record["values"]["gst_rate_bp"], 1700)
        self.assertTrue(any("slab" in w for w in record["warnings"]))

    def test_zero_percent_survives_instead_of_becoming_the_default(self):
        record = self.one(["M-1", "Mouse", "", "", "0", "", "", ""], default_gst_rate_bp=1800)
        self.assertEqual(record["values"]["gst_rate_bp"], 0)

    def test_an_hsn_code_is_reduced_to_digits(self):
        self.assertEqual(self.one(["M-1", "Mouse", "", "8471.60.29", "", "", "", ""])["values"]["hsn_code"], "84716029")
        self.assertEqual(self.one(["M-1", "Mouse", "", "HSN 8525", "", "", "", ""])["values"]["hsn_code"], "8525")
        self.assertEqual(self.one(["M-1", "Mouse", "", "n/a", "", "", "", ""])["values"]["hsn_code"], "")

    def test_a_negative_price_warns_and_becomes_zero(self):
        record = self.one(["M-1", "Mouse", "", "", "", "(250.00)", "", ""])
        self.assertEqual(record["values"]["cost_price_paise"], 0)
        self.assertTrue(any("cost price" in w for w in record["warnings"]))

    def test_an_unreadable_quantity_warns_and_is_treated_as_zero(self):
        record = self.one(["M-1", "Mouse", "", "", "", "", "", "many"])
        self.assertEqual(record["values"]["quantity"], 0)
        self.assertTrue(any("Quantity" in w for w in record["warnings"]))

    def test_an_unmapped_quantity_column_stays_none_rather_than_zero(self):
        mapping = {k: v for k, v in self.MAPPING.items() if k != "quantity"}
        record = self.one(["M-1", "Mouse", "", "", "", "", "", "10"], mapping=mapping)
        self.assertIsNone(record["values"]["quantity"])

    def test_the_unit_falls_back_to_nos(self):
        self.assertEqual(self.one(["M-1", "Mouse", "", "", "", "", "", ""])["values"]["unit"], "Nos")

    def test_a_category_matching_an_existing_one_takes_its_existing_spelling(self):
        # Otherwise "cameras" and "Cameras" become two categories in the products list.
        repo.ensure_category("CCTV Cameras")
        record = self.one(["C-1", "Dome Camera", "cctv cameras", "", "", "", "", ""])
        self.assertEqual(record["values"]["category"], "CCTV Cameras")

    def test_a_blank_category_takes_the_default(self):
        record = self.one(["M-1", "Mouse", "", "", "", "", "", ""], default_category="Accessories")
        self.assertEqual(record["values"]["category"], "Accessories")

    def test_the_low_stock_default_applies_only_when_the_column_says_nothing(self):
        # "Reorder" is appended, so it is column index 8 — one past the eight standard ones.
        mapping = dict(self.MAPPING, low_stock_threshold=8)
        headers = self.HEADERS + ["Reorder"]
        job = {"filename": "f.csv", "headers": headers, "rows": [
            ["M-1", "Mouse", "", "", "", "", "", "", "5"],
            ["K-1", "Keyboard", "", "", "", "", "", "", ""],
        ], "mapping": mapping}
        results = imp._analyse(job, self.options(default_low_stock=3))["results"]
        self.assertEqual(results[0]["values"]["low_stock_threshold"], 5)
        self.assertEqual(results[1]["values"]["low_stock_threshold"], 3)

    def test_the_mapped_columns_are_reported_back_by_their_real_names(self):
        # This is what the preview shows the owner to confirm the mapping before committing.
        analysis = self.analyse([["M-1", "Mouse", "", "", "", "", "", ""]])
        self.assertEqual(analysis["mapped"]["sku"], "Item Code")
        self.assertEqual(analysis["mapped"]["quantity"], "Closing Balance")


class ExistingSkuTests(AnalyseBase):
    def setUp(self) -> None:
        super().setUp()
        self.existing = self.make_product(sku="M-1", name="Mouse (old name)", quantity=7)

    def test_a_known_sku_becomes_an_update_and_carries_the_product_id(self):
        record = self.one(["M-1", "Wireless Mouse", "", "", "", "", "", ""])
        self.assertEqual(record["action"], "update")
        self.assertEqual(record["product_id"], self.existing)
        self.assertEqual(record["existing_name"], "Mouse (old name)")
        self.assertEqual(record["existing_qty"], 7)

    def test_the_sku_match_ignores_case_and_padding(self):
        # "m-1" and "M-1 " are the same product to anyone reading the file, so they must be
        # the same product to the import — otherwise it creates a duplicate SKU.
        for written in ("m-1", " M-1 ", "M-1"):
            with self.subTest(written=written):
                self.assertEqual(self.one([written, "Wireless Mouse", "", "", "", "", "", ""])["action"], "update")

    def test_on_existing_skip_leaves_the_product_alone(self):
        record = self.one(["M-1", "Wireless Mouse", "", "", "", "", "", ""], on_existing="skip")
        self.assertEqual(record["action"], "skip")
        self.assertEqual(record["product_id"], self.existing)

    def test_an_unknown_sku_is_still_a_create_alongside_a_known_one(self):
        analysis = self.analyse([
            ["M-1", "Wireless Mouse", "", "", "", "", "", ""],
            ["K-1", "USB Keyboard", "", "", "", "", "", ""],
        ])
        self.assertEqual([r["action"] for r in analysis["results"]], ["update", "create"])
        self.assertEqual(analysis["counts"], {"create": 1, "update": 1, "skip": 0, "error": 0})

    def test_analysing_twice_does_not_write_anything(self):
        # The dry run must be genuinely dry: the owner may go back and forth between the
        # mapping and preview screens several times before committing.
        before = self.product_count()
        self.analyse([["K-1", "USB Keyboard", "", "", "", "", "", ""]])
        self.analyse([["K-1", "USB Keyboard", "", "", "", "", "", ""]])
        self.assertEqual(self.product_count(), before)

    def product_count(self) -> int:
        from app import db

        return db.scalar("SELECT COUNT(*) FROM products")


class TallyRoundTripTests(ShopTestCase):
    """The whole chain on a file shaped the way Tally actually exports one."""

    RAW = (
        "PRATYUSH COMPUTER SERVICES\n"
        "Stock Summary\n"
        "1-Apr-2026 to 30-Aug-2026\n"
        "Item Code,Particulars,Stock Group,HSN,GST Rate,Purchase Rate,Sale Rate,Closing Balance\n"
        "M-1,Wireless Mouse,Accessories,8471.60.29,18%,\"250.00\",\"450.00\",10 Nos\n"
        "K-1,USB Keyboard,Accessories,8471,18,\"1,250.00\",\"1,800.00\",4 Nos\n"
        ",,,,,,,\n"
        "C-1,CCTV Dome Camera 2MP,CCTV Cameras,8525,18,\"2,250.00\",\"3,500.00\",6 Nos\n"
        "B-1,Blank Row Without Name,,,,,,\n"
        "Grand Total,,,,,,,20 Nos\n"
    ).encode()

    def test_a_tally_export_is_read_mapped_and_analysed_end_to_end(self):
        rows = tabular.read_table("Stock Items.csv", self.RAW)
        header_index = tabular.find_header(rows)
        self.assertEqual(header_index, 3, "the title block should have been skipped")

        headers, data = tabular.normalise(rows, header_index)
        self.assertEqual(headers[0], "Item Code")
        self.assertEqual(len(data), 5, "the blank filler row should be gone already")

        mapping = imp._guess_mapping(headers)
        for key in ("sku", "name", "category", "hsn_code", "gst_rate", "cost_price", "sale_price", "quantity"):
            self.assertIn(key, mapping, f"{key} was not guessed from a normal Tally header")
        self.assertEqual(headers[mapping["cost_price"]], "Purchase Rate")
        self.assertEqual(headers[mapping["sale_price"]], "Sale Rate")

        job = {"filename": "Stock Items.csv", "headers": headers, "rows": data, "mapping": mapping}
        options = {
            "default_category": "Accessories",
            "default_gst_rate_bp": 1800,
            "default_low_stock": 2,
            "on_existing": "update",
            "quantity_mode": "new_only",
        }
        analysis = imp._analyse(job, options)

        by_sku = {r["values"]["sku"]: r for r in analysis["results"] if r["values"].get("sku")}
        mouse = by_sku["M-1"]
        self.assertEqual(mouse["action"], "create")
        self.assertEqual(mouse["values"]["gst_rate_bp"], 1800)
        self.assertEqual(mouse["values"]["hsn_code"], "84716029")
        self.assertEqual(mouse["values"]["cost_price_paise"], 25_000)
        self.assertEqual(mouse["values"]["quantity"], 10)

        keyboard = by_sku["K-1"]
        self.assertEqual(keyboard["values"]["cost_price_paise"], 125_000)
        self.assertEqual(keyboard["values"]["sale_price_paise"], 180_000)
        self.assertEqual(keyboard["values"]["quantity"], 4)

        camera = by_sku["C-1"]
        self.assertEqual(camera["values"]["category"], "CCTV Cameras")
        self.assertEqual(camera["values"]["quantity"], 6)

        # "Grand Total" has a name but no SKU, and the row with a name but nothing else is
        # a create with no prices — both must be visible in the counts, not swallowed.
        self.assertEqual(analysis["counts"]["error"], 1)
        self.assertEqual(analysis["counts"]["create"], 4)
        self.assertEqual(analysis["total"], 5)


if __name__ == "__main__":
    unittest.main()
