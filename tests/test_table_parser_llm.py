import io
from unittest.mock import Mock

import pytest
from langchain_core.runnables import RunnableLambda
from openpyxl import Workbook

from table_parser_generic import _load_grid
from table_parser_llm import (
    _SPEC_CACHE,
    _ColumnSpec,
    _TableSpec,
    _grid_snapshot,
    _validate_spec,
    parse_grid_with_llm,
    parse_table_with_llm,
)


@pytest.fixture(autouse=True)
def _clear_spec_cache():
    _SPEC_CACHE.clear()
    yield
    _SPEC_CACHE.clear()


def _save(wb) -> bytes:
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _llm_returning(spec, call_log=None):
    """Fake llm whose structured-output chain returns `spec` and records each call."""
    def _respond(_prompt_value):
        if call_log is not None:
            call_log.append(1)
        if isinstance(spec, Exception):
            raise spec
        return spec

    llm = Mock()
    llm.with_structured_output = Mock(return_value=RunnableLambda(_respond))
    return llm


# ---------------------------------------------------------------------------
# Fixtures: a messy categorical sheet (gap column, code column before the name column)
# that the heuristic tiers would misread — exactly tier 3's target.
# ---------------------------------------------------------------------------

def _messy_inventory_bytes():
    wb = Workbook()
    ws = wb.active
    ws.title = "Gudang"
    ws.append(["Inventaris Gudang"])
    ws.append([])
    ws.append(["Kode", "Barang", None, "Harga", "Stok"])
    ws.append(["B1", "Laptop ASUS", None, 7_500_000, 10])
    ws.append(["B2", "Mouse Logitech", None, 250_000, 45])
    return _save(wb)


def _inventory_spec():
    return _TableSpec(
        header_row=2,
        label_col=1,
        data_start_row=3,
        axis_type="categorical",
        title="Inventaris Gudang",
        unit=None,
        columns=[
            _ColumnSpec(col=3, kind="attribute", name="Harga"),
            _ColumnSpec(col=4, kind="attribute", name="Stok"),
        ],
    )


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------

def test_parse_table_with_llm_extracts_values_from_spec_coordinates():
    llm = _llm_returning(_inventory_spec())

    table = parse_table_with_llm(_messy_inventory_bytes(), "Gudang", llm)

    assert table.axis_type == "categorical"
    assert table.title == "Inventaris Gudang"
    # Rows keyed by the spec's label_col (the NAME column, not the code column).
    assert table.row_labels == ["Laptop ASUS", "Mouse Logitech"]
    assert table.lookup_cell("Laptop ASUS", "Harga") == 7_500_000.0
    assert table.lookup_cell("Mouse Logitech", "Stok") == 45.0


def test_parse_table_with_llm_supports_temporal_specs():
    wb = Workbook()
    ws = wb.active
    ws.title = "S"
    ws.append(["Metrik", "kolom-apr", "kolom-mei"])  # opaque headers only the LLM can map
    ws.append(["M2", 100.0, 110.0])
    spec = _TableSpec(
        header_row=0, label_col=0, data_start_row=1, axis_type="temporal",
        title=None, unit="Miliar Rp",
        columns=[
            _ColumnSpec(col=1, kind="period", year=2026, month="Apr"),
            _ColumnSpec(col=2, kind="period", year=2026, month="May"),
        ],
    )

    table = parse_table_with_llm(_save(wb), "S", _llm_returning(spec))

    assert table.axis_type == "temporal"
    assert table.unit == "Miliar Rp"
    assert table.lookup("M2", 2026, "Apr") == 100.0
    assert table.lookup("M2", 2026, "May") == 110.0


def test_parse_table_with_llm_caches_validated_spec_per_layout():
    call_log = []
    llm = _llm_returning(_inventory_spec(), call_log)
    data = _messy_inventory_bytes()

    first = parse_table_with_llm(data, "Gudang", llm)
    second = parse_table_with_llm(data, "Gudang", llm)

    assert len(call_log) == 1  # second parse served from the layout-fingerprint cache
    assert second.lookup_cell("Laptop ASUS", "Harga") == first.lookup_cell("Laptop ASUS", "Harga")


# ---------------------------------------------------------------------------
# Failure paths — a bad spec must fail validation, never mis-extract silently
# ---------------------------------------------------------------------------

def _grid():
    return [
        ["Inventaris Gudang"],
        [],
        ["Kode", "Barang", None, "Harga", "Stok"],
        ["B1", "Laptop ASUS", None, 7_500_000, 10],
    ]


def test_validate_spec_rejects_label_col_out_of_range():
    spec = _inventory_spec()
    spec.label_col = 99

    with pytest.raises(ValueError, match="label_col"):
        _validate_spec(spec, _grid())


def test_validate_spec_rejects_period_column_without_month():
    spec = _TableSpec(
        header_row=2, label_col=1, data_start_row=3, axis_type="temporal",
        title=None, unit=None,
        columns=[_ColumnSpec(col=3, kind="period", year=2026, month=None)],
    )

    with pytest.raises(ValueError, match="invalid month"):
        _validate_spec(spec, _grid())


def test_grid_snapshot_larger_caps_expose_far_cells():
    grid = [[None] * 45 for _ in range(25)]
    grid[22][40] = 99.9  # beyond the default 20x30 window

    assert "[40]=" not in _grid_snapshot(grid)
    assert "[40]='99.9'" in _grid_snapshot(grid, max_rows=120, max_cols=100)


def test_validate_spec_accepts_quarter_tokens_as_period_months():
    spec = _TableSpec(
        header_row=2, label_col=1, data_start_row=3, axis_type="temporal",
        title=None, unit=None,
        columns=[_ColumnSpec(col=3, kind="period", year=2026, month="Q2")],
    )

    _validate_spec(spec, _grid())  # must not raise


def test_validate_spec_rejects_invalid_quarter_token():
    spec = _TableSpec(
        header_row=2, label_col=1, data_start_row=3, axis_type="temporal",
        title=None, unit=None,
        columns=[_ColumnSpec(col=3, kind="period", year=2026, month="Q5")],
    )

    with pytest.raises(ValueError, match="invalid month"):
        _validate_spec(spec, _grid())


def test_validate_spec_rejects_attribute_column_without_name():
    spec = _inventory_spec()
    spec.columns[0].name = "  "

    with pytest.raises(ValueError, match="no name"):
        _validate_spec(spec, _grid())


def test_validate_spec_rejects_columns_without_numeric_data():
    spec = _inventory_spec()
    spec.columns = [_ColumnSpec(col=0, kind="attribute", name="Kode")]  # text-only column

    with pytest.raises(ValueError, match="no numeric values"):
        _validate_spec(spec, _grid())


def test_validate_spec_rejects_data_start_before_header():
    spec = _inventory_spec()
    spec.data_start_row = 1

    with pytest.raises(ValueError, match="out of order"):
        _validate_spec(spec, _grid())


def test_parse_table_with_llm_wraps_llm_failures_as_value_error():
    llm = _llm_returning(RuntimeError("provider down"))

    with pytest.raises(ValueError, match="LLM structure mapping failed"):
        parse_table_with_llm(_messy_inventory_bytes(), "Gudang", llm)


def test_parse_table_with_llm_does_not_cache_invalid_specs():
    bad = _inventory_spec()
    bad.label_col = 99
    llm = _llm_returning(bad)
    data = _messy_inventory_bytes()

    with pytest.raises(ValueError):
        parse_table_with_llm(data, "Gudang", llm)

    assert _SPEC_CACHE == {}

# ---------------------------------------------------------------------------
# The grid seam: parse_table_with_llm is now a thin wrapper over parse_grid_with_llm, and the
# layout-fingerprint cache lives in the grid function so any kind of source can hit it.
# ---------------------------------------------------------------------------

def test_parse_grid_with_llm_matches_the_bytes_wrapper():
    data = _messy_inventory_bytes()
    spec = _inventory_spec()
    from_bytes = parse_table_with_llm(data, "Gudang", _llm_returning(spec))
    _SPEC_CACHE.clear()
    from_grid = parse_grid_with_llm(_load_grid(data, "Gudang"), _llm_returning(spec))
    assert from_grid == from_bytes


def test_parse_grid_with_llm_populates_the_layout_cache():
    grid = _load_grid(_messy_inventory_bytes(), "Gudang")
    calls = []
    llm = _llm_returning(_inventory_spec(), call_log=calls)
    parse_grid_with_llm(grid, llm)
    parse_grid_with_llm(grid, llm)
    assert len(calls) == 1, "the same layout must be mapped only once"
