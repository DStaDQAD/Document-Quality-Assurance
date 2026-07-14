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
