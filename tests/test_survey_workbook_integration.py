"""Integration test: the real BI 'Survei Perbankan' quarterly workbook.

Exercises the full parser cascade (no LLM constructed — tier 2 must win) against all
five sheets of the sample file. Expected values were read directly from the workbook's
cells; percent-formatted cells (Tabel4) are asserted at DISPLAY scale (0.0577 -> 5.77).

Skipped automatically when the sample workbook is not present (e.g. a fresh clone
without sample_data).
"""

from pathlib import Path

import pytest

from paired_verifier import _parse_table_with_fallback

_XLSX = (
    Path(__file__).resolve().parent.parent
    / "sample_data"
    / "Data Series Survei Perbankan_ Triwulan II 2_026_.xlsx"
)

pytestmark = pytest.mark.skipif(not _XLSX.exists(), reason="sample survey workbook not present")

_SHEETS = ["Tabel1", "Tabel2", "Tabel3", "Tabel4", "Tabel 5 (disc)"]


@pytest.fixture(scope="module")
def workbook_bytes():
    return _XLSX.read_bytes()


@pytest.fixture(scope="module")
def tables(workbook_bytes):
    return {
        name: _parse_table_with_fallback(workbook_bytes, name, llm=None)
        for name in _SHEETS
    }


def test_all_sheets_parse_via_generic_tier_as_temporal(tables):
    for name, (table, parser) in tables.items():
        assert parser == "generic", f"{name} parsed via {parser}"
        assert table.axis_type == "temporal", f"{name} axis {table.axis_type}"
        assert len(table.row_labels) >= 8, f"{name} only {len(table.row_labels)} rows"
        assert len(table._data) >= 400, f"{name} only {len(table._data)} cells"


def test_tabel1_values_and_fuzzy_lookup(tables):
    table, _ = tables["Tabel1"]

    label, value = table.lookup_fuzzy("Kredit Modal Kerja", 2012, "Q1")
    assert "Kredit Modal Kerja" in label
    assert value == 63.55
    assert table.lookup_fuzzy("Kredit Investasi", 2013, "Q2")[1] == 82.02


def test_tabel2_rank_codes_and_second_block(tables):
    table, _ = tables["Tabel2"]

    kmk = next(l for l in table.row_labels if "Kredit Modal Kerja" in l)
    assert table.lookup(kmk, 2013, "Q1") == 1.0
    assert any("Setahun" in l for l in table.row_labels)  # second stacked block present


def test_tabel3_stacked_segments_are_disambiguated(tables):
    table, _ = tables["Tabel3"]

    per_tw = next(l for l in table.row_labels if "Giro" in l and "per Triwulan" in l)
    setahun = next(l for l in table.row_labels if "Giro" in l and "Setahun" in l)
    assert table.lookup(per_tw, 2013, "Q2") == 91.9
    assert table.lookup(setahun, 2013, "Q1") == 77.2


def test_tabel4_percent_cells_stored_at_display_scale(tables):
    table, _ = tables["Tabel4"]

    assert table.lookup(
        "Realisasi per Triwulan > Rupiah > Cost of Funds", 2012, "Q1"
    ) == pytest.approx(5.77)


def test_tabel5_total_row_and_no_garbage(tables):
    table, _ = tables["Tabel 5 (disc)"]

    assert table.lookup("Total", 2012, "Q1") == pytest.approx(59.52)
    # The old single-row path stored the year header itself as a data value
    # (('Periode','I') = 2012.0) — integral year-range values must never appear.
    assert not any(
        float(v).is_integer() and 1990 <= v <= 2100 for v in table._data.values()
    )
    assert not any("*)" in l for l in table.row_labels)  # footnotes excluded
