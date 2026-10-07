"""Which sheets each publication parser claims, and how the reader wrappers report failure."""
from unittest.mock import patch

import pytest

from publication_parsers import PublicationParseError, covers, parse_for_publication
from table_model import TableData


@pytest.mark.parametrize("publication, sheet, expected", [
    ("uang-beredar", "I.1", True),
    ("uang-beredar", "I.1A", True),
    ("uang-beredar", " i.1a ", True),
    ("uang-beredar", "I.10", False),
    ("uang-beredar", "Th 1985-1992", False),
    ("uang-primer-m0", "I.2", True),
    ("uang-primer-m0", "Th 2010-2021", False),
    ("cadangan-devisa", "5.9", True),
    ("cadangan-devisa", "5.90", False),
    ("cadangan-devisa", "I.1", False),
    ("", "I.1", False),
    ("tidak-ada", "I.1", False),
])
def test_covers(publication, sheet, expected):
    assert covers(publication, sheet) is expected


def test_an_uncovered_sheet_is_left_to_the_generic_cascade():
    assert parse_for_publication("cadangan-devisa", b"never read", "I.1") is None


def test_seki_wrapper_reports_an_unreadable_file_as_a_publication_error():
    with pytest.raises(PublicationParseError, match="SEKI"):
        parse_for_publication("uang-beredar", b"not an excel file", "I.1")


@patch("publication_parsers._common.parse_bi_table")
def test_seki_wrapper_rejects_a_table_whose_hierarchy_collapsed(mock_bi):
    mock_bi.return_value = TableData(
        title="t", unit="Miliar Rp", row_labels=["Rupiah", "Rupiah"],
        _data={("Rupiah", 2026, "Jul"): 1.0},
    )

    with pytest.raises(PublicationParseError, match="berulang"):
        parse_for_publication("uang-beredar", b"bytes", "I.1")


@patch("publication_parsers._common.parse_bi_table")
def test_seki_wrapper_rejects_a_table_without_figures(mock_bi):
    mock_bi.return_value = TableData(title="t", unit="Miliar Rp", row_labels=["M2"])

    with pytest.raises(PublicationParseError, match="angka"):
        parse_for_publication("uang-primer-m0", b"bytes", "I.2")


@pytest.mark.parametrize("sheet, expected", [
    ("TabI.1", True), ("TabI.7", True), ("Tbl II.1", True), ("Tbl II.6", True),
    ("Tbl II.7", False), ("Tbl II.8", False),
    ("Tbl III.1", True), ("Tbl III.10", True), ("Tbl III.11", False),
])
def test_sulni_covers_only_its_time_series_sheets(sheet, expected):
    assert covers("sulni", sheet) is expected


@pytest.mark.parametrize("publication, sheet, expected", [
    ("npi", "5.1", True), ("npi", "Th 2004-2010", False), ("npi", "I.1", False),
    ("pii", "5.39", True), ("pii", "2001-2012", False), ("pii", "5.3", False),
])
def test_npi_and_pii_cover_their_seki_sheets(publication, sheet, expected):
    assert covers(publication, sheet) is expected


@pytest.mark.parametrize("publication, sheet, expected", [
    ("sk", "Tabel 1", True), ("sk", "Tabel 9", True), ("sk", "Tabel 10", False),
    ("skdu", "T1 Kegiatan Usaha", True), ("skdu", "T7b Investasi Semesteran", True),
    ("skdu", "T10 Margin Usaha", True), ("skdu", "Tabel 1", False),
])
def test_sk_and_skdu_cover_their_survey_sheets(publication, sheet, expected):
    assert covers(publication, sheet) is expected


@patch("publication_parsers._common.parse_generic_table")
def test_survey_wrapper_keeps_the_indonesian_half_of_the_unit(mock_generic):
    mock_generic.return_value = TableData(
        title="t", unit="Rp)  / (IDR", row_labels=["Mandor"], _data={("Mandor", 2026, "Q1"): 1.0},
    )

    assert parse_for_publication("skdu", b"bytes", "T9 Upah Rata-rata").unit == "Rp"


@patch("publication_parsers._common.parse_generic_table")
def test_survey_wrapper_fills_in_the_unit_a_consumer_survey_sheet_leaves_out(mock_generic):
    mock_generic.side_effect = lambda data, sheet: TableData(
        title="t", unit="", row_labels=["IKK"], _data={("IKK", 2026, "Aug"): 120.0},
    )

    assert parse_for_publication("sk", b"bytes", "Tabel 1").unit == "Indeks"
    assert parse_for_publication("sk", b"bytes", "Tabel 5").unit == "%"


@patch("publication_parsers._common.parse_generic_table")
def test_survey_wrapper_rejects_a_table_that_is_not_a_time_series(mock_generic):
    mock_generic.return_value = TableData(
        title="t", unit="", row_labels=["IKK"], axis_type="categorical",
        _data={("IKK", "Nilai"): 1.0},
    )

    with pytest.raises(PublicationParseError, match="deret waktu"):
        parse_for_publication("sk", b"bytes", "Tabel 1")
