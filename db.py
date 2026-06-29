"""Read-only access to the local statistical indicators SQLite database."""

import ast
import os
import sqlite3
from pathlib import Path
from typing import List, Tuple

from langchain_community.utilities import SQLDatabase

DB_FILENAME = os.getenv("DATABASE_PATH", "statistik_makro.db")
DB_PATH = Path(__file__).parent / DB_FILENAME


def get_readonly_db() -> SQLDatabase:
    """Open the SQLite database strictly in READ-ONLY mode via a `file:` URI."""
    if not DB_PATH.exists():
        raise FileNotFoundError(
            f"Database file not found at '{DB_PATH}'. Run `python setup_db.py` first."
        )
    # `mode=ro` enforces read-only access at the SQLite engine level (writes raise
    # "attempt to write a readonly database"), regardless of what the agent attempts.
    read_only_uri = f"sqlite:///file:{DB_PATH.as_posix()}?mode=ro&uri=true"
    return SQLDatabase.from_uri(read_only_uri)


def _get_readonly_connection() -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise FileNotFoundError(
            f"Database file not found at '{DB_PATH}'. Run `python setup_db.py` first."
        )
    return sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True)


def list_tables() -> List[str]:
    """List user-created tables (e.g. `indikator_ekonomi`, `excel_facts`), newest source included."""
    conn = _get_readonly_connection()
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        return [row[0] for row in rows]
    finally:
        conn.close()


def fetch_table_rows(table_name: str, limit: int = 200, offset: int = 0) -> Tuple[List[str], List[tuple], int]:
    """Return (column_names, rows, total_row_count) for one table.

    `table_name` is interpolated directly into the SQL since SQLite doesn't support parameterized
    identifiers - the caller MUST validate it against `list_tables()` first to avoid SQL injection.
    """
    conn = _get_readonly_connection()
    try:
        cursor = conn.execute(f'SELECT * FROM "{table_name}" LIMIT ? OFFSET ?', (limit, offset))
        columns = [d[0] for d in cursor.description]
        rows = cursor.fetchall()
        total_rows = conn.execute(f'SELECT COUNT(*) FROM "{table_name}"').fetchone()[0]
        return columns, rows, total_rows
    finally:
        conn.close()


def get_distinct_value_hints(db: SQLDatabase, max_distinct: int = 30) -> str:
    """List the exact distinct values of every TEXT column small enough to fit (<= max_distinct),
    plus which values of those columns actually co-occur in the same row.

    `db.get_table_info()` only samples 3 rows per table, so a Text-to-SQL call can't see every
    valid value of a free-text key column (e.g. `excel_facts.row_label`/`source_file`) - it ends up
    guessing a plausible-looking string from the claim's wording instead of the real one. Listing
    the full distinct set for columns small enough to fit closes that gap, without hardcoding any
    table or column name - high-cardinality columns are simply skipped, leaving today's behavior
    (the model infers from the schema/claim alone) as the fallback for those.

    Per-column lists alone are independent of each other, though, so a claim that names a value for
    one column (e.g. `row_label = 'Produk A'`) gives no signal about which value of another column
    (e.g. `source_file`) actually belongs with it - the model can silently combine values that never
    co-occur in any real row (e.g. pairing 'Produk A' with the wrong source file), producing a query
    that runs fine but matches zero rows. Once 2+ columns in a table qualify individually, this also
    lists their actual joint distinct combinations (capped wider than `max_distinct`, since
    combinations naturally outnumber single-column values) so the model can look up the real pairing
    instead of guessing one.
    """
    lines = []
    try:
        tables = list(db.get_usable_table_names())
    except Exception:
        return ""
    for table in tables:
        try:
            columns_info = ast.literal_eval(db.run(f"PRAGMA table_info({table})"))
        except Exception:
            continue
        text_columns = [row[1] for row in columns_info if "TEXT" in (row[2] or "").upper()]
        qualifying_columns = []
        for column in text_columns:
            try:
                count = ast.literal_eval(db.run(f'SELECT COUNT(DISTINCT "{column}") FROM "{table}"'))[0][0]
                if not (0 < count <= max_distinct):
                    continue
                values = [row[0] for row in ast.literal_eval(db.run(f'SELECT DISTINCT "{column}" FROM "{table}"'))]
            except Exception:
                continue
            lines.append(f"{table}.{column}: {values}")
            qualifying_columns.append(column)

        if len(qualifying_columns) >= 2:
            columns_sql = ", ".join(f'"{c}"' for c in qualifying_columns)
            try:
                combos = ast.literal_eval(db.run(f'SELECT DISTINCT {columns_sql} FROM "{table}"'))
                if 0 < len(combos) <= max_distinct * 2:
                    lines.append(
                        f"{table} ({', '.join(qualifying_columns)}) value combinations that "
                        f"actually occur together (use one whole tuple verbatim - never mix "
                        f"values from different tuples): {combos}"
                    )
            except Exception:
                pass
    return "\n".join(lines)
