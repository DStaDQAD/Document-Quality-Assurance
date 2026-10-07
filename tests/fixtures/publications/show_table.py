"""Print what a publication parser reads from one sheet — for writing golden.json by hand.

    .venv/Scripts/python tests/fixtures/publications/show_table.py sulni \
        "tests/fixtures/publications/sulni/2026-07/TABEL_INDONESIA Sep26_value.xlsx" TabI.1 2026 Jul
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from publication_parsers import parse_for_publication  # noqa: E402

_ORDER = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
          "Q1", "Q2", "Q3", "Q4"]

publication, path, sheet, year, period = sys.argv[1:6]
table = parse_for_publication(publication, Path(path).read_bytes(), sheet)
if table is None:
    sys.exit(f"sheet {sheet!r} tidak dicakup parser {publication}")
latest = max(((y, p) for _, y, p in table._data), key=lambda yp: (yp[0], _ORDER.index(yp[1])))
print(f"{table.title} | satuan: {table.unit} | {len(table.row_labels)} baris | terakhir: {latest}")
year = int(year)
for label in table.row_labels:
    now, before = table.lookup(label, year, period), table.lookup(label, year - 1, period)
    yoy = f"{(now / before - 1) * 100:8.2f}" if now is not None and before else "       -"
    print(f"{now!s:>20}  yoy {yoy}  {label}")
