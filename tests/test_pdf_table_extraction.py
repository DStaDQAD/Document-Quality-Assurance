import asyncio
from unittest.mock import AsyncMock, Mock, patch

import pytest

from paired_verifier import _parse_grid_with_fallback
from pdf_table_extraction import (
    _TABLE_CACHE,
    PdfTable,
    _assemble_grid,
    _coerce_cell,
    _is_usable,
    _PageTables,
    _reconstruct_year_row,
    _unit_from_caption,
    _PdfTableOut,
    extract_tables_from_pdf,
)
from table_parser_generic import _is_number, _is_year_cell, parse_generic_grid


@pytest.fixture(autouse=True)
def _clear_cache():
    _TABLE_CACHE.clear()
    yield
    _TABLE_CACHE.clear()


def _table_out(**overrides):
    base = dict(
        caption="Lampiran 1. Tabel Uang Beredar dan Faktor yang Memengaruhinya",
        unit="(Triliun Rp)",
        header_rows=[["", "2026", "2026"], ["", "Apr", "Mei"]],
        rows=[
            ["Uang Beredar (M2)", "10.255,3", "10.415,9"],
            ["Aktiva Luar Negeri Bersih", "2.052,5", "2.056,3"],
            ["Surat Berharga Selain Saham **", "53,6", "(49,8)"],
        ],
    )
    base.update(overrides)
    return _PdfTableOut(**base)


def _gemini_llm(*returns):
    """A vision LLM mock whose structured-output channel returns each value in turn."""
    structured = Mock()
    structured.ainvoke = AsyncMock(side_effect=list(returns) * 20)
    llm = Mock()
    llm.with_structured_output = Mock(return_value=structured)
    llm.model = "gemini-2.5-flash"
    type(llm).__name__ = "ChatGoogleGenerativeAI"
    return llm, structured


# ---------------------------------------------------------------------------
# _coerce_cell — the mandatory step: every downstream consumer gates on
# table_parser_generic._is_number, which is an isinstance check.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("10.415,9", 10415.9),        # Indonesian thousands dot + decimal comma
    ("8.759,0", 8759.0),
    ("9,2", 9.2),
    ("10,8%", 10.8),              # printed percent
    ("(49,8)", -49.8),            # BI accounting negative
    ("-9,2", -9.2),
    ("2026", 2026),               # a year, as int (see below)
])
def test_coerce_cell_parses_printed_numbers(text, expected):
    assert _coerce_cell(text) == pytest.approx(expected)
    assert _is_number(_coerce_cell(text))


@pytest.mark.parametrize("text", ["1)", "-", "n.a.", "Uang Beredar (M2)", "Apr", "Jan '26", "..."])
def test_coerce_cell_keeps_non_numbers_as_strings(text):
    # '1)' is the trap: pdf_extraction._CELL_NUMERIC_RE accepts an unbalanced paren and would
    # turn this enumeration marker into the number 1.
    assert _coerce_cell(text) == text


def test_coerce_cell_maps_blank_to_none():
    assert _coerce_cell("") is None
    assert _coerce_cell("   ") is None
    assert _coerce_cell(None) is None


def test_coerce_cell_returns_int_for_whole_numbers():
    # Two consumers depend on this: _is_year_cell accepts int, and parse_generic_grid renders
    # categorical column headers with str(cell) — where 2026.0 would become the label "2026.0".
    year = _coerce_cell("2026")
    assert isinstance(year, int)
    assert _is_year_cell(year)
    assert str(year) == "2026"


def test_coerce_cell_keeps_decimals_as_float():
    assert isinstance(_coerce_cell("10.415,9"), float)


# ---------------------------------------------------------------------------
# _assemble_grid
# ---------------------------------------------------------------------------

def test_assemble_grid_puts_caption_and_unit_on_their_own_rows():
    grid = _assemble_grid(_table_out())
    # A single-cell row cannot be mistaken for a header row (_find_header_row needs >= 2
    # non-empty cells), and _title_and_unit reads exactly this shape.
    assert grid[0][0].startswith("Lampiran 1.")
    assert grid[1][0] == "(Triliun Rp)"
    assert all(c is None for c in grid[0][1:])


def test_assemble_grid_pads_every_row_to_the_same_width():
    grid = _assemble_grid(_table_out(rows=[["a", "1,0"], ["b", "2,0", "3,0", "4,0"]]))
    assert len({len(r) for r in grid} ) == 1


def test_assemble_grid_spreads_a_single_year_over_all_period_columns():
    out = _table_out(
        header_rows=[["", "2026", "", "", ""], ["", "Jan", "Feb", "Mar", "Apr"]],
        rows=[["M2", "1,0", "", "", "4,0"], ["M1", "5,0", "6,0", "7,0", "8,0"]],
    )
    grid = _assemble_grid(out)
    # The year must span a contiguous run so _years_form_merged_runs recognises it, exactly
    # like openpyxl's merged-range fill does for a real sheet.
    assert grid[2][1:] == [2026, 2026, 2026, 2026]
    # Body rows are NEVER filled sideways: that would invent data.
    body = next(r for r in grid if r[0] == "M2")
    assert body[2] is None and body[3] is None


# ---------------------------------------------------------------------------
# _reconstruct_year_row — the highest-risk correction in this module. Both shapes below were
# measured on sample_data/M2-April-2026.pdf: the model copies the PERIOD row faithfully but
# mis-positions the years, which would file real numbers under the wrong month.
# ---------------------------------------------------------------------------

_M2_MONTHS = ["Uraian", "Apr", "Mei", "Jun", "Jul", "Agu", "Sep", "Okt", "Nov", "Des",
              "Jan", "Feb", "Mar", "Apr", "Mei*"]
# Apr..Des belong to 2025; the Des->Jan wrap starts 2026.
_M2_EXPECTED = [None] + [2025] * 9 + [2026] * 5


def test_reconstruct_year_row_fixes_years_written_only_once():
    # Lampiran 1's shape: each year written a single time, and 2026 landing on 'Mei' — a 2025
    # column. A naive forward-fill would date nine months wrongly.
    assert _reconstruct_year_row([None, 2025, 2026] + [None] * 12, _M2_MONTHS) == _M2_EXPECTED


def test_reconstruct_year_row_fixes_years_spread_over_the_wrong_columns():
    # Lampiran 2's shape: 12x2025 then 3x2026, so Jan/Feb/Mar 2026 arrive labelled 2025.
    assert _reconstruct_year_row([None] + [2025] * 12 + [2026] * 2, _M2_MONTHS) == _M2_EXPECTED


def test_reconstruct_year_row_handles_quarterly_headers():
    quarters = ["Uraian", "I", "II", "III", "IV", "I", "II"]
    assert _reconstruct_year_row([None, 2025, None, None, None, 2026, None], quarters) == \
        [None, 2025, 2025, 2025, 2025, 2026, 2026]


def test_reconstruct_year_row_leaves_ambiguous_rows_untouched():
    # Two wraps imply three year blocks, but only two years were written — refuse to guess.
    months = ["Uraian", "Nov", "Des", "Jan", "Des", "Jan"]
    written = [None, 2024, None, 2025, None, None]
    assert _reconstruct_year_row(written, months) == written


def test_reconstruct_year_row_leaves_row_untouched_when_no_year_was_written():
    months = ["Uraian", "Jan", "Feb", "Mar"]
    assert _reconstruct_year_row([None, None, None, None], months) == [None, None, None, None]


def test_assemble_grid_reconstructs_years_end_to_end():
    out = _table_out(
        header_rows=[[""] + ["2025", "2026"] + [""] * 12, _M2_MONTHS[:1] + _M2_MONTHS[1:]],
        rows=[["M2"] + ["1,0"] * 14, ["M1"] + ["2,0"] * 14],
    )
    grid = _assemble_grid(out)
    assert grid[2][10] == 2026, "the Des->Jan wrap must start the next year"
    assert grid[2][9] == 2025


# ---------------------------------------------------------------------------
# _unit_from_caption — an empty unit is not a harmless gap; it produces false Refuted verdicts.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("caption,expected", [
    ("Tabel 9. Komponen Uang Primer adjusted (triliun Rp)", "triliun Rp"),
    ("Lampiran 3. Tabel Dana Pihak Ketiga di Perbankan (Triliun Rp)", "Triliun Rp"),
    ("Lampiran 2. Pertumbuhan Uang Beredar (%, yoy)", "%, yoy"),
    ("Tabel 5. Indeks Keyakinan Konsumen (Indeks)", "Indeks"),
])
def test_unit_from_caption_recovers_an_inline_unit(caption, expected):
    assert _unit_from_caption(caption) == expected


@pytest.mark.parametrize("caption", [
    "Tabel 1. Uang Beredar Sempit (M1)",          # an aside, not a unit
    "Grafik 2. Pertumbuhan M2 (yoy)",
    "Lampiran 1. Uang Beredar dan Faktor-Faktornya",
    "",
])
def test_unit_from_caption_ignores_non_units(caption):
    assert _unit_from_caption(caption) == ""


def test_assemble_grid_falls_back_to_the_caption_unit():
    # The measured failure: the model copies the unit into the caption and leaves the field
    # empty, so paired_verifier scales a trillions table by 1e12 and computes ~0.0.
    grid = _assemble_grid(_table_out(
        caption="Tabel 9. Komponen Uang Primer adjusted (triliun Rp)", unit=None,
    ))
    assert grid[1][0] == "(triliun Rp)"
    assert parse_generic_grid(grid).unit == "triliun Rp"


def test_extract_tables_fills_the_unit_from_the_caption():
    llm, _ = _gemini_llm(_PageTables(tables=[_table_out(
        caption="Tabel 9. Komponen Uang Primer adjusted (triliun Rp)", unit=None,
    )]))
    with patch("pdf_table_extraction.render_pages_to_b64", return_value=["a"]):
        tables = asyncio.run(extract_tables_from_pdf(b"%PDF-fake", llm))
    assert tables[0].unit == "triliun Rp"


def test_assemble_grid_omits_unit_row_when_no_unit_was_printed():
    grid = _assemble_grid(_table_out(unit=None))
    assert grid[0][0].startswith("Lampiran 1.")
    assert grid[1][0] == "Uang Beredar (M2)" or grid[1][1] == 2026


# ---------------------------------------------------------------------------
# _is_usable — structural rejection of garbled transcriptions
# ---------------------------------------------------------------------------

def test_is_usable_accepts_a_well_formed_table():
    assert _is_usable(_table_out(), 7)


def test_is_usable_rejects_table_without_header():
    assert not _is_usable(_table_out(header_rows=[]), 7)


def test_is_usable_rejects_table_with_fewer_than_two_body_rows():
    assert not _is_usable(_table_out(rows=[["M2", "1,0", "2,0"]]), 7)


def test_is_usable_rejects_table_with_no_numeric_cell():
    assert not _is_usable(
        _table_out(rows=[["M2", "n.a.", "-"], ["M1", "-", "-"]]), 7
    )


def test_is_usable_rejects_body_row_that_repeats_the_header():
    # The fingerprint of two stacked tables transcribed as one: the second table's header
    # row landed in the first table's body.
    out = _table_out(rows=_table_out().rows + [["", "Apr", "Mei"]])
    assert not _is_usable(out, 9)


# ---------------------------------------------------------------------------
# The end-to-end proof: an assembled grid is digestible by the UNTOUCHED Excel cascade.
# ---------------------------------------------------------------------------

def test_assembled_grid_parses_through_the_generic_parser():
    grid = _assemble_grid(_table_out())
    table = parse_generic_grid(grid)

    assert table.title.startswith("Lampiran 1.")
    assert table.unit == "Triliun Rp"
    assert table.axis_type == "temporal"
    assert "Uang Beredar (M2)" in table.row_labels
    # 'Mei' must have been normalised to the canonical English abbreviation.
    assert table.lookup_fuzzy("Uang Beredar (M2)", 2026, "Apr") == ("Uang Beredar (M2)", 10255.3)
    assert table.lookup_fuzzy("Uang Beredar (M2)", 2026, "May") == ("Uang Beredar (M2)", 10415.9)
    # The accounting negative survived the whole trip.
    assert table.lookup_fuzzy("Surat Berharga Selain Saham **", 2026, "May")[1] == -49.8


def test_assembled_grid_goes_through_the_paired_cascade_as_generic():
    table, parser = _parse_grid_with_fallback(_assemble_grid(_table_out()))
    assert parser == "generic"
    assert table.unit == "Triliun Rp"


def test_parse_grid_with_fallback_raises_when_no_tier_understands_the_grid():
    with pytest.raises(ValueError):
        _parse_grid_with_fallback([["just text"], ["more text"]])


# ---------------------------------------------------------------------------
# extract_tables_from_pdf
# ---------------------------------------------------------------------------

def test_extract_tables_returns_one_table_per_page_with_page_numbers():
    llm, structured = _gemini_llm(_PageTables(tables=[_table_out()]))
    with patch("pdf_table_extraction.render_pages_to_b64", return_value=["a", "b", "c"]):
        tables = asyncio.run(extract_tables_from_pdf(b"%PDF-fake", llm))

    assert [t.page_number for t in tables] == [1, 2, 3]
    assert structured.ainvoke.call_count == 3
    assert tables[0].label.startswith("Hal. 1 · Lampiran 1.")
    assert tables[0].unit == "Triliun Rp"


def test_extract_tables_handles_two_tables_on_one_page():
    page = _PageTables(tables=[
        _table_out(caption="Lampiran 4. Suku Bunga Kredit"),
        _table_out(caption="Lampiran 5. Suku Bunga Simpanan"),
    ])
    llm, _ = _gemini_llm(page)
    with patch("pdf_table_extraction.render_pages_to_b64", return_value=["a"]):
        tables = asyncio.run(extract_tables_from_pdf(b"%PDF-fake", llm))

    assert len(tables) == 2
    assert [t.index_on_page for t in tables] == [0, 1]
    assert [t.page_number for t in tables] == [1, 1]


def test_extract_tables_skips_a_page_whose_call_fails_without_raising():
    structured = Mock()
    structured.ainvoke = AsyncMock(side_effect=[
        RuntimeError("boom"), _PageTables(tables=[_table_out()]),
    ])
    llm = Mock()
    llm.with_structured_output = Mock(return_value=structured)
    llm.model = "gemini-2.5-flash"
    type(llm).__name__ = "ChatGoogleGenerativeAI"

    with patch("pdf_table_extraction.render_pages_to_b64", return_value=["a", "b"]):
        tables = asyncio.run(extract_tables_from_pdf(b"%PDF-fake", llm))

    # One page contributed nothing; the document still produced a result.
    assert [t.page_number for t in tables] == [2]


def test_extract_tables_returns_empty_when_no_page_holds_a_table():
    llm, _ = _gemini_llm(_PageTables(tables=[]))
    with patch("pdf_table_extraction.render_pages_to_b64", return_value=["a", "b"]):
        assert asyncio.run(extract_tables_from_pdf(b"%PDF-fake", llm)) == []


def test_extract_tables_drops_unusable_transcriptions():
    llm, _ = _gemini_llm(_PageTables(tables=[_table_out(rows=[["M2", "1,0", "2,0"]])]))
    with patch("pdf_table_extraction.render_pages_to_b64", return_value=["a"]):
        assert asyncio.run(extract_tables_from_pdf(b"%PDF-fake", llm)) == []


def test_extract_tables_serves_a_repeat_run_from_cache():
    llm, structured = _gemini_llm(_PageTables(tables=[_table_out()]))
    with patch("pdf_table_extraction.render_pages_to_b64", return_value=["a", "b"]):
        first = asyncio.run(extract_tables_from_pdf(b"%PDF-fake", llm))
        second = asyncio.run(extract_tables_from_pdf(b"%PDF-fake", llm))

    assert structured.ainvoke.call_count == 2, "second run must not re-pay for transcription"
    assert [t.label for t in second] == [t.label for t in first]


def test_extract_tables_does_not_serve_a_different_pdf_from_cache():
    llm, structured = _gemini_llm(_PageTables(tables=[_table_out()]))
    with patch("pdf_table_extraction.render_pages_to_b64", return_value=["a"]):
        asyncio.run(extract_tables_from_pdf(b"%PDF-one", llm))
        asyncio.run(extract_tables_from_pdf(b"%PDF-two", llm))
    assert structured.ainvoke.call_count == 2


def test_extract_tables_reports_progress():
    seen = []
    llm, _ = _gemini_llm(_PageTables(tables=[_table_out()]))
    with patch("pdf_table_extraction.render_pages_to_b64", return_value=["a", "b"]):
        asyncio.run(extract_tables_from_pdf(
            b"%PDF-fake", llm, on_progress=lambda done, total: seen.append((done, total))
        ))
    assert seen == [(1, 2), (2, 2)]


def test_pdf_table_label_falls_back_when_caption_is_missing():
    assert PdfTable(page_number=3, caption="", unit="", index_on_page=1).label == "Hal. 3 · Tabel 2"
