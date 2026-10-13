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
        label_cols=[1],
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
        label_cols=[0], data_start_row=1, axis_type="temporal",
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
    spec.label_cols = [99]

    with pytest.raises(ValueError, match="label column"):
        _validate_spec(spec, _grid())


def test_validate_spec_rejects_period_column_without_month():
    spec = _TableSpec(
        label_cols=[1], data_start_row=3, axis_type="temporal",
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
        label_cols=[1], data_start_row=3, axis_type="temporal",
        title=None, unit=None,
        columns=[_ColumnSpec(col=3, kind="period", year=2026, month="Q2")],
    )

    _validate_spec(spec, _grid())  # must not raise


def test_validate_spec_rejects_invalid_quarter_token():
    spec = _TableSpec(
        label_cols=[1], data_start_row=3, axis_type="temporal",
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


def test_validate_spec_rejects_data_start_outside_the_sheet():
    spec = _inventory_spec()
    spec.data_start_row = 99

    with pytest.raises(ValueError, match="data_start_row"):
        _validate_spec(spec, _grid())


def test_parse_table_with_llm_wraps_llm_failures_as_value_error():
    llm = _llm_returning(RuntimeError("provider down"))

    with pytest.raises(ValueError, match="LLM structure mapping failed"):
        parse_table_with_llm(_messy_inventory_bytes(), "Gudang", llm)


def test_parse_table_with_llm_does_not_cache_invalid_specs():
    bad = _inventory_spec()
    bad.label_cols = [99]
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


# ---------------------------------------------------------------------------
# Freer structure map (2026-10-13): several label columns, periods named anywhere, the whole
# sheet's height and its newest columns shown. The code still reads every number.
# ---------------------------------------------------------------------------

from table_parser_llm import _SYSTEM_PROMPT, _extract, _structure_snapshot  # noqa: E402


def _spec(label_cols, columns, data_start_row=1, axis="temporal"):
    return _TableSpec(label_cols=label_cols, data_start_row=data_start_row, axis_type=axis, columns=columns)


def _q(col, year=2026, month="Q2"):
    return _ColumnSpec(col=col, kind="period", year=year, month=month)


def test_the_rightmost_label_cell_names_the_row_and_the_left_one_is_its_group():
    # SBank Tabel 1: col 1 is the group, col 2 the row. Reading the group as the name filed
    # KPR/KPA's 78,2 under "Kredit Konsumsi" (spike 2026-10-13).
    grid = [
        [None, "Jenis", "Rincian", "Q2 2026"],
        [None, "Menurut Penggunaan", "Kredit Konsumsi", 83.67],
        [None, "Kredit Konsumsi", "KPR/KPA", 78.2],
        [None, "Kredit Konsumsi", "Multiguna", 69.71],
    ]
    table = _extract(_spec([1, 2], [_q(3)]), grid)
    assert table.lookup("Kredit Konsumsi", 2026, "Q2") == 83.67
    assert table.lookup("KPR/KPA", 2026, "Q2") == 78.2


def test_a_repeated_name_is_qualified_by_its_group():
    grid = [
        [None, None, "Q2 2026"],
        ["Migas", "Ekspor", 2956.2],
        ["Migas", "Impor", -13136.54],
        ["Nonmigas", "Ekspor", 70459.06],
        ["Nonmigas", "Impor", -58322.94],
    ]
    table = _extract(_spec([0, 1], [_q(2)]), grid)
    assert table.lookup("Migas > Ekspor", 2026, "Q2") == 2956.2
    assert table.lookup("Nonmigas > Impor", 2026, "Q2") == -58322.94


def test_a_name_still_ambiguous_after_qualifying_is_dropped_not_guessed():
    grid = [
        [None, None, "Q2 2026"],
        ["Kredit", "Total", 10.0],
        ["Kredit", "Total", 20.0],
        ["Kredit", "Bank", 5.0],
    ]
    table = _extract(_spec([0, 1], [_q(2)]), grid)
    assert "Kredit > Total" not in table.row_labels and "Total" not in table.row_labels
    assert table.lookup("Bank", 2026, "Q2") == 5.0


def test_a_numbered_row_with_no_label_cell_takes_the_leftmost_text():
    # Cadev ORA (SDDS): the total's name sits in col 0, the components' in col 1, and the model
    # named only col 1 as the label column. The periods come from relative headers.
    grid = [
        ["SDDS Data Category", None, "Unit", "Date of Latest Observation", "Latest Data", "Latest Data -1"],
        ["Official Reserve Assets", None, "Millions of US$", "September/26", 146320.43, 146507.61],
        [None, "Foreign currency reserves", "Millions of US$", "September/26", 125187.37, 124668.19],
        [None, "Gold", "Millions of US$", "September/26", 11739.34, 12446.11],
    ]
    spec = _spec([1], [_ColumnSpec(col=4, kind="period", year=2026, month="Sep"),
                       _ColumnSpec(col=5, kind="period", year=2026, month="Aug")])
    table = _extract(spec, grid)
    assert table.lookup("Official Reserve Assets", 2026, "Sep") == 146320.43
    assert table.lookup("Official Reserve Assets", 2026, "Aug") == 146507.61
    assert table.lookup("Gold", 2026, "Sep") == 11739.34


def test_the_structure_snapshot_shows_every_row_and_the_newest_columns():
    grid = [["Label"] + [f"P{c}" for c in range(1, 100)]] + [[f"Baris {r}"] + list(range(99)) for r in range(60)]
    snap = _structure_snapshot(grid)
    assert "row 60:" in snap                 # the whole height, not the first 20 rows
    assert "[99]=" in snap and "[0]=" in snap  # the newest column and the label column
    assert "[30]=" not in snap and "…" in snap  # the middle is skipped, and says so


def test_the_prompt_maps_relative_period_headers_to_real_months():
    assert "Latest Data" in _SYSTEM_PROMPT and "label_cols" in _SYSTEM_PROMPT


def test_validate_spec_rejects_a_label_column_also_listed_as_data():
    grid = [["Label", "Q2 2026"], ["A", 1.0]]
    with pytest.raises(ValueError, match="label"):
        _validate_spec(_spec([1], [_q(1)]), grid)


def test_next_months_sheet_is_not_served_last_months_map():
    # A spec may read its periods off a date cell ("Latest Data" = the "September/26" beside
    # it). October's sheet, identical but for the date, needs its own map, not September's.
    def ora(date):
        return [["Category", "Unit", "Date", "Latest Data", "Latest Data -1"],
                ["Official Reserve Assets", "Millions of US$", date, 146320.43, 146507.61]]

    spec = _TableSpec(label_cols=[0], data_start_row=1, axis_type="temporal", columns=[
        _ColumnSpec(col=3, kind="period", year=2026, month="Sep"),
        _ColumnSpec(col=4, kind="period", year=2026, month="Aug")])
    calls = []
    llm = _llm_returning(spec, calls)
    parse_grid_with_llm(ora("September/26"), llm)
    parse_grid_with_llm(ora("September/26"), llm)
    assert len(calls) == 1                     # same sheet again: served from the cache
    parse_grid_with_llm(ora("October/26"), llm)
    assert len(calls) == 2                     # new date: mapped afresh
