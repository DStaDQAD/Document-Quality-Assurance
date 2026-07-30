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
    _align_rows,
    _labels_match,
    _text_layer_rows,
    _verify_against_text_layer,
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
# Text-layer verification — where the file states the numbers, the file wins.
# ---------------------------------------------------------------------------

_PAGE_7_TEXT = """\
 Departemen Statistik 7
Lampiran 1. Tabel Uang Beredar dan Faktor -Faktor yang Memengaruhinya (Triliun Rp)
Keterangan:
*Angka sementara
Apr Mei Jun
Uang Beredar (M2) 9.387,9 9.404,3 9.595,3
Uang Beredar Sempit (M1) 5.223,6 5.224,9 5.407,7
Uang Kuasi 4.060,8 4.076,3 4.123,0
"""


def _three_col_table(**overrides):
    base = dict(
        caption="Lampiran 1. Tabel Uang Beredar dan Faktor-Faktor yang Memengaruhinya",
        unit="(Triliun Rp)",
        header_rows=[["", "2025", "2025", "2025"], ["", "Apr", "Mei", "Jun"]],
        rows=[
            ["Uang Beredar (M2)", "9.387,9", "9.404,3", "9.595,3"],
            ["Uang Beredar Sempit (M1)", "5.223,6", "5.224,9", "5.407,7"],
            ["Uang Kuasi", "4.060,8", "4.076,3", "4.123,0"],
        ],
    )
    base.update(overrides)
    return _PdfTableOut(**base)


def _as_pdf_table(out, page=7):
    return PdfTable(page_number=page, caption=out.caption or "", unit="Triliun Rp",
                    grid=_assemble_grid(out))


def _body(table, label):
    return next(r for r in table.grid if r[0] == label)


def test_text_layer_rows_reads_label_and_values():
    rows = _text_layer_rows(_PAGE_7_TEXT)
    assert [label for label, _ in rows] == [
        "Uang Beredar (M2)", "Uang Beredar Sempit (M1)", "Uang Kuasi",
    ]
    assert rows[0][1] == [9387.9, 9404.3, 9595.3]


def test_text_layer_rows_ignores_prose_and_headers():
    text = (
        "M2 pada April 2026 tercatat sebesar Rp10.355,1 triliun atau tumbuh 9,7% (yoy).\n"
        "Apr Mei Jun Jul\n"
        "Posisi GWM Januari 2020 (5,5%), Mei 2020 (3%)\n"
    )
    assert _text_layer_rows(text) == []


def test_verification_repairs_a_misread_digit():
    # The measured case: 5.224,9 read as 5.274,9, which turned a correct 'M1 tumbuh 15,3%'
    # into Tidak Sesuai.
    out = _three_col_table(rows=[
        ["Uang Beredar (M2)", "9.387,9", "9.404,3", "9.595,3"],
        ["Uang Beredar Sempit (M1)", "5.223,6", "5.274,9", "5.407,7"],
        ["Uang Kuasi", "4.060,8", "4.076,3", "4.123,0"],
    ])
    table = _as_pdf_table(out)
    _verify_against_text_layer([table], ["", "", "", "", "", "", _PAGE_7_TEXT])

    assert _body(table, "Uang Beredar Sempit (M1)")[1:] == [5223.6, 5224.9, 5407.7]
    assert table.verified is True


def test_verification_repairs_values_attached_to_the_wrong_label():
    # The failure no structural check can see: the grid is well-formed, but a value row is
    # attached to the label above it. Measured on Lampiran 6 of the M2 report.
    out = _three_col_table(rows=[
        ["Uang Beredar (M2)", "5.223,6", "5.224,9", "5.407,7"],       # M1's values
        ["Uang Beredar Sempit (M1)", "4.060,8", "4.076,3", "4.123,0"],  # Uang Kuasi's
        ["Uang Kuasi", "1.000,0", "1.000,0", "1.000,0"],
    ])
    table = _as_pdf_table(out)
    _verify_against_text_layer([table], ["", "", "", "", "", "", _PAGE_7_TEXT])

    assert _body(table, "Uang Beredar (M2)")[1:] == [9387.9, 9404.3, 9595.3]
    assert _body(table, "Uang Beredar Sempit (M1)")[1:] == [5223.6, 5224.9, 5407.7]
    assert _body(table, "Uang Kuasi")[1:] == [4060.8, 4076.3, 4123.0]


def test_verification_empties_a_row_whose_cell_count_disagrees():
    # The text layer says how many values the row has, but not which columns the gaps are in.
    out = _three_col_table(rows=[
        ["Uang Beredar (M2)", "9.387,9", "", "9.595,3"],
        ["Uang Beredar Sempit (M1)", "5.223,6", "5.224,9", "5.407,7"],
        ["Uang Kuasi", "4.060,8", "4.076,3", "4.123,0"],
    ])
    table = _as_pdf_table(out)
    _verify_against_text_layer([table], ["", "", "", "", "", "", _PAGE_7_TEXT])

    assert all(c is None for c in _body(table, "Uang Beredar (M2)")[1:])
    # An emptied row stops being an answer rather than becoming a wrong one.
    assert "Uang Beredar (M2)" not in parse_generic_grid(table.grid).row_labels
    # Its neighbours are untouched.
    assert _body(table, "Uang Kuasi")[1:] == [4060.8, 4076.3, 4123.0]


@pytest.mark.parametrize("transcribed,printed", [
    # Measured on page 9 of the M2 report: the PDF prints these labels into columns too narrow
    # to hold them, so the page is visually truncated and the model copies what it can see.
    ("pertanianpeternakankehutanan", "pertanianpeternakankehutanandanperikanan"),
    ("industripengolahandansejenisny", "industripengolahandansejenisnya"),
    ("perdaganganhoteldanrestorar", "perdaganganhoteldanrestoran"),   # misread final letter
    ("keuanganrealestatdanjasape", "keuanganrealestatdanjasaperusahaan"),
])
def test_labels_match_tolerates_visual_truncation(transcribed, printed):
    assert _labels_match(transcribed, printed)


@pytest.mark.parametrize("a,b", [
    ("kreditinvestasi", "kreditkonsumsi"),
    ("kreditmodalkerja", "kreditinvestasi"),
    ("uangkuasi", "uangkartal"),
    ("rupiah", "valas"),
])
def test_labels_match_rejects_different_rows(a, b):
    assert not _labels_match(a, b)


def test_align_rows_keeps_repeated_labels_in_order():
    # 'pertambangan' appears once per credit type; only order tells the two apart.
    transcribed = ["kreditinvestasi", "pertambangan", "kreditmodalkerja", "pertambangan"]
    printed = ["kreditinvestasi", "pertambangan", "kreditmodalkerja", "pertambangan"]
    assert _align_rows(transcribed, printed) == {0: 0, 1: 1, 2: 2, 3: 3}


def test_align_rows_never_fuzzy_matches_a_label_that_exists_verbatim():
    # Measured on page 10: 'Giro Bank Umum di BI' is a legitimate prefix of the SEPARATE row
    # 'Giro Bank Umum di BI Adjusted 2)'. Fuzzy-pairing them filed one row's numbers under the
    # other's name — 15 wrong cells. Both exist verbatim, so there was nothing to guess.
    both = ["girobankumumdibi", "girobankumumdibiadjusted2"]
    assert _align_rows(both, both) == {0: 0, 1: 1}
    # Only the longer row transcribed: it must still take its OWN printed row, not the prefix.
    assert _align_rows(["girobankumumdibiadjusted2"], both) == {0: 1}
    # And the prefix row alone must take the prefix row.
    assert _align_rows(["girobankumumdibi"], both) == {0: 0}


def test_align_rows_skips_a_row_only_one_side_has():
    transcribed = ["alpha", "hantu", "gamma"]
    printed = ["alpha", "beta", "gamma"]
    pairs = _align_rows(transcribed, printed)
    assert pairs == {0: 0, 2: 2}, "the extra row must not drag the alignment out of order"


def test_verification_empties_a_row_the_text_layer_does_not_contain():
    # On a page whose text layer we can read, a real table row has to be in there. In practice
    # these are the rows the model emitted out of order — the ones most likely to be carrying
    # another row's numbers, which is exactly why they must not be kept.
    out = _three_col_table(rows=[
        ["Uang Beredar (M2)", "9.387,9", "9.404,3", "9.595,3"],
        ["Baris Yang Tidak Ada Di Text Layer", "1,0", "2,0", "3,0"],
        ["Uang Kuasi", "4.060,8", "4.076,3", "4.123,0"],
    ])
    table = _as_pdf_table(out)
    _verify_against_text_layer([table], ["", "", "", "", "", "", _PAGE_7_TEXT])

    assert all(c is None for c in _body(table, "Baris Yang Tidak Ada Di Text Layer")[1:])
    assert _body(table, "Uang Beredar (M2)")[1:] == [9387.9, 9404.3, 9595.3]


def test_every_surviving_value_on_a_verified_page_comes_from_the_text_layer():
    # The invariant the whole pass exists to establish.
    out = _three_col_table(rows=[
        ["Uang Beredar (M2)", "1,0", "2,0", "3,0"],                  # all wrong
        ["Hantu", "9,9", "9,9", "9,9"],                              # not in the text layer
        ["Uang Kuasi", "4.060,8", "4.076,3", "4.123,0"],             # correct already
    ])
    table = _as_pdf_table(out)
    _verify_against_text_layer([table], ["", "", "", "", "", "", _PAGE_7_TEXT])

    from_text = {v for _, values in _text_layer_rows(_PAGE_7_TEXT) for v in values}
    survivors = [c for row in table.grid for c in row[1:] if isinstance(c, float)]
    assert survivors, "the pass must not empty everything"
    assert all(v in from_text for v in survivors)


def test_verification_keeps_repeated_sub_labels_in_printed_order():
    # A Lampiran repeats 'Rupiah'/'Valas' under different parents, so only the printed order
    # tells them apart — which is why rows are aligned as sequences, not looked up by label.
    text = (
        "Simpanan Berjangka 100,0 200,0 300,0\n"
        "Rupiah 10,0 20,0 30,0\n"
        "Valas 1,0 2,0 3,0\n"
        "Tabungan Lainnya 400,0 500,0 600,0\n"
        "Rupiah 40,0 50,0 60,0\n"
        "Valas 4,0 5,0 6,0\n"
    )
    out = _three_col_table(rows=[
        ["Simpanan Berjangka", "100,0", "200,0", "300,0"],
        ["Rupiah", "99,9", "99,9", "99,9"],
        ["Valas", "99,9", "99,9", "99,9"],
        ["Tabungan Lainnya", "400,0", "500,0", "600,0"],
        ["Rupiah", "99,9", "99,9", "99,9"],
        ["Valas", "99,9", "99,9", "99,9"],
    ])
    table = _as_pdf_table(out, page=1)
    _verify_against_text_layer([table], [text])

    rupiah = [r for r in table.grid if r[0] == "Rupiah"]
    valas = [r for r in table.grid if r[0] == "Valas"]
    assert rupiah[0][1:] == [10.0, 20.0, 30.0]
    assert rupiah[1][1:] == [40.0, 50.0, 60.0]
    assert valas[0][1:] == [1.0, 2.0, 3.0]
    assert valas[1][1:] == [4.0, 5.0, 6.0]


def test_verification_leaves_a_scanned_page_untouched_and_unverified():
    out = _three_col_table(rows=[
        ["Uang Beredar (M2)", "9.387,9", "9.404,3", "9.595,3"],
        ["Uang Kuasi", "4.060,8", "4.076,3", "4.123,0"],
    ])
    table = _as_pdf_table(out)
    _verify_against_text_layer([table], [""] * 10)   # no text layer on any page

    assert _body(table, "Uang Beredar (M2)")[1:] == [9387.9, 9404.3, 9595.3]
    assert table.verified is False, "a scanned page's numbers really are the model's"


def test_verification_aligns_across_two_tables_sharing_a_page():
    text = (
        "Alpha 1,0 2,0 3,0\n"
        "Beta 4,0 5,0 6,0\n"
        "Gamma 7,0 8,0 9,0\n"
        "Delta 10,0 11,0 12,0\n"
    )
    first = _as_pdf_table(_three_col_table(caption="Lampiran 4. Satu", rows=[
        ["Alpha", "0,0", "0,0", "0,0"], ["Beta", "0,0", "0,0", "0,0"],
    ]), page=9)
    second = _as_pdf_table(_three_col_table(caption="Lampiran 5. Dua", rows=[
        ["Gamma", "0,0", "0,0", "0,0"], ["Delta", "0,0", "0,0", "0,0"],
    ]), page=9)
    second.index_on_page = 1
    _verify_against_text_layer([first, second], [""] * 8 + [text])

    assert _body(first, "Alpha")[1:] == [1.0, 2.0, 3.0]
    assert _body(second, "Delta")[1:] == [10.0, 11.0, 12.0]


def test_extract_tables_marks_tables_verified_when_a_text_layer_exists():
    llm, _ = _gemini_llm(_PageTables(tables=[_three_col_table()]))
    with patch("pdf_table_extraction.render_pages_to_b64", return_value=["a"]), \
         patch("pdf_table_extraction._extract_pages_raw", return_value=[_PAGE_7_TEXT]):
        tables = asyncio.run(extract_tables_from_pdf(b"%PDF-fake", llm))

    assert tables[0].verified is True


def test_extract_tables_survives_an_unreadable_text_layer():
    llm, _ = _gemini_llm(_PageTables(tables=[_three_col_table()]))
    with patch("pdf_table_extraction.render_pages_to_b64", return_value=["a"]), \
         patch("pdf_table_extraction._extract_pages_raw", side_effect=RuntimeError("corrupt")):
        tables = asyncio.run(extract_tables_from_pdf(b"%PDF-fake", llm))

    assert len(tables) == 1
    assert tables[0].verified is False


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
