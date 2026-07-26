import asyncio
from unittest.mock import Mock

from langchain_core.runnables import RunnableLambda

from cell_pointer import (
    _BatchCellPointers,
    _CellPointer,
    PointQuery,
    build_point_queries,
    pointer_is_plausible,
    read_grid_cell,
    resolve_pointers,
)
from structured_extractor import ExtractedFact, PeriodPoint


def _fact(operation="value", periods=None):
    return ExtractedFact(
        operation=operation,
        periods=periods or [PeriodPoint(metric_label="KPR/KPA", year=2026, month="Q2")],
        claimed_value=40.0,
        unit="persen",
        context_quote="q",
        page_number=1,
    )


def _llm_returning(batch, call_log=None):
    def _respond(_prompt_value):
        if call_log is not None:
            call_log.append(_prompt_value)
        return batch

    llm = Mock()
    llm.with_structured_output = Mock(return_value=RunnableLambda(_respond))
    return llm


def _llm_raising():
    def _raise(_prompt_value):
        raise RuntimeError("boom")

    llm = Mock()
    llm.with_structured_output = Mock(return_value=RunnableLambda(_raise))
    return llm


# ---------------------------------------------------------------------------
# build_point_queries
# ---------------------------------------------------------------------------

def test_build_point_queries_temporal_keys_and_desc():
    queries = build_point_queries([_fact()], [0])

    assert len(queries) == 1
    assert queries[0].data_key == ("KPR/KPA", 2026, "Q2")
    assert "Triwulan II (Q2) 2026" in queries[0].desc


def test_build_point_queries_yoy_synthesizes_prior_year():
    queries = build_point_queries([_fact(operation="yoy_growth")], [0])

    assert [q.data_key for q in queries] == [
        ("KPR/KPA", 2026, "Q2"),
        ("KPR/KPA", 2025, "Q2"),
    ]
    assert "pembanding tahun sebelumnya" in queries[1].desc


def test_build_point_queries_categorical_two_tuple_key():
    fact = _fact(periods=[PeriodPoint(metric_label="Laptop ASUS", col_label="Harga")])

    queries = build_point_queries([fact], [0])

    assert queries[0].data_key == ("Laptop ASUS", "Harga")
    assert "atribut='Harga'" in queries[0].desc


def test_build_point_queries_skips_mixed_axis_facts():
    fact = _fact(periods=[
        PeriodPoint(metric_label="A", year=2026, month="Q2"),
        PeriodPoint(metric_label="B", col_label="Harga"),
    ])

    assert build_point_queries([fact], [0]) == []


def test_build_point_queries_only_candidate_indices():
    queries = build_point_queries([_fact(), _fact()], [1])

    assert len(queries) == 1
    assert queries[0].fact_index == 1


# ---------------------------------------------------------------------------
# read_grid_cell — the ONLY pointer-to-number conversion, always from the grid
# ---------------------------------------------------------------------------

def test_read_grid_cell_returns_float_for_numeric():
    assert read_grid_cell([[1, 40.63]], 0, 1) == 40.63


def test_read_grid_cell_rejects_text_bool_and_out_of_range():
    grid = [["na", True], [1.0]]

    assert read_grid_cell(grid, 0, 0) is None    # text sentinel
    assert read_grid_cell(grid, 0, 1) is None    # bool
    assert read_grid_cell(grid, 5, 0) is None    # row out of range
    assert read_grid_cell(grid, 1, 9) is None    # col out of range
    assert read_grid_cell(grid, -1, 0) is None   # negative


# ---------------------------------------------------------------------------
# pointer_is_plausible — reject pointers at rows unrelated to the metric
# ---------------------------------------------------------------------------

_PLAUSIBLE_GRID = [
    ["Menurut Penggunaan", "Kredit Modal Kerja", "Working Capital Loans", 92.11],
    ["TOTAL", "TOTAL", "TOTAL", 93.08],
]


def test_pointer_plausible_when_row_shares_a_metric_word():
    assert pointer_is_plausible(_PLAUSIBLE_GRID, 0, "Kredit Modal Kerja") is True


def test_pointer_implausible_when_row_is_unrelated_total():
    # The ILS regression: a metric absent from the sheet must not bind to the TOTAL row.
    assert pointer_is_plausible(_PLAUSIBLE_GRID, 1, "Indeks Lending Standard (ILS)") is False


def test_pointer_plausible_via_table_title_subject():
    grid = [["TOTAL", 100.0]]
    assert pointer_is_plausible(grid, 0, "Cadangan Devisa", table_title="Cadangan Devisa Indonesia") is True


def test_pointer_plausible_when_metric_has_no_significant_word():
    # Nothing to guard against (e.g. 'M2') — don't block.
    assert pointer_is_plausible(_PLAUSIBLE_GRID, 1, "M2") is True


# ---------------------------------------------------------------------------
# resolve_pointers
# ---------------------------------------------------------------------------

_GRID = [["Metric", 2026], ["KPR/KPA", 40.63]]
_QUERIES = [PointQuery(fact_index=0, data_key=("KPR/KPA", 2026, "Q2"), desc="d0")]


def test_resolve_pointers_returns_mapping_and_sheet_unit():
    batch = _BatchCellPointers(
        sheet_unit=" Miliar Rp ",
        pointers=[_CellPointer(query_index=0, found=True, row=1, col=1)],
    )
    log = []

    pointers, unit = asyncio.run(
        resolve_pointers(_QUERIES, _GRID, _llm_returning(batch, log))
    )

    assert pointers == {0: (1, 1)}
    assert unit == "Miliar Rp"
    assert len(log) == 1  # exactly one batched call


def test_resolve_pointers_drops_not_found_bad_index_and_missing_coords():
    batch = _BatchCellPointers(pointers=[
        _CellPointer(query_index=0, found=False, row=1, col=1),   # not found
        _CellPointer(query_index=7, found=True, row=1, col=1),    # unknown index
        _CellPointer(query_index=0, found=True, row=None, col=1),  # missing coord
    ])

    pointers, unit = asyncio.run(
        resolve_pointers(_QUERIES, _GRID, _llm_returning(batch))
    )

    assert pointers == {}
    assert unit is None


def test_resolve_pointers_first_pointer_wins_for_duplicate_index():
    batch = _BatchCellPointers(pointers=[
        _CellPointer(query_index=0, found=True, row=1, col=1),
        _CellPointer(query_index=0, found=True, row=0, col=0),
    ])

    pointers, _ = asyncio.run(resolve_pointers(_QUERIES, _GRID, _llm_returning(batch)))

    assert pointers == {0: (1, 1)}


def test_resolve_pointers_swallows_llm_failure():
    pointers, unit = asyncio.run(resolve_pointers(_QUERIES, _GRID, _llm_raising()))

    assert pointers == {}
    assert unit is None


def test_resolve_pointers_uses_fallback_llm_when_primary_fails():
    batch = _BatchCellPointers(pointers=[_CellPointer(query_index=0, found=True, row=1, col=1)])

    pointers, _ = asyncio.run(
        resolve_pointers(_QUERIES, _GRID, _llm_raising(), fallback_llm=_llm_returning(batch))
    )

    assert pointers == {0: (1, 1)}


def test_resolve_pointers_no_queries_short_circuits_without_llm_call():
    llm = Mock()

    pointers, unit = asyncio.run(resolve_pointers([], _GRID, llm))

    assert pointers == {}
    assert unit is None
    llm.with_structured_output.assert_not_called()
