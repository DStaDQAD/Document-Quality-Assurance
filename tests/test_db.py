import sqlite3
from unittest.mock import Mock

from langchain_community.utilities import SQLDatabase

import db
from db import get_distinct_value_hints


def _sqlite_db(tmp_path, rows):
    db_path = tmp_path / "hints.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE t (label TEXT NOT NULL, value REAL NOT NULL)")
        conn.executemany("INSERT INTO t (label, value) VALUES (?, ?)", rows)
        conn.commit()
    finally:
        conn.close()
    return SQLDatabase.from_uri(f"sqlite:///{db_path}")


def test_get_distinct_value_hints_lists_small_text_columns(tmp_path):
    db = _sqlite_db(tmp_path, [("Surabaya", 1.0), ("Jakarta", 2.0), ("Surabaya", 3.0)])

    hints = get_distinct_value_hints(db)

    assert "t.label" in hints
    assert "Surabaya" in hints
    assert "Jakarta" in hints


def test_get_distinct_value_hints_skips_high_cardinality_columns(tmp_path):
    db = _sqlite_db(tmp_path, [(f"label_{i}", float(i)) for i in range(50)])

    hints = get_distinct_value_hints(db, max_distinct=30)

    assert hints == ""


def test_get_distinct_value_hints_returns_empty_string_when_db_is_unusable():
    broken_db = Mock()
    broken_db.get_usable_table_names = Mock(side_effect=Exception("boom"))

    assert get_distinct_value_hints(broken_db) == ""


def _excel_facts_db(tmp_path):
    db_path = tmp_path / "facts.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE excel_facts (source_file TEXT, sheet TEXT, row_label TEXT, value REAL)"
        )
        conn.executemany(
            "INSERT INTO excel_facts VALUES (?, ?, ?, ?)",
            [
                ("penjualan_cabang.xlsx", "Cabang", "Jakarta", 120.0),
                ("penjualan_cabang.xlsx", "Cabang", "Surabaya", 95.0),
                ("penjualan_2024.xlsx", "Penjualan", "Produk A", 150.0),
                ("penjualan_2024.xlsx", "Penjualan", "Produk B", 90.0),
            ],
        )
        conn.commit()
    finally:
        conn.close()
    return SQLDatabase.from_uri(f"sqlite:///{db_path}")


def test_get_distinct_value_hints_lists_combinations_that_actually_co_occur(tmp_path):
    db = _excel_facts_db(tmp_path)

    hints = get_distinct_value_hints(db)

    assert "value combinations that actually occur together" in hints
    assert "('penjualan_2024.xlsx', 'Penjualan', 'Produk A')" in hints
    assert "('penjualan_cabang.xlsx', 'Cabang', 'Produk A')" not in hints


def _make_browse_db(tmp_path):
    db_path = tmp_path / "browse.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE excel_facts (row_label TEXT, value REAL)")
        conn.executemany(
            "INSERT INTO excel_facts VALUES (?, ?)",
            [("Surabaya", 120.0), ("Jakarta", 95.0), ("Bandung", 80.0)],
        )
        conn.execute("CREATE TABLE indikator_ekonomi (tahun INTEGER)")
        conn.commit()
    finally:
        conn.close()
    return db_path


def test_list_tables_returns_user_tables_sorted(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", _make_browse_db(tmp_path))

    assert db.list_tables() == ["excel_facts", "indikator_ekonomi"]


def test_fetch_table_rows_paginates_and_returns_total_count(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", _make_browse_db(tmp_path))

    columns, rows, total_rows = db.fetch_table_rows("excel_facts", limit=2, offset=1)

    assert columns == ["row_label", "value"]
    assert rows == [("Jakarta", 95.0), ("Bandung", 80.0)]
    assert total_rows == 3
