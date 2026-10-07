"""Golden numbers: each publication parser reproduces the figures BI printed in its own release.

Fixtures live in tests/fixtures/publications/<publication>/<data period>/. The BI files (Excel,
PDF) are gitignored — only golden.json is committed — so an edition whose Excel is missing is
skipped. Run these locally before merging any parser change (see that folder's README.md).
"""
import json
from functools import lru_cache
from pathlib import Path

import pytest

from publication_parsers import parse_for_publication
from table_model import TableData

FIXTURES = Path(__file__).parent / "fixtures" / "publications"
_ORDER = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
          "Q1", "Q2", "Q3", "Q4"]


def _goldens():
    return [
        (path.parent, json.loads(path.read_text(encoding="utf-8")))
        for path in sorted(FIXTURES.glob("*/*/golden.json"))
    ]


def _sheet_cases():
    for folder, golden in _goldens():
        for workbook, sheets in golden["sheets"].items():
            for sheet, latest in sheets.items():
                yield pytest.param(
                    folder, golden["publication"], workbook, sheet, latest,
                    id=f"{golden['publication']}/{folder.name}/{sheet}",
                )


def _value_cases():
    for folder, golden in _goldens():
        for i, value in enumerate(golden["values"]):
            yield pytest.param(
                folder, golden["publication"], value,
                id=f"{golden['publication']}/{folder.name}/{i}-{value['sheet']}-{value['label']}",
            )


@lru_cache(maxsize=None)
def _parsed(folder: Path, publication: str, workbook: str, sheet: str) -> TableData:
    path = folder / workbook
    if not path.exists():
        pytest.skip(f"{path.name} tidak ada di lokal (file BI di-gitignore; lihat README folder ini)")
    table = parse_for_publication(publication, path.read_bytes(), sheet)
    assert table is not None, f"sheet '{sheet}' tidak dicakup parser {publication}"
    return table


def test_an_edition_without_its_excel_is_skipped_not_failed(tmp_path):
    with pytest.raises(pytest.skip.Exception):
        _parsed(tmp_path, "uang-beredar", "TABEL1_1.xls", "I.1")


@pytest.mark.parametrize("folder, publication, workbook, sheet, latest", list(_sheet_cases()))
def test_each_sheet_is_read_by_its_publication_parser(folder, publication, workbook, sheet, latest):
    table = _parsed(folder, publication, workbook, sheet)

    assert table.row_labels and table._data
    assert table.unit.strip()
    last = max(((y, p) for _, y, p in table._data), key=lambda yp: (yp[0], _ORDER.index(yp[1])))
    assert last == (latest["year"], latest["period"])


@pytest.mark.parametrize("folder, publication, value", list(_value_cases()))
def test_each_golden_figure_matches_the_release(folder, publication, value):
    table = _parsed(folder, publication, value["workbook"], value["sheet"])
    year, period = value["year"], value["period"]

    current = table.lookup(value["label"], year, period)
    assert current is not None, (
        f"tidak ada sel {value['label']!r} {period} {year}; label: {table.row_labels}"
    )
    if value["kind"] == "yoy":
        previous = table.lookup(value["label"], year - 1, period)
        assert previous, f"tidak ada sel {value['label']!r} {period} {year - 1}"
        got = (current / previous - 1) * 100
    else:
        got = current * value.get("scale", 1)
    assert abs(got - value["expected"]) <= 0.5 * 10 ** -value["decimals"] + 1e-9, (
        f"tabel {got:.4f} vs rilis {value['expected']} — {value['quote']}"
    )
    if value.get("query"):
        matched, _ = table.lookup_fuzzy(value["query"], year, period)
        assert matched == value["label"], f"{value['query']!r} mendarat di {matched!r}"
