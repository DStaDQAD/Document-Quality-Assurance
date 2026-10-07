"""publication_parsers._common: header cells, label cleaning and the sheet engine on synthetic grids.

These run in CI (no BI files needed). The real workbooks are covered by test_publication_golden.py.
"""
import datetime

import pytest

from publication_parsers import _common
from publication_parsers._common import (
    PublicationParseError,
    clean_label,
    load_grid,
    load_label_indents,
    numbering_depth,
    parse_period,
    parse_year,
    parse_year_period,
)


# ---------------------------------------------------------------------------
# Header cells
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    (2026, 2026), (2026.0, 2026), ("2026", 2026), ("2026*", 2026), ("2010.0", 2010),
    (datetime.datetime(2007, 1, 1), 2007),
    ("ITEMS", None), ("Perubahan (Poin)", None), (51.49, None), (12, None), (True, None), (None, None),
])
def test_parse_year(raw, expected):
    assert parse_year(raw) == expected


@pytest.mark.parametrize("raw, expected", [
    ("Jan", "Jan"), ("Mei", "May"), ("Agt*", "Aug"), ("Agu", "Aug"), ("Jul**", "Jul"), ("Des", "Dec"),
    ("QI", "Q1"), ("QIII", "Q3"), ("QIV", "Q4"), ("Q2", "Q2"), ("III*", "Q3"), ("I", "Q1"), ("Tw II", "Q2"),
    ("2007", None), (2007, None), ("Perubahan (Poin)", None), ("", None), (None, None),
])
def test_parse_period(raw, expected):
    assert parse_period(raw) == expected


@pytest.mark.parametrize("raw, expected", [
    ("Q1-2013", (2013, "Q1")), ("Q2-2026**", (2026, "Q2")), ("2009", None), (2009, None), (None, None),
])
def test_parse_year_period(raw, expected):
    assert parse_year_period(raw) == expected


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("Pemerintah / Government ", "Pemerintah"),
    ("Pemerintah dan Bank Sentral/ Government and Central Bank", "Pemerintah dan Bank Sentral"),
    ("2.1.1. Bank / Bank", "Bank"),
    ("  2.2   Bukan Lembaga Keuangan / Nonfinancial Corporations", "Bukan Lembaga Keuangan"),
    ("I. Transaksi Berjalan", "Transaksi Berjalan"),
    ("III. Transaksi Finansial \ufffd", "Transaksi Finansial"),
    ("V.1. NERACA PEMBAYARAN INDONESIA", "NERACA PEMBAYARAN INDONESIA"),
    ("A. Barang", "Barang"),
    ("a. Nonmigas", "Nonmigas"),
    ("- Ekspor, fob", "Ekspor, fob"),
    ("A.D.B", "A.D.B"),
    ("I.B.R.D", "I.B.R.D"),
    ("KPR/KPA", "KPR/KPA"),
    ("PMI - BI", "PMI - BI"),
    ("DSR Tier-1", "DSR Tier-1"),
    ("TOTAL (1+2) ", "TOTAL"),
    ("TOTAL ( 1 + 2 )", "TOTAL"),
    ("Total (I + II + III)", "Total"),
    ("Semarang **", "Semarang"),
    ("Pinjaman yang Diberikan 2)", "Pinjaman yang Diberikan"),
    ("Aset", "Aset"),
    ("1.", ""), ("  2.1 ", ""), ("a.", ""), ("-", ""), (3, ""), (None, ""),
])
def test_clean_label(raw, expected):
    assert clean_label(raw) == expected


@pytest.mark.parametrize("raw, expected", [
    ("1. Pemerintah", 1), ("1.1", 2), ("2.1.1. Bank", 3), ("  2.2   Bukan", 2), (7, 1),
    ("TOTAL (1+2)", 0), ("a.", 0), ("- Bilateral", 0), (None, 0),
])
def test_numbering_depth(raw, expected):
    assert numbering_depth(raw) == expected


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def test_load_grid_opens_each_workbook_once_and_hands_out_copies(monkeypatch):
    opened = []

    def fake_load(data):
        opened.append(data)
        return {"A": [[1, 2]], "B": [[3]]}

    monkeypatch.setattr(_common, "load_workbook_grids", fake_load)
    _common._WORKBOOK_CACHE.clear()

    first = load_grid(b"book-1", "A")
    first[0][0] = "edited by a caller"

    assert load_grid(b"book-1", "A") == [[1, 2]]
    assert load_grid(b"book-1", "B") == [[3]]
    assert opened == [b"book-1"]


def test_load_grid_names_the_available_sheets_when_one_is_missing(monkeypatch):
    monkeypatch.setattr(_common, "load_workbook_grids", lambda data: {"A": []})
    _common._WORKBOOK_CACHE.clear()

    with pytest.raises(ValueError, match=r"Sheet 'Z' not found\. Available: \['A'\]"):
        load_grid(b"book-2", "Z")


def test_label_indents_are_only_read_from_xls_files():
    with pytest.raises(PublicationParseError, match=r"\.xls"):
        load_label_indents(b"PK\x03\x04 an xlsx file", "5.1", 2)
