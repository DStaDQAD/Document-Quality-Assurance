"""A level the extractor names by its abbreviation but with words missing.

'Indeks Kondisi Ekonomi (IKE) > Mataram' for the row 'Mataram > Indeks Kondisi Ekonomi Saat Ini
(IKE)': the abbreviation identifies the index, and every word the label does give belongs to it.
Synthetic rows, not BI series.
"""

from excel_parser_bi import BITableData

ROWS = ["1. Kota Utara > Indeks Omzet Saat Ini (IOS)", "1. Kota Utara > Indeks Ekspektasi Omzet (IEO)",
        "2. Kota Selatan > Indeks Omzet Saat Ini (IOS)", "2. Kota Selatan > Indeks Ekspektasi Omzet (IEO)"]


def _table():
    table = BITableData(title="Indeks per kota", unit="Indeks", row_labels=list(ROWS))
    for i, label in enumerate(ROWS):
        table._data[(label, 2026, "Sep")] = 100.0 + i
    return table


def test_an_abbreviated_level_missing_words_finds_its_row():
    assert _table().lookup_fuzzy("Indeks Omzet (IOS) > Kota Selatan", 2026, "Sep")[0] == ROWS[2]


def test_the_abbreviation_does_not_override_words_of_another_index():
    """'Indeks Ekspektasi … (IOS)' carries a word the IOS name does not have: the label is
    contradictory, and it is not silently read as IOS."""
    label, _ = _table().lookup_fuzzy("Indeks Ekspektasi Penjualan (IOS) > Kota Selatan", 2026, "Sep")
    assert label != ROWS[2]
