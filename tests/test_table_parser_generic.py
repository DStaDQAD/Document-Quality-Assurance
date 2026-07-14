import io
from datetime import datetime

import pytest
from openpyxl import Workbook

from table_parser_generic import _parse_period, parse_generic_table


def _save(wb) -> bytes:
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# _parse_period
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Apr 2026", (2026, "Apr")),
        ("April 2026", (2026, "Apr")),
        ("Apr-26", (2026, "Apr")),
        ("Apr'26", (2026, "Apr")),
        ("Des 2025", (2025, "Dec")),
        ("Mei 2026", (2026, "May")),
        ("2026 Apr", (2026, "Apr")),
        ("2026M04", (2026, "Apr")),
        ("2026-04", (2026, "Apr")),
        ("04/2026", (2026, "Apr")),
        (datetime(2026, 4, 30), (2026, "Apr")),
    ],
)
def test_parse_period_recognizes_common_month_header_formats(raw, expected):
    assert _parse_period(raw) == expected


@pytest.mark.parametrize("raw", ["Total", "Harga", "2026", 2026.0, None, "Apr", "Q1 2026"])
def test_parse_period_rejects_non_month_headers(raw):
    assert _parse_period(raw) is None


# ---------------------------------------------------------------------------
# Categorical tables (item lists)
# ---------------------------------------------------------------------------

def _build_item_list_bytes():
    wb = Workbook()
    ws = wb.active
    ws.title = "Barang"
    ws.append(["Daftar Barang Elektronik"])
    ws.append(["Nama Barang", "Harga", "Stok"])
    ws.append(["Laptop ASUS", 7_500_000, 10])
    ws.append(["Mouse Logitech", 250_000, 45])
    ws.append(["Keyboard Mechanical", 850_000, 20])
    return _save(wb)


def test_parse_generic_table_detects_categorical_item_list():
    table = parse_generic_table(_build_item_list_bytes(), "Barang")

    assert table.axis_type == "categorical"
    assert table.title == "Daftar Barang Elektronik"
    assert table.row_labels == ["Laptop ASUS", "Mouse Logitech", "Keyboard Mechanical"]
    assert table.col_labels == ["Harga", "Stok"]
    assert table.lookup_cell("Laptop ASUS", "Harga") == 7_500_000.0
    assert table.lookup_cell("Keyboard Mechanical", "Stok") == 20.0


def test_parse_generic_table_skips_leading_numeric_index_column():
    wb = Workbook()
    ws = wb.active
    ws.title = "S"
    ws.append(["No", "Nama Barang", "Harga"])
    ws.append([1, "Laptop ASUS", 7_500_000])
    ws.append([2, "Mouse Logitech", 250_000])

    table = parse_generic_table(_save(wb), "S")

    assert table.axis_type == "categorical"
    assert table.row_labels == ["Laptop ASUS", "Mouse Logitech"]
    assert table.col_labels == ["Harga"]
    assert table.lookup_cell("Mouse Logitech", "Harga") == 250_000.0


def test_parse_generic_table_ignores_text_attribute_columns():
    # Text cells (e.g. 'Kategori') cannot be numerically verified — only numeric cells
    # are stored, but the numeric columns around them must still come through.
    wb = Workbook()
    ws = wb.active
    ws.title = "S"
    ws.append(["Nama Barang", "Kategori", "Harga"])
    ws.append(["Laptop ASUS", "Elektronik", 7_500_000])

    table = parse_generic_table(_save(wb), "S")

    assert table.lookup_cell("Laptop ASUS", "Harga") == 7_500_000.0
    assert table.lookup_cell("Laptop ASUS", "Kategori") is None


# ---------------------------------------------------------------------------
# Temporal tables with combined single-row period headers
# ---------------------------------------------------------------------------

def _build_combined_period_bytes():
    wb = Workbook()
    ws = wb.active
    ws.title = "Penjualan"
    ws.append(["Penjualan per Wilayah"])
    ws.append(["(juta Rp)"])
    ws.append(["Wilayah", "Jan 2026", "Feb 2026", "Mar 2026"])
    ws.append(["Jakarta", 120.0, 130.0, 125.0])
    ws.append(["Surabaya", 80.0, 85.0, 90.0])
    return _save(wb)


def test_parse_generic_table_detects_combined_period_headers_as_temporal():
    table = parse_generic_table(_build_combined_period_bytes(), "Penjualan")

    assert table.axis_type == "temporal"
    assert table.title == "Penjualan per Wilayah"
    assert table.unit == "juta Rp"
    assert table.row_labels == ["Jakarta", "Surabaya"]
    assert table.lookup("Jakarta", 2026, "Jan") == 120.0
    assert table.lookup("Surabaya", 2026, "Mar") == 90.0


def test_parse_generic_table_temporal_lookup_fuzzy_still_works():
    table = parse_generic_table(_build_combined_period_bytes(), "Penjualan")

    matched, value = table.lookup_fuzzy("jakarta", 2026, "Feb")

    assert matched == "Jakarta"
    assert value == 130.0


def test_parse_generic_table_reads_excel_date_cells_as_periods():
    wb = Workbook()
    ws = wb.active
    ws.title = "S"
    ws.append(["Metrik", datetime(2026, 1, 31), datetime(2026, 2, 28)])
    ws.append(["Produksi", 500.0, 520.0])

    table = parse_generic_table(_save(wb), "S")

    assert table.axis_type == "temporal"
    assert table.lookup("Produksi", 2026, "Jan") == 500.0
    assert table.lookup("Produksi", 2026, "Feb") == 520.0


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------

def test_parse_generic_table_raises_on_unrecognized_file_format():
    with pytest.raises(ValueError, match="Unrecognized file format"):
        parse_generic_table(b"not an excel file at all", "S")


def test_parse_generic_table_raises_when_sheet_not_found():
    with pytest.raises(ValueError, match="not found"):
        parse_generic_table(_build_item_list_bytes(), "WrongSheet")


def test_parse_generic_table_raises_when_no_header_structure_found():
    wb = Workbook()
    ws = wb.active
    ws.title = "S"
    for _ in range(5):
        ws.append(["hanya teks tanpa angka sama sekali"])

    with pytest.raises(ValueError, match="header"):
        parse_generic_table(_save(wb), "S")
