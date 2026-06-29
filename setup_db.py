"""
Creates and seeds the local SQLite database used by the fact-checking PoC.

Run this once (or any time you want to reset the data) with:
    python setup_db.py
"""

import sqlite3
from pathlib import Path

DB_FILENAME = "statistik_makro.db"

SCHEMA_SQL = """
CREATE TABLE indikator_ekonomi (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    tahun                       INTEGER NOT NULL,
    kuartal                     TEXT NOT NULL,
    inflasi_persen              REAL NOT NULL,
    suku_bunga_persen           REAL NOT NULL,
    penyaluran_kredit_triliun   REAL NOT NULL
);
"""

# Realistic, consecutive-quarter dummy data (Indonesian macroeconomic style figures).
DUMMY_ROWS = [
    # tahun, kuartal, inflasi_persen, suku_bunga_persen, penyaluran_kredit_triliun
    (2023, "Q1", 5.47, 5.75, 6750.5),
    (2023, "Q2", 3.52, 5.75, 6850.2),
    (2023, "Q3", 2.28, 6.00, 6975.8),
    (2023, "Q4", 2.61, 6.00, 7100.4),
    (2024, "Q1", 3.05, 6.00, 7250.9),
    (2024, "Q2", 2.84, 6.25, 7400.3),
]

# A second table, deliberately shaped a bit differently from `indikator_ekonomi`: different
# topic (trade balance, not inflation/rates/credit), monthly instead of quarterly granularity,
# and a derived column (`neraca_juta_usd` = ekspor - impor) instead of only independent values -
# a relational stand-in for the "aggregate cell" problem in the stylized Excel sources.
NERACA_SCHEMA_SQL = """
CREATE TABLE neraca_dagang (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    tahun               INTEGER NOT NULL,
    bulan               TEXT NOT NULL,
    ekspor_juta_usd     REAL NOT NULL,
    impor_juta_usd      REAL NOT NULL,
    neraca_juta_usd     REAL NOT NULL
);
"""

# tahun, bulan, ekspor_juta_usd, impor_juta_usd, neraca_juta_usd (neraca = ekspor - impor)
NERACA_DUMMY_ROWS = [
    (2024, "Januari", 20500.3, 18440.7, 2059.6),
    (2024, "Februari", 19250.8, 17800.2, 1450.6),
    (2024, "Maret", 21800.4, 19120.5, 2679.9),
    (2024, "April", 19670.1, 16650.0, 3020.1),
    (2024, "Mei", 21300.9, 18900.4, 2400.5),
    (2024, "Juni", 20750.6, 19300.8, 1449.8),
]


def main() -> None:
    db_path = Path(__file__).parent / DB_FILENAME

    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute("DROP TABLE IF EXISTS indikator_ekonomi")
        cursor.execute(SCHEMA_SQL)
        cursor.executemany(
            """
            INSERT INTO indikator_ekonomi
                (tahun, kuartal, inflasi_persen, suku_bunga_persen, penyaluran_kredit_triliun)
            VALUES (?, ?, ?, ?, ?)
            """,
            DUMMY_ROWS,
        )

        cursor.execute("DROP TABLE IF EXISTS neraca_dagang")
        cursor.execute(NERACA_SCHEMA_SQL)
        cursor.executemany(
            """
            INSERT INTO neraca_dagang
                (tahun, bulan, ekspor_juta_usd, impor_juta_usd, neraca_juta_usd)
            VALUES (?, ?, ?, ?, ?)
            """,
            NERACA_DUMMY_ROWS,
        )

        conn.commit()
        print(f"Database created at: {db_path.resolve()}")
        print(f"Inserted {len(DUMMY_ROWS)} rows into 'indikator_ekonomi'.")
        print(f"Inserted {len(NERACA_DUMMY_ROWS)} rows into 'neraca_dagang'.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
