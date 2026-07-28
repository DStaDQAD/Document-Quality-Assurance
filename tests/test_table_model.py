"""Tests for the categorical axis of TableData.

The temporal axis (lookup / lookup_fuzzy / available_periods) is exercised extensively via
tests/test_excel_parser_bi.py — BITableData is an alias of TableData — so this file focuses
on the categorical lookups and the axis guards.
"""

from table_model import TableData


def _make_categorical():
    table = TableData(
        title="Daftar Barang Elektronik",
        unit="",
        row_labels=["Laptop ASUS", "Laptop HP", "Mouse Logitech"],
        col_labels=["Harga", "Stok"],
        axis_type="categorical",
    )
    table._data = {
        ("Laptop ASUS", "Harga"): 7_500_000.0,
        ("Laptop HP", "Harga"): 6_200_000.0,
        ("Laptop HP", "Stok"): 12.0,
        ("Mouse Logitech", "Harga"): 250_000.0,
        ("Mouse Logitech", "Stok"): 45.0,
    }
    return table


def test_lookup_cell_returns_exact_value():
    table = _make_categorical()

    assert table.lookup_cell("Laptop ASUS", "Harga") == 7_500_000.0
    assert table.lookup_cell("Mouse Logitech", "Stok") == 45.0


def test_lookup_cell_returns_none_for_missing_row_or_column():
    table = _make_categorical()

    assert table.lookup_cell("Laptop ASUS", "Stok") is None  # row exists, cell empty
    assert table.lookup_cell("Keyboard", "Harga") is None


def test_lookup_cell_fuzzy_matches_both_axes_case_insensitively():
    table = _make_categorical()

    row, col, value = table.lookup_cell_fuzzy("laptop asus", "harga")

    assert row == "Laptop ASUS"
    assert col == "Harga"
    assert value == 7_500_000.0


def test_lookup_cell_fuzzy_matches_row_by_containment():
    table = _make_categorical()

    row, col, value = table.lookup_cell_fuzzy("mouse", "stok")

    assert row == "Mouse Logitech"
    assert col == "Stok"
    assert value == 45.0


def test_lookup_cell_fuzzy_skips_row_candidate_without_data_for_the_column():
    # 'laptop' matches both laptops; Laptop ASUS has no Stok value, so the lookup must
    # settle on the row that actually carries the requested attribute.
    table = _make_categorical()

    row, col, value = table.lookup_cell_fuzzy("laptop", "stok")

    assert row == "Laptop HP"
    assert value == 12.0


def test_lookup_cell_fuzzy_returns_none_triple_when_nothing_matches():
    table = _make_categorical()

    assert table.lookup_cell_fuzzy("Printer Canon", "Harga") == (None, None, None)
    assert table.lookup_cell_fuzzy("Laptop ASUS", "Garansi") == (None, None, None)


def test_available_periods_is_empty_for_categorical_tables():
    # Categorical keys are 2-tuples — there are no (year, month) periods to enumerate,
    # and the guard keeps the 3-tuple unpacking from crashing.
    table = _make_categorical()

    assert table.available_periods("Laptop ASUS") == []


# ---------------------------------------------------------------------------
# Cosmetic spacing, qualifier coverage, and match scoring (temporal axis)
# ---------------------------------------------------------------------------

from table_model import label_match_score


def _make_temporal(labels):
    table = TableData(title="Indeks", unit="", row_labels=list(labels))
    table._data = {(label, 2026, "Jun"): float(i + 1) for i, label in enumerate(labels)}
    return table


def test_lookup_fuzzy_ignores_spacing_around_punctuation():
    # The narrative writes 'Rp4,1-5 juta'; the sheet writes 'Rp4,1 - 5 juta'.
    table = _make_temporal([
        "Indeks Ekspektasi Kegiatan Usaha (IEKU) > Pengeluaran Rp4,1 - 5 juta",
    ])

    matched, value = table.lookup_fuzzy(
        "Indeks Ekspektasi Kegiatan Usaha (IEKU) > Pengeluaran Rp4,1-5 juta", 2026, "Jun"
    )

    assert matched == "Indeks Ekspektasi Kegiatan Usaha (IEKU) > Pengeluaran Rp4,1 - 5 juta"
    assert value == 1.0


def test_lookup_fuzzy_rejects_a_qualified_query_answered_by_the_bare_parent():
    # 'IKLK' IS contained in the query, but answering a claim about one education level
    # with the national aggregate compares the wrong series.
    table = _make_temporal(["Indeks Ketersediaan Lapangan Kerja (IKLK)"])

    assert table.lookup_fuzzy(
        "Indeks Ketersediaan Lapangan Kerja (IKLK) > tingkat pendidikan lainnya", 2026, "Jun"
    ) == (None, None)


def test_lookup_fuzzy_rejects_a_leaf_match_on_a_single_shared_word():
    # 'Lainnya' shares one word out of three with 'tingkat pendidikan lainnya'.
    table = _make_temporal(["Lainnya", "Tabungan/deposito"])

    assert table.lookup_fuzzy(
        "Indeks Ketersediaan Lapangan Kerja (IKLK) > tingkat pendidikan lainnya", 2026, "Jun"
    ) == (None, None)


def test_lookup_fuzzy_keeps_a_qualified_query_whose_leaf_survives():
    table = _make_temporal(["Indeks Ketersediaan Lapangan Kerja (IKLK) > Sarjana"])

    matched, _ = table.lookup_fuzzy(
        "Indeks Ketersediaan Lapangan Kerja (IKLK) > Sarjana", 2026, "Jun"
    )

    assert matched == "Indeks Ketersediaan Lapangan Kerja (IKLK) > Sarjana"


def test_label_match_score_is_one_for_an_exact_match():
    assert label_match_score("Indeks Keyakinan Konsumen (IKK)",
                             "Indeks Keyakinan Konsumen (IKK)") == 1.0


def test_label_match_score_penalises_a_dropped_qualifier():
    query = "Indeks Keyakinan Konsumen (IKK) > Pengeluaran >Rp5 juta"

    exact = label_match_score(query, "Indeks Keyakinan Konsumen (IKK) > Pengeluaran >Rp5 juta")
    coarse = label_match_score(query, "Indeks Keyakinan Konsumen (IKK)")

    assert exact == 1.0
    assert coarse < exact


def test_label_match_score_penalises_an_over_specific_label():
    query = "Indeks Kondisi Ekonomi Saat Ini (IKE)"

    national = label_match_score(query, "Indeks Kondisi Ekonomi Saat Ini (IKE)")
    one_city = label_match_score(query, "15. Mataram > Indeks Kondisi Ekonomi Saat Ini (IKE)")

    assert national > one_city
