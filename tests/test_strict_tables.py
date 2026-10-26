"""Strict extraction's view of the uploaded tables: codes, shape and rows, never the values."""
import pytest

from excel_parser_bi import BITableData
from paired_verifier import _ExcelSource
from strict.tables import build_views, find_row, parse_period, period_header, table_block


def _source(rows, periods, unit="Juta USD", title="Posisi Investasi Internasional", **kw):
    table = BITableData(title=title, unit=unit, row_labels=list(rows))
    for i, row in enumerate(rows):
        for year, period in periods:
            table._data[(row, year, period)] = 558496.4949 + i
    return _ExcelSource(table=table, filename="t.xls", sheet="5.39", **kw)


QUARTERS = [(2015, "Q1"), (2025, "Q3"), (2025, "Q4")]


def test_views_get_codes_and_skip_unusable_sources():
    a = _source(["Aset"], QUARTERS)
    pointer = _source([], [], pointer_only=True)
    categorical = _source(["Laptop"], [])
    categorical.table.axis_type = "categorical"
    pdf = _source(["Aset"], QUARTERS, origin="pdf")
    d = _source(["Kewajiban"], QUARTERS)

    views = build_views([a, pointer, categorical, pdf, d])

    assert [v.code for v in views] == ["S1", "S2"]
    assert views[0].source is a and views[1].source is d


def test_shape_names_axis_span_and_unit():
    [view] = build_views([_source(["Aset"], QUARTERS)])

    assert view.shape == "per triwulan, Q1 2015 s.d. Q4 2025, satuan Juta USD"


def test_a_monthly_table_without_unit():
    [view] = build_views([_source(["M2"], [(2026, "Aug"), (2020, "Jan")], unit="")])

    assert view.shape == "per bulan, Jan 2020 s.d. Aug 2026"


@pytest.mark.parametrize("text, key", [
    ("Q4 2025", (2025, "Q4")), ("Triwulan IV 2025", (2025, "Q4")), ("Tw IV 2025", (2025, "Q4")),
    ("TW I-2026", (2026, "Q1")), ("Dec 2025", (2025, "Dec")), ("Desember 2025", (2025, "Dec")),
    ("Des 2025", (2025, "Dec")), ("Mei 2026", (2026, "May")), ("Agustus 2026", (2026, "Aug")),
    ("S1 2026", (2026, "S1")), ("Semester I 2026", (2026, "S1")), ("akhir tahun", None), ("2025", None),
])
def test_parse_period(text, key):
    assert parse_period(text) == key


def test_period_header():
    assert [period_header(2025, "Q4"), period_header(2026, "Jun"), period_header(2026, "S1")] == \
        ["Q4 2025", "Jun 2026", "S1 2026"]


def test_find_row_folds_case_and_spaces_only():
    [view] = build_views([_source(["Uang Primer Adjusted 1)", "Aset > Investasi Lainnya"], QUARTERS)])

    assert find_row(view, "uang primer  adjusted 1)") == "Uang Primer Adjusted 1)"
    assert find_row(view, " ASET > investasi lainnya ") == "Aset > Investasi Lainnya"
    assert find_row(view, "Investasi Lainnya") is None


def test_block_shows_rows_and_shape_but_no_values():
    views = build_views([_source(["Aset", "Kewajiban"], QUARTERS)])

    block = table_block(views, chunk="Posisi AFLN Indonesia")

    assert "[S1: Posisi Investasi Internasional — per triwulan, Q1 2015 s.d. Q4 2025, satuan Juta USD]" in block
    assert "- Aset" in block and "- Kewajiban" in block
    assert "558496" not in block and "558.496" not in block


def test_block_keeps_every_row_of_a_254_row_sheet():
    rows = [f"Baris nomor {i}" for i in range(254)]
    views = build_views([_source(rows, QUARTERS)])

    block = table_block(views, chunk="x")

    assert all(f"- {row}" in block for row in rows)


def test_row_units_are_listed_under_the_header():
    src = _source(["Jumlah pelawat", "Ke dalam negeri"], QUARTERS)
    src.table.row_units = {"Ke dalam negeri": "ribuan orang"}

    block = table_block(build_views([src]), chunk="x")

    assert "Satuan khusus: 'Ke dalam negeri' = ribuan orang" in block
