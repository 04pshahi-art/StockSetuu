"""Reading tabular files (CSV / XLSX) with nothing but the standard library.

Tally exports vary between versions and configurations, so nothing here assumes any
particular column layout. The job of this module is only to hand back "rows of strings,
plus whatever the header row said" — deciding what those columns *mean* is the user's,
via the mapping screen.
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from xml.etree import ElementTree as ET


class TableError(Exception):
    """The file could not be read as a table."""


MAX_ROWS = 20_000
MAX_COLUMNS = 60

# Encodings worth trying, in order. Tally on Windows commonly writes cp1252, and Excel
# "CSV UTF-8" writes a BOM.
_ENCODINGS = ("utf-8-sig", "utf-8", "cp1252", "latin-1")


def _decode(raw: bytes) -> str:
    for encoding in _ENCODINGS:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    # latin-1 never fails, so this is unreachable in practice; kept for clarity.
    return raw.decode("latin-1", errors="replace")


def read_csv(raw: bytes) -> list[list[str]]:
    text = _decode(raw).replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        raise TableError("That file is empty.")
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        delimiter = dialect.delimiter
    except csv.Error:
        # Sniffer gives up on single-column files and some quoted exports; count instead.
        first = sample.splitlines()[0] if sample.splitlines() else ""
        delimiter = max(",;\t|", key=first.count) if any(c in first for c in ",;\t|") else ","
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows: list[list[str]] = []
    for row in reader:
        rows.append([(cell or "").strip() for cell in row[:MAX_COLUMNS]])
        if len(rows) >= MAX_ROWS:
            break
    return rows


_CELL_REF = re.compile(r"^([A-Z]+)")


def _column_index(ref: str) -> int:
    match = _CELL_REF.match(ref or "")
    if not match:
        return 0
    index = 0
    for char in match.group(1):
        index = index * 26 + (ord(char) - 64)
    return index - 1


def _tag(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _cell_text(element: ET.Element, shared: list[str]) -> str:
    kind = element.get("t", "n")
    if kind == "s":  # shared string, <v> holds the index
        node = next((c for c in element if _tag(c) == "v"), None)
        if node is None or not (node.text or "").isdigit():
            return ""
        position = int(node.text)
        return shared[position] if 0 <= position < len(shared) else ""
    if kind == "inlineStr":
        return "".join(t.text or "" for t in element.iter() if _tag(t) == "t").strip()
    # Numbers, dates (stored as serial numbers), booleans and cached formula results.
    node = next((c for c in element if _tag(c) == "v"), None)
    return (node.text or "").strip() if node is not None else ""


def read_xlsx(raw: bytes) -> list[list[str]]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise TableError(
            "That does not look like an .xlsx file. If Tally produced an old .xls, "
            "open it in Excel and re-save as CSV or .xlsx."
        ) from exc

    shared: list[str] = []
    if "xl/sharedStrings.xml" in archive.namelist():
        root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
        for item in root:
            # A shared string can be split across several <r><t> runs.
            shared.append("".join(t.text or "" for t in item.iter() if _tag(t) == "t"))

    sheets = sorted(n for n in archive.namelist() if n.startswith("xl/worksheets/sheet"))
    if not sheets:
        raise TableError("That workbook has no worksheets.")

    root = ET.fromstring(archive.read(sheets[0]))
    rows: list[list[str]] = []
    for row_element in (e for e in root.iter() if _tag(e) == "row"):
        cells: dict[int, str] = {}
        for cell in (c for c in row_element if _tag(c) == "c"):
            index = _column_index(cell.get("r", ""))
            if index < MAX_COLUMNS:
                cells[index] = _cell_text(cell, shared).strip()
        width = (max(cells) + 1) if cells else 0
        rows.append([cells.get(i, "") for i in range(width)])
        if len(rows) >= MAX_ROWS:
            break
    return rows


def read_table(filename: str, raw: bytes) -> list[list[str]]:
    lowered = (filename or "").lower()
    if lowered.endswith(".xlsx") or raw[:2] == b"PK":
        return read_xlsx(raw)
    if lowered.endswith(".xls"):
        raise TableError(
            "Old .xls files are not supported. In Tally or Excel, save the export as "
            "CSV or .xlsx and upload that."
        )
    return read_csv(raw)


def find_header(rows: list[list[str]]) -> int:
    """Guess which row is the header.

    Tally stock summaries carry a title block ("YOUR SHOP NAME", "Stock
    Summary", a date range) above the real header. The header is the first row with
    several non-empty cells where the row below it also has several — a title line is
    typically one wide merged cell.
    """
    best_index, best_score = 0, -1
    for index, row in enumerate(rows[:25]):
        filled = sum(1 for cell in row if cell.strip())
        if filled < 2:
            continue
        following = rows[index + 1] if index + 1 < len(rows) else []
        follow_filled = sum(1 for cell in following if cell.strip())
        score = filled + min(follow_filled, filled)
        # Prefer earlier rows on a tie: the first plausible header is usually the header.
        if score > best_score:
            best_index, best_score = index, score
    return best_index


def normalise(rows: list[list[str]], header_index: int) -> tuple[list[str], list[list[str]]]:
    """Split into (headers, data rows) with every row padded to the header width."""
    if not rows:
        raise TableError("That file has no rows.")
    header_index = max(0, min(header_index, len(rows) - 1))
    raw_headers = rows[header_index]
    headers = []
    for position, cell in enumerate(raw_headers):
        label = " ".join((cell or "").split())
        headers.append(label or f"Column {position + 1}")
    width = len(headers)
    data = []
    for row in rows[header_index + 1 :]:
        if not any((cell or "").strip() for cell in row):
            continue
        padded = list(row[:width]) + [""] * max(0, width - len(row))
        data.append([(cell or "").strip() for cell in padded])
    return headers, data
