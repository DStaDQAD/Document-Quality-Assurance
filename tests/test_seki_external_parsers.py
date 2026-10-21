"""The rows the PII parser derives from SEKI V.39: net positions and external debt; and the
Cadangan Devisa parser's name for V.9's total."""
from unittest import mock

import pytest

from publication_parsers import cadangan_devisa, pii
from table_model import TableData


def test_the_reserves_total_is_called_what_the_release_calls_it():
    # "posisi cadangan devisa … 146,5" found 'Cadangan Devisa Lainnya' (125,4) before 'Total'.
    sheet = TableData(title="V.9. Posisi Cadangan Devisa", unit="Juta USD",
                      row_labels=["Cadangan Devisa Lainnya", "Total"])
    sheet._data = {("Cadangan Devisa Lainnya", 2026, "Aug"): 125377.24,
                   ("Total", 2026, "Aug"): 146507.61}
    with mock.patch.object(cadangan_devisa, "parse_with_bi_reader", return_value=sheet):
        table = cadangan_devisa.parse(b"", "5.9")

    assert table.lookup_fuzzy("Cadangan Devisa", 2026, "Aug") == ("Cadangan Devisa", 146507.61)
    assert table.lookup_fuzzy("cadangan devisa lainnya", 2026, "Aug")[1] == 125377.24

_ROWS = {
    "Aset > Investasi Portofolio": 43485.39,
    "Kewajiban > Investasi Portofolio": 273103.82,
    "Kewajiban > Investasi Langsung > Instrumen Utang": 47148.36,
    "Kewajiban > Investasi Portofolio > Surat Utang": 209618.08,
    "Kewajiban > Investasi Lainnya": 196602.10,
    "Ke luar negeri > Instrumen utang": 51264.29,
}


def _table():
    table = TableData(title="V.39. Posisi Investasi Internasional Indonesia", unit="Juta USD",
                      row_labels=list(_ROWS))
    for label, value in _ROWS.items():
        table._data[(label, 2026, "Q2")] = value
    return table


def test_a_component_with_both_sides_gets_a_hidden_net_row():
    table = _table()
    pii._add_net_rows(table)

    row = table.net_rows["investasi portofolio"]
    assert table._data[(row, 2026, "Q2")] == pytest.approx(43485.39 - 273103.82)
    assert row not in table.row_labels
    assert "investasi langsung" not in table.net_rows  # one side only


def test_external_debt_is_the_liabilities_debt_instruments_and_the_report_finds_it():
    # "posisi total utang luar negeri (ULN) Indonesia (posisi KFLN dalam instrumen utang) …
    # 453,4": its 'instrumen utang' reached the memorandum's 'Ke luar negeri > Instrumen utang'.
    table = _table()
    assert pii._add_sum_row(table, pii.ULN_ROW, pii.ULN_PARTS)
    table.term_aliases = dict(pii.TERM_ALIASES)

    label, value = table.lookup_fuzzy(
        "total utang luar negeri (ULN) Indonesia (posisi KFLN dalam instrumen utang)", 2026, "Q2")

    assert label == pii.ULN_ROW
    assert value == pytest.approx(453368.54)


def test_npi_reads_still_in_surplus_as_a_level():
    # NPI Tw I-2026: "Investasi langsung tetap mencatatkan surplus" came back as a rise on
    # 3,15 -> 2,02 — still a surplus, a smaller one.
    import re
    from publication_parsers import npi

    sentence = "Investasi langsung tetap mencatatkan surplus sebagai cerminan dari persepsi positif investor"
    matched = [side for pattern, side in npi.LEVEL_PHRASES.items() if re.search(pattern, sentence, re.I)]
    assert matched == [("above", 0.0)]
    assert "transaksi finansial" in npi.LAYOUT_LEVELS
