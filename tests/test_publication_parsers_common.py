"""publication_parsers._common: header cells, label cleaning and the sheet engine on synthetic grids.

These run in CI (no BI files needed). The real workbooks are covered by test_publication_golden.py.
"""
import datetime

import pytest

from publication_parsers import _common
from publication_parsers._common import (
    PublicationParseError,
    SheetSpec,
    clean_label,
    load_grid,
    load_label_indents,
    numbering_depth,
    parse_period,
    parse_series_sheet,
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
    ("- 3 bulan yang akan datang", "3 bulan yang akan datang"),
    ("17 Lapangan Usaha", "17 Lapangan Usaha"),
    ("1.", ""), ("  2.1 ", ""), ("a.", ""), ("-", ""), (3, ""), ("12", ""), (None, ""),
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


# ---------------------------------------------------------------------------
# The sheet engine
# ---------------------------------------------------------------------------

def test_monthly_sheet_skips_annual_columns_reads_sparse_years_and_ignores_the_mirror():
    grid = [
        [None, "Tabel I.1", None, None, None, None, None, None, None, None],
        [None, None, "Posisi Utang Luar Negeri / External Debt", None, None, None, None, None, None, None],
        [None, None, None, None, None, None, None, "(Juta USD / Million of USD)", None, None],
        [None, None, None, 2013, 2025, None, 2026, "ITEMS", 2026, None],
        [None, None, None, 2013, "Jul", "Agu*", "Jul**", "ITEMS", "Jul", None],
        [None, "1.", "Pemerintah / Government", 100.0, 110.0, 111.0, 120.0, "Government", 999.0, None],
        [None, "2.", "Swasta / Private", 50.0, 60.0, 61.0, 59.0, "Private", 999.0, None],
        [None, "TOTAL (1+2)", None, 150.0, 170.0, 172.0, 179.0, "Total", 999.0, None],
        [None, "Keterangan: * angka sementara", None, None, None, None, None, None, None, None],
    ]

    table = parse_series_sheet(grid, SheetSpec(label_cols=(1, 2), hierarchy="numbering"))

    assert table.title == "Posisi Utang Luar Negeri"
    assert table.unit == "Juta USD"
    assert table.row_labels == ["Pemerintah", "Swasta", "TOTAL"]
    assert table.lookup("Swasta", 2026, "Jul") == 59.0     # the mirror's 999 never wins
    assert table.lookup("Swasta", 2025, "Aug") == 61.0     # year carried over a blank cell
    assert not any(year == 2013 for _, year, _ in table._data)  # annual column skipped


def test_years_written_only_at_the_end_of_each_year_still_label_every_quarter():
    # SEKI's NPI table merges the year over all of a year's columns for early years, but later
    # writes it only on Q4 and on the annual column after it — and the last, partial year only
    # on its latest quarter.
    grid = [
        [None, "KETERANGAN", 2021, 2021, 2021, 2021, 2021, None, None, None, 2022, 2022, None, 2023],
        [None, "KETERANGAN", "Q1", "Q2", "Q3", "Q4", None, "Q1", "Q2", "Q3", "Q4", None, "Q1*", "Q2**"],
        [None, "Transaksi Berjalan", 1.0, 2.0, 3.0, 4.0, 10.0, 5.0, 6.0, 7.0, 8.0, 26.0, 9.0, 10.0],
    ]

    table = parse_series_sheet(grid, SheetSpec(label_cols=(1,), unit="Juta USD"))

    assert table.lookup("Transaksi Berjalan", 2021, "Q1") == 1.0
    assert table.lookup("Transaksi Berjalan", 2022, "Q1") == 5.0
    assert table.lookup("Transaksi Berjalan", 2022, "Q3") == 7.0
    assert table.lookup("Transaksi Berjalan", 2023, "Q1") == 9.0
    assert table.lookup("Transaksi Berjalan", 2023, "Q2") == 10.0
    assert len(table._data) == 10  # the two annual columns are not quarters


def test_a_year_missing_from_its_whole_run_follows_the_year_before():
    grid = [
        [None, None, 2025, 2025, None, None],
        [None, None, "Nov", "Des", "Jan", "Feb*"],
        [None, "Indeks", 1.0, 2.0, 3.0, 4.0],
    ]

    table = parse_series_sheet(grid, SheetSpec(label_cols=(1,), unit="Indeks"))

    assert table.lookup("Indeks", 2026, "Feb") == 4.0


def test_duplicate_labels_take_their_parents_from_the_indent_hierarchy():
    grid = [
        ["NERACA PEMBAYARAN", None, None, None],
        ["(Juta USD)", None, None, None],
        [None, "KETERANGAN", 2026, 2026],
        [None, "KETERANGAN", "Q1", "Q2*"],
        [None, "I. Transaksi Berjalan", 1.0, 2.0],
        [None, "A. Barang", 3.0, 4.0],
        [None, "- Ekspor", 5.0, 6.0],
        [None, "B. Jasa", 7.0, 8.0],
        [None, "- Ekspor", 9.0, 10.0],
    ]
    indents = {4: 0, 5: 1, 6: 2, 7: 1, 8: 2}

    table = parse_series_sheet(grid, SheetSpec(label_cols=(1,), hierarchy="indent"), indents)

    assert table.row_labels == [
        "Transaksi Berjalan", "Barang", "Barang > Ekspor", "Jasa", "Jasa > Ekspor",
    ]
    assert table.lookup("Jasa > Ekspor", 2026, "Q2") == 10.0
    assert table.unit == "Juta USD"


def test_a_label_still_ambiguous_under_its_parent_takes_the_grandparent_too():
    short, long_ = "1. Utang Jangka Pendek / Short-term", "2. Utang Jangka Panjang / Long-term"
    grid = [
        [None, None, None, 2026, 2026],
        [None, None, None, "Jun", "Jul"],
        [None, short, "1. Pemerintah dan Bank Sentral", 1.0, 2.0],
        [None, short, "1.1 Pemerintah", 3.0, 4.0],
        [None, short, "Total", 5.0, 6.0],
        [None, long_, "1. Pemerintah dan Bank Sentral", 7.0, 8.0],
        [None, long_, "1.1 Pemerintah", 9.0, 10.0],
        [None, long_, "Total", 11.0, 12.0],
    ]

    table = parse_series_sheet(
        grid, SheetSpec(label_cols=(1, 2), hierarchy="numbering", unit="Juta USD")
    )

    assert table.row_labels == [
        "Utang Jangka Pendek > Pemerintah dan Bank Sentral",
        "Utang Jangka Pendek > Pemerintah dan Bank Sentral > Pemerintah",
        "Utang Jangka Pendek > Total",
        "Utang Jangka Panjang > Pemerintah dan Bank Sentral",
        "Utang Jangka Panjang > Pemerintah dan Bank Sentral > Pemerintah",
        "Utang Jangka Panjang > Total",
    ]
    assert table.lookup("Utang Jangka Panjang > Pemerintah dan Bank Sentral > Pemerintah", 2026, "Jul") == 10.0


def test_letter_and_dash_items_nest_by_the_column_they_start_in():
    grid = [
        [None, None, None, None, 2026],
        [None, None, None, None, "Jul"],
        [None, "1.", "Pemerintah / Government", None, 10.0],
        [None, None, "a.", "Pinjaman / Loan", 4.0],
        [None, None, None, "-  Bilateral", 1.0],
        [None, "2.", "Bank Sentral / Central Bank", None, 6.0],
        [None, None, "a.", "Pinjaman / Loan", 0.0],
        [None, None, None, "-  Bilateral", 0.0],
    ]

    table = parse_series_sheet(
        grid, SheetSpec(label_cols=(1, 2, 3), hierarchy="numbering", unit="Juta USD")
    )

    assert table.row_labels == [
        "Pemerintah", "Pemerintah > Pinjaman", "Pemerintah > Pinjaman > Bilateral",
        "Bank Sentral", "Bank Sentral > Pinjaman", "Bank Sentral > Pinjaman > Bilateral",
    ]


def test_section_rows_head_their_block_and_bands_split_the_columns():
    grid = [
        [None, None, None, None, "TRIWULANAN (QTQ)", None, "TAHUNAN (YOY)", None],
        [None, "No.", "KOTA", "TIPE", 2026, 2026, 2026, 2026],
        [None, None, None, None, "Q1", "Q2", "Q1", "Q2"],
        [None, None, "BANDUNG", None, None, None, None, None],
        [None, 1, "  KECIL", "KECIL", 0.1, 0.2, 1.1, 1.2],
        [None, None, "TOTAL", "TOTAL", 0.3, 0.4, 1.3, 1.4],
        [None, None, "DENPASAR", None, None, None, None, None],
        [None, 2, "  KECIL", "KECIL", 0.5, 0.6, 1.5, 1.6],
    ]

    table = parse_series_sheet(
        grid, SheetSpec(label_cols=(2,), hierarchy="sections", bands=True, unit="%")
    )

    assert table.lookup("TAHUNAN (YOY) > DENPASAR > KECIL", 2026, "Q2") == 1.6
    assert table.lookup("TRIWULANAN (QTQ) > BANDUNG > TOTAL", 2026, "Q1") == 0.3
    assert "BANDUNG" not in table.row_labels  # a section heads rows; it holds no figures


def test_combined_header_nests_by_the_first_label_column():
    rasio, rasio_pem = (
        "- Rasio Pembayaran Utang / Debt Service Ratio",
        "- Rasio Pembayaran Utang Pemerintah / Government DSR",
    )
    grid = [
        [None, None, None, "(Dalam persen / In percent)", None, None],
        [None, None, None, "2009", "Q1-2013", "Q2-2026**"],
        [None, None, None, "2009", "Q1-2013", "Q2-2026**"],
        [None, rasio, rasio, 30.0, 25.0, 0.0],
        [None, None, "- DSR Tier-1", 20.0, 15.0, 13.7],
        [None, rasio_pem, rasio_pem, 10.0, 9.0, 0.0],
        [None, None, "- DSR Tier-1", 5.0, 4.0, 14.4],
    ]

    table = parse_series_sheet(
        grid, SheetSpec(label_cols=(1, 2), header="combined", hierarchy="first_col")
    )

    assert table.unit == "Dalam persen"
    assert table.lookup("Rasio Pembayaran Utang Pemerintah > DSR Tier-1", 2026, "Q2") == 14.4
    assert table.lookup("Rasio Pembayaran Utang > DSR Tier-1", 2013, "Q1") == 15.0
    assert not any(year == 2009 for _, year, _ in table._data)


def test_stacked_blocks_each_use_their_own_header_and_numbers_stored_as_text_are_read():
    grid = [
        [None, "Tabel 4. Suku Bunga", None, None, None, None, None],
        [None, "Periode", "Jenis Valuta", None, "2025", "2026", None],
        [None, "Periode", "Jenis Valuta", None, "IV", "I", None],
        [None, "Realisasi per Triwulan", "Rupiah", None, "5.70", "5.61", None],
        [None, "Periode", "Jenis Valuta", "Period", None, "2026", "2026"],
        [None, "Periode", "Jenis Valuta", "Period", None, "I", "II*"],
        [None, "Prakiraan per Triwulan", "Rupiah", "Estimation", None, "na", "5.40"],
        [None, "**) Sejak periode survei triwulan II 2021 ...", None, None, None, None, None],
    ]

    table = parse_series_sheet(grid, SheetSpec(label_cols=(1, 2), unit="%"))

    assert table.row_labels == ["Realisasi per Triwulan > Rupiah", "Prakiraan per Triwulan > Rupiah"]
    assert table.lookup("Realisasi per Triwulan > Rupiah", 2025, "Q4") == 5.70
    assert table.lookup("Prakiraan per Triwulan > Rupiah", 2026, "Q2") == 5.40
    assert table.lookup("Prakiraan per Triwulan > Rupiah", 2026, "Q1") is None  # 'na'


def test_a_sheet_without_a_period_header_is_rejected():
    grid = [["Daftar Seri SBN", None, None], [None, "FR0100", 5.0], [None, "FR0101", 6.0]]

    with pytest.raises(PublicationParseError, match="tahun/periode"):
        parse_series_sheet(grid, SheetSpec(label_cols=(1,)))


def test_a_header_without_any_figures_below_it_is_rejected():
    grid = [[None, None, 2026, 2026], [None, None, "Jan", "Feb"], [None, "Catatan", None, None]]

    with pytest.raises(PublicationParseError, match="berisi angka"):
        parse_series_sheet(grid, SheetSpec(label_cols=(1,)))
