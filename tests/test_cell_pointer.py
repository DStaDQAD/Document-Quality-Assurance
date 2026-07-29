import asyncio
from unittest.mock import Mock

from langchain_core.runnables import RunnableLambda

from cell_pointer import (
    _BatchCellPointers,
    _CellPointer,
    PointQuery,
    build_point_queries,
    build_snapshot,
    metric_could_match,
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


def test_build_point_queries_includes_claim_context():
    # The claim sentence carries the dimensions the structured form drops — it must ride
    # along so the pointer can disambiguate rows on a long-format table.
    fact = _fact(periods=[PeriodPoint(metric_label="Laptop", col_label="Revenue")])
    fact.context_quote = "Surabaya mencatat pendapatan Laptop tertinggi pada Q2 sebesar 52.000"

    queries = build_point_queries([fact], [0])

    assert "konteks:" in queries[0].desc
    assert "Surabaya" in queries[0].desc


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
# metric_could_match — the same guard, quantified over the sheet (pre-call filter)
# ---------------------------------------------------------------------------

def test_metric_could_match_true_when_some_row_shares_a_word():
    assert metric_could_match(_PLAUSIBLE_GRID, "Kredit Modal Kerja") is True


def test_metric_could_match_false_when_no_row_relates():
    # Asking this sheet about ILS can only produce a pointer the guard would reject.
    assert metric_could_match(_PLAUSIBLE_GRID, "Indeks Lending Standard (ILS)") is False


def test_metric_could_match_true_via_title_and_for_unguarded_metric():
    assert metric_could_match([["TOTAL", 1.0]], "Cadangan Devisa",
                              table_title="Cadangan Devisa Indonesia") is True
    assert metric_could_match(_PLAUSIBLE_GRID, "M2") is True


def test_metric_could_match_agrees_with_the_row_guard():
    # The filter must never drop a metric that some row would have accepted.
    for metric in ("Kredit Modal Kerja", "Indeks Lending Standard (ILS)", "M2", "TOTAL"):
        any_row_ok = any(
            pointer_is_plausible(_PLAUSIBLE_GRID, r, metric)
            for r in range(len(_PLAUSIBLE_GRID))
        )
        assert metric_could_match(_PLAUSIBLE_GRID, metric) == any_row_ok


# ---------------------------------------------------------------------------
# build_snapshot — relevance selection, printed with ORIGINAL coordinates
# ---------------------------------------------------------------------------

def _wide_grid():
    """A time-series sheet whose newest year sits far to the right, like the BI tables.

    Deliberately taller than the header window so row selection has room to act.
    """
    def _row(label):
        return ["", label] + [float(i) for i in range(24)]

    return [
        ["", ""] + ["2024"] * 12 + ["2025"] * 12,
        ["", "KETERANGAN"] + ["Jan"] * 24,
        _row("Uang Beredar Sempit (M1)"),
        _row("Uang Kartal"),
        _row("Uang Giral"),
        _row("Uang Kuasi"),
        _row("Surat Berharga Selain Saham"),
        _row("Tagihan Bersih kepada Pemerintah"),
        _row("Aktiva Luar Negeri Bersih"),
    ]


def test_build_snapshot_keeps_original_indices_and_drops_old_years():
    grid = _wide_grid()
    queries = [PointQuery(fact_index=0, data_key=("Uang Beredar Sempit (M1)", 2025, "Jan"), desc="d")]

    snapshot, note = build_snapshot(grid, queries)

    # The 2025 block starts at column 14; the 2024 columns are not worth sending.
    assert "[14]=" in snapshot
    assert "[2]=" not in snapshot
    # Row 3 is an unrelated metric — the guard would reject a pointer there anyway.
    assert "Tagihan Bersih" not in snapshot
    assert "relevant to these queries" in note


def test_build_snapshot_indices_read_back_from_the_full_grid():
    grid = _wide_grid()
    queries = [PointQuery(fact_index=0, data_key=("Uang Beredar Sempit (M1)", 2025, "Jan"), desc="d")]

    snapshot, _ = build_snapshot(grid, queries)

    # Whatever coordinate the LLM copies out of the snapshot must address the real grid.
    for line in snapshot.splitlines():
        row = int(line.split(":")[0].removeprefix("row ").strip())
        for cell in line.split(": ", 1)[1].split(" ["):
            if "]=" not in cell:
                continue
            col = int(cell.split("]=")[0].lstrip("["))
            printed = cell.split("]=", 1)[1]
            if printed.startswith(("'", '"')):
                continue
            assert read_grid_cell(grid, row, col) == float(printed)


def test_build_snapshot_falls_back_to_full_window_when_no_year_matches():
    grid = _wide_grid()
    # 2099 appears in no header, so the column selection cannot be trusted.
    queries = [PointQuery(fact_index=0, data_key=("Uang Beredar Sempit (M1)", 2099, "Jan"), desc="d")]

    snapshot, _ = build_snapshot(grid, queries)

    assert "[2]=" in snapshot  # the 2024 block is back


def test_build_snapshot_trim_keeps_label_columns_and_the_newest_periods():
    # A selection wider than the cap must lose its OLDEST period columns, never the label
    # column — slicing from the front would drop both the labels and the target cells.
    from cell_pointer import _POINTER_MAX_COLS, _select_cols, _select_rows

    n_old, n_new = 300, _POINTER_MAX_COLS + 40
    grid = [
        ["", ""] + ["2010"] * n_old + ["2025"] * n_new,
        ["", "KETERANGAN"] + ["Jan"] * (n_old + n_new),
    ] + [
        ["", label] + [float(i) for i in range(n_old + n_new)]
        for label in ("Uang Beredar Sempit (M1)", "Uang Kartal", "Uang Giral", "Uang Kuasi",
                      "Surat Berharga", "Tagihan Bersih", "Aktiva Luar Negeri")
    ]
    queries = [PointQuery(fact_index=0, data_key=("Uang Beredar Sempit (M1)", 2025, "Jan"), desc="d")]

    cols = _select_cols(grid, queries, _select_rows(grid, queries))

    assert len(cols) <= _POINTER_MAX_COLS
    assert 1 in cols                              # the label column survived
    assert cols[-1] == n_old + n_new + 1          # the newest period survived
    assert n_old + 2 not in cols                  # the oldest of the selection was dropped


def test_build_snapshot_keeps_every_row_when_a_metric_is_unguarded():
    grid = _wide_grid()
    # 'M2' has no significant word, so the guard accepts any row and none may be dropped.
    queries = [PointQuery(fact_index=0, data_key=("M2", 2025, "Jan"), desc="d")]

    snapshot, _ = build_snapshot(grid, queries)

    assert "Tagihan Bersih" in snapshot


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
