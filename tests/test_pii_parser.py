"""The rows the PII parser derives from SEKI V.39: net positions and external debt."""
import pytest

from publication_parsers import pii
from table_model import TableData

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
