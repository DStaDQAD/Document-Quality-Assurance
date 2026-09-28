"""Appendix tables printed as pictures, and the survey-table shapes they carry.

SK-Juni-2026 (Survei Konsumen) embeds each appendix table as one image. Three things had to
change before that appendix could be a source at all, and each is pinned here:
  - section BANDS ("A. Indeks Keyakinan Konsumen (IKK)") name the rows repeated under them;
  - the trailing "Perubahan (Jun-Mei)" column is dropped, and used first as a row checksum;
  - a tall picture is read in strips at its own resolution, each strip against the header the
    first one read.
"""

import asyncio
import base64
import io
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from PIL import Image, ImageDraw

from pdf_table_extraction import (
    _cut_rows,
    _drop_off_width_rows,
    _drop_summary_columns,
    _merge_one_table,
    _PageTables,
    _PdfTableOut,
    _qualify_under_section_bands,
    _StripRows,
    _TableImage,
    _assemble_grid,
    _table_images,
    _transcribe_table_image,
)
from table_parser_generic import parse_generic_grid

_SK = Path(__file__).resolve().parent.parent / "sample_data" / "SK-Juni-2026.pdf"


def _survey_rows():
    # Tabel 2's shape: the same expenditure groups repeated under every index band.
    return [
        ["A. Indeks Keyakinan Konsumen (IKK)", "", "", ""],
        ["- Pengeluaran Rp1 - 2 juta", "114,0", "113,0", "108,9"],
        ["- Pengeluaran >Rp5 juta", "128,2", "124,6", "121,4"],
        ["B. Indeks Kondisi Ekonomi (IKE)", "", "", ""],
        ["- Pengeluaran Rp1 - 2 juta", "106,2", "104,8", "101,3"],
        ["- Pengeluaran >Rp5 juta", "123,5", "116,0", "113,1"],
    ]


def _survey_table(rows=None, header=None):
    return _PdfTableOut(
        caption="Tabel 2 Indeks Keyakinan Konsumen per Kelompok Pengeluaran Responden",
        unit=None,
        header_rows=header or [["", "2026", "", ""], ["KETERANGAN", "Apr", "Mei", "Juni"]],
        rows=rows or _survey_rows(),
    )


# ---------------------------------------------------------------------------
# Section bands
# ---------------------------------------------------------------------------

def test_a_repeated_row_is_named_after_the_band_above_it():
    rows = _qualify_under_section_bands(_survey_rows())

    assert [r[0] for r in rows] == [
        "Indeks Keyakinan Konsumen (IKK) > Pengeluaran Rp1 - 2 juta",
        "Indeks Keyakinan Konsumen (IKK) > Pengeluaran >Rp5 juta",
        "Indeks Kondisi Ekonomi (IKE) > Pengeluaran Rp1 - 2 juta",
        "Indeks Kondisi Ekonomi (IKE) > Pengeluaran >Rp5 juta",
    ]
    assert rows[0][1:] == ["114,0", "113,0", "108,9"]


def test_a_unique_row_under_a_band_keeps_its_own_name():
    # Tabel 1 prints IKK, IKE and IEK under the band "A. Indeks Keyakinan Konsumen (IKK)".
    # 'IKK > IKE' would make the headline IKE row read like a breakdown of IKK.
    rows = _qualify_under_section_bands([
        ["A. Indeks Keyakinan Konsumen (IKK)", "", ""],
        ["- Indeks Keyakinan Konsumen (IKK)", "120,9", "117,8"],
        ["- Indeks Kondisi Ekonomi Saat Ini (IKE)", "112,2", "109,2"],
    ])
    assert [r[0] for r in rows] == [
        "Indeks Keyakinan Konsumen (IKK)", "Indeks Kondisi Ekonomi Saat Ini (IKE)",
    ]


def test_a_table_without_bands_comes_back_untouched():
    rows = [["Giro", "1,0", "2,0"], ["Giro", "3,0", "4,0"]]
    assert _qualify_under_section_bands(rows) is rows


def test_a_banded_table_parses_to_the_workbook_row_names():
    grid = _assemble_grid(_survey_table())
    table = parse_generic_grid(grid)

    assert table.lookup(
        "Indeks Keyakinan Konsumen (IKK) > Pengeluaran >Rp5 juta", 2026, "Jun") == 121.4
    assert table.lookup(
        "Indeks Kondisi Ekonomi (IKE) > Pengeluaran Rp1 - 2 juta", 2026, "May") == 104.8


def test_the_m2_giro_rows_are_still_dropped_rather_than_guessed():
    # Rupiah and Valas carry values of their own, so they are not bands: the three 'Giro' rows
    # stay unresolvable and are dropped, exactly as before.
    grid = _assemble_grid(_PdfTableOut(
        caption="Lampiran 3", unit="(Triliun Rp)",
        header_rows=[["", "2026", "2026"], ["", "Mar", "Apr"]],
        rows=[["Rupiah", "1,0", "2,0"], ["Giro", "3,0", "4,0"],
              ["Valas", "5,0", "6,0"], ["Giro", "7,0", "8,0"]],
    ))
    labels = [r[0] for r in grid if isinstance(r[0], str)]
    assert "Giro" not in labels and "Rupiah" in labels


# ---------------------------------------------------------------------------
# The trailing change column
# ---------------------------------------------------------------------------

def _with_change(rows):
    return _survey_table(
        rows=rows,
        header=[["", "2026", "", "", "Perubahan"], ["KETERANGAN", "Apr", "Mei", "Juni", "(Jun-Mei)"]],
    )


def test_the_change_column_is_trimmed_off_header_and_rows():
    table = _drop_summary_columns(_with_change([
        ["- Indeks Keyakinan Konsumen (IKK)", "123,0", "120,9", "117,8", "-3,1"],
        ["- Indeks Kondisi Ekonomi Saat Ini (IKE)", "116,5", "112,2", "109,2", "-3,0"],
    ]))
    assert table.header_rows[-1] == ["KETERANGAN", "Apr", "Mei", "Juni"]
    assert table.rows[0] == ["- Indeks Keyakinan Konsumen (IKK)", "123,0", "120,9", "117,8"]


def test_a_row_whose_change_contradicts_its_last_two_months_is_dropped():
    # Read one cell shifted: the months are May's and April's values, so Jun - Mei no longer
    # equals the printed change.
    table = _drop_summary_columns(_with_change([
        ["- Indeks Keyakinan Konsumen (IKK)", "123,0", "120,9", "117,8", "-3,1"],
        ["- Indeks Ekspektasi Konsumen (IEK)", "131,7", "133,5", "129,6", "-4,7"],
    ]))
    assert [r[0] for r in table.rows] == ["- Indeks Keyakinan Konsumen (IKK)"]


def test_rounding_in_the_printed_change_is_tolerated():
    # 109,2 - 112,2 = -3,0 as printed, but the unrounded change can print as -3,1.
    table = _drop_summary_columns(_with_change([
        ["- Indeks Kondisi Ekonomi Saat Ini (IKE)", "116,5", "112,2", "109,2", "-3,1"],
    ]))
    assert len(table.rows) == 1


def test_snippet_growth_columns_are_not_mistaken_for_a_summary():
    # "Mar'26" is a period token: the M2 snippet tables' yoy block must survive untouched.
    table = _PdfTableOut(
        caption="Tabel 1", unit="(Triliun Rp)",
        header_rows=[["", "Mar", "Apr*", "Mar'26", "Apr'26*"]],
        rows=[["M2", "9.900,0", "10.000,0", "8,7", "9,7"], ["M1", "1,0", "2,0", "3,0", "4,0"]],
    )
    assert _drop_summary_columns(table) is table


# ---------------------------------------------------------------------------
# Pictures and strips
# ---------------------------------------------------------------------------

def _striped_image(rows=60, row_px=20, width=400):
    image = Image.new("RGB", (width, rows * row_px), "white")
    draw = ImageDraw.Draw(image)
    for r in range(rows):
        top = r * row_px + 4
        draw.rectangle([10, top, width - 10, top + row_px - 8], fill="black")
    return image


def test_a_short_picture_is_one_strip():
    assert len(_cut_rows(_striped_image(rows=10), strip_px=450)) == 1


def test_a_tall_picture_is_cut_between_rows_not_through_them():
    image = _striped_image(rows=60, row_px=20)          # 1200 px tall
    strips = _cut_rows(image, strip_px=450)

    assert len(strips) >= 2
    assert sum(s.height for s in strips) == image.height
    # Every cut lands in a gap between two rows (the 8 px of white under each band of ink).
    y = 0
    for strip in strips[:-1]:
        y += strip.height
        assert y % 20 >= 16 or y % 20 < 4


def test_bands_split_off_as_separate_tables_are_put_back_as_band_rows():
    merged = _merge_one_table([
        _PdfTableOut(caption="A. Indeks Keyakinan Konsumen (IKK)", header_rows=[["", "Jun"]],
                     rows=[["Rp1 - 2 juta", "108,9"]]),
        _PdfTableOut(caption="B. Indeks Kondisi Ekonomi (IKE)", header_rows=[["", "Jun"]],
                     rows=[["Rp1 - 2 juta", "101,3"]]),
    ])
    assert [r[0] for r in merged.rows] == [
        "A. Indeks Keyakinan Konsumen (IKK)", "Rp1 - 2 juta",
        "B. Indeks Kondisi Ekonomi (IKE)", "Rp1 - 2 juta",
    ]


def test_a_row_that_is_not_as_wide_as_the_others_is_dropped():
    table = _survey_table(rows=_survey_rows() + [["- Pengeluaran Rp2,1 - 3 juta", "1,0", "2,0", "3,0", "4,0"]])
    kept = _drop_off_width_rows(table, page_number=9)
    assert len(kept.rows) == len(_survey_rows())         # bands kept, the 5-cell row gone


def _png():
    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), "white").save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _picture_llm(head, strips):
    """A vision model whose _PageTables channel returns `head` and _StripRows channel `strips`."""
    head_channel, strip_channel = Mock(), Mock()
    head_channel.ainvoke = AsyncMock(return_value=head)
    strip_channel.ainvoke = AsyncMock(side_effect=strips)
    llm = Mock()
    llm.with_structured_output = Mock(
        side_effect=lambda schema: head_channel if schema is _PageTables else strip_channel)
    return llm, strip_channel


def test_a_picture_is_read_strip_by_strip_against_the_first_strip_header():
    rows = _survey_rows()
    head = _PageTables(tables=[_survey_table(rows=rows[:3])])
    llm, strip_channel = _picture_llm(head, [_StripRows(rows=rows[3:])])
    picture = _TableImage(strips=[_png(), _png()], caption="Tabel 2 Indeks Keyakinan Konsumen")

    tables = asyncio.run(_transcribe_table_image(
        9, picture, llm, asyncio.Semaphore(2), max_retries=1, number_format="id"))

    assert len(tables) == 1
    assert tables[0].caption == "Tabel 2 Indeks Keyakinan Konsumen"
    table = parse_generic_grid(tables[0].grid)
    # The strip's rows were filed under the band the strip itself carried.
    assert table.lookup("Indeks Kondisi Ekonomi (IKE) > Pengeluaran >Rp5 juta", 2026, "Jun") == 113.1
    # The strip was told which columns it is reading.
    prompt = strip_channel.ainvoke.call_args.args[0][0].content[1]["text"]
    assert "KETERANGAN | Apr | Mei | Juni" in prompt


def test_the_rows_below_a_lost_strip_are_not_kept():
    rows = _survey_rows()
    head = _PageTables(tables=[_survey_table(rows=rows[:3])])
    llm, _ = _picture_llm(head, [RuntimeError("boom"), _StripRows(rows=rows[3:])])
    picture = _TableImage(strips=[_png(), _png(), _png()], caption="Tabel 2")

    tables = asyncio.run(_transcribe_table_image(
        9, picture, llm, asyncio.Semaphore(1), max_retries=1, number_format="id"))

    labels = [r[0] for r in tables[0].grid if isinstance(r[0], str)]
    assert not any("IKE" in label for label in labels)


@pytest.mark.skipif(not _SK.exists(), reason="SK-Juni-2026 sample not present")
def test_the_survey_appendix_pages_are_found_as_table_pictures():
    pictures = _table_images(_SK.read_bytes(), set(range(14)))

    # Pages 8-14 are the appendix; the chart pages and the back cover are not pictures of
    # tables (their only images are page art or chart markers).
    assert sorted(i + 1 for i in pictures) == [8, 9, 10, 11, 12, 13, 14]
    assert pictures[8].caption.startswith("Tabel 2")
    assert len(pictures[8].strips) >= 2          # 60 rows: read in strips
    assert len(pictures[7].strips) == 1          # Tabel 1, 12 rows: read whole
