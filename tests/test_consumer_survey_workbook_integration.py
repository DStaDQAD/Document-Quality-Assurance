"""Integration test: the real BI 'Survei Konsumen' monthly workbook (SK Juni 2026).

This workbook is the one that exposed the breakdown-vs-aggregate failure: its headline
indicators live in Tabel 1 while every breakdown of the SAME indicator lives in another
sheet (Tabel 2 per expenditure group, Tabel 3 per age, Tabel 4 per education, Tabel 6 per
city). Claims about a breakdown were resolving against Tabel 1's national row, so this
exercises the whole path — parser cascade, label composition, and source selection — with
values read directly from the workbook's cells.

Skipped automatically when the sample workbook is not present.
"""

from pathlib import Path

import pytest

from paired_verifier import _ExcelSource, _evaluate_fact, _parse_table_with_fallback
from structured_extractor import ExtractedFact, PeriodPoint

_XLSX = (
    Path(__file__).resolve().parent.parent
    / "sample_data"
    / "Tabel Series SK Juni 2026.xlsx"
)

pytestmark = pytest.mark.skipif(not _XLSX.exists(), reason="sample consumer-survey workbook not present")

_SHEETS = [f"Tabel {n}" for n in range(1, 10)]


@pytest.fixture(scope="module")
def sources():
    data = _XLSX.read_bytes()
    out = []
    for name in _SHEETS:
        table, _ = _parse_table_with_fallback(data, name, llm=None)
        out.append(_ExcelSource(table=table, filename=_XLSX.name, sheet=name))
    return out


def _resolve(sources, metric_label, year=2026, month="Jun"):
    """Run one value claim through the real pipeline and report (sheet, matched row, value)."""
    fact = ExtractedFact(
        operation="value",
        periods=[PeriodPoint(metric_label=metric_label, year=year, month=month)],
        claimed_value=0.0, unit=None, context_quote="", page_number=1,
    )
    result = _evaluate_fact(fact, sources)
    if not result.periods:
        return None, None, None
    sheet = (result.matched_excel_source or "").rsplit(" / ", 1)[-1]
    return sheet, result.periods[0].metric_label, result.periods[0].excel_value


def test_every_sheet_parses_with_row_data(sources):
    for src in sources:
        assert src.table.row_labels, f"{src.sheet} produced no row labels"
        assert src.table._data, f"{src.sheet} produced no values"


def test_per_city_sheet_keeps_every_city_separate(sources):
    # Tier 1 read the metric column only and collapsed all 18 cities onto Jakarta's values.
    per_city = next(s for s in sources if s.sheet == "Tabel 6")

    assert len(set(per_city.table.row_labels)) == len(per_city.table.row_labels)
    assert len(per_city.table.row_labels) == 108


@pytest.mark.parametrize("metric,sheet,value", [
    # Headline indicators — must stay on Tabel 1, not drift to a breakdown row.
    ("Indeks Keyakinan Konsumen (IKK)", "Tabel 1", 117.8),
    ("Indeks Kondisi Ekonomi Saat Ini (IKE)", "Tabel 1", 109.2),
    ("Indeks Ekspektasi Konsumen (IEK)", "Tabel 1", 126.4),
    ("Indeks Penghasilan Saat Ini (IPSI)", "Tabel 1", 119.8),
    ("Indeks Ketersediaan Lapangan Kerja (IKLK)", "Tabel 1", 101.8),
    # Breakdowns — must reach the sheet that actually carries them.
    ("Indeks Keyakinan Konsumen (IKK) > Pengeluaran >Rp5 juta", "Tabel 2", 121.4),
    ("Indeks Penghasilan Saat Ini (IPSI) > Pengeluaran >Rp5 juta", "Tabel 2", 129.2),
    ("Indeks Pembelian Barang Tahan Lama (Durable Goods) (IPDG) > Pengeluaran >Rp5 juta",
     "Tabel 2", 108.8),
    # Narrative spacing ('Rp4,1-5') differs from the sheet's ('Rp4,1 - 5').
    ("Indeks Ekspektasi Kegiatan Usaha (IEKU) > Pengeluaran Rp4,1-5 juta", "Tabel 2", 118.8),
    ("Indeks Ketersediaan Lapangan Kerja (IKLK) > Sarjana", "Tabel 4", 109.0),
    ("Indeks Kondisi Ekonomi Saat Ini (IKE) > Mataram", "Tabel 6", 119.2),
    ("Rp 2,1 - 3 juta > Konsumsi", "Tabel 5", 75.2),
])
def test_claim_resolves_against_the_right_sheet_and_cell(sources, metric, sheet, value):
    got_sheet, _, got_value = _resolve(sources, metric)

    assert (got_sheet, got_value) == (sheet, value)


@pytest.mark.parametrize("metric", [
    # A group the workbook does not list as a row: 'lainnya' is the unnamed remainder, and
    # '>41 tahun' spans three separate age rows. Answering either from the national row
    # would be a confident wrong verdict, so both must stay unresolved.
    "Indeks Ketersediaan Lapangan Kerja (IKLK) > tingkat pendidikan lainnya",
    "Indeks Ketersediaan Lapangan Kerja (IKLK) > Usia >41 tahun",
])
def test_claim_about_a_group_with_no_row_stays_unresolved(sources, metric):
    assert _resolve(sources, metric) == (None, None, None)
