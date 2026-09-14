import asyncio
from unittest.mock import Mock

from langchain_core.runnables import RunnableLambda

from cell_pointer import (
    _BatchCellPointers,
    _CellPointer,
    _MultiCellPointer,
    _MultiSourcePointers,
    _SheetUnit,
    PointQuery,
    build_point_queries,
    build_snapshot,
    metric_could_match,
    pointer_is_plausible,
    read_grid_cell,
    resolve_pointers,
    resolve_pointers_multi,
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


# ---------------------------------------------------------------------------
# resolve_pointers_multi — every source in ONE call
#
# Internal mode turns one report into ~25 sources, and asking each separately re-sent the
# 511-token system prompt AND the whole query list every time. Measured on the April report
# (2026-09-14): 22 calls, 45.231 chars of query text and 44.990 of prompt against only 29.002
# chars of actual snapshot — the repetition was the cost, not the payload.
# ---------------------------------------------------------------------------

_GRID_A = [["Metric", 2026], ["KPR/KPA", 40.63]]
_GRID_B = [["Metric", 2026], ["Kredit UMKM", 12.5]]
_MULTI_QUERIES = [
    PointQuery(fact_index=0, data_key=("KPR/KPA", 2026, "Q2"), desc="d0"),
    PointQuery(fact_index=1, data_key=("Kredit UMKM", 2026, "Q2"), desc="d1"),
]
# (label, grid, the GLOBAL query indices this source may be asked about)
_MULTI_SOURCES = [("A.xls / S1", _GRID_A, [0]), ("B.xls / S2", _GRID_B, [1])]


def test_resolve_pointers_multi_asks_every_source_in_a_single_call():
    batch = _MultiSourcePointers(pointers=[
        _MultiCellPointer(source_index=0, query_index=0, found=True, row=1, col=1),
        _MultiCellPointer(source_index=1, query_index=1, found=True, row=1, col=1),
    ])
    log = []

    resolutions = asyncio.run(
        resolve_pointers_multi(_MULTI_SOURCES, _MULTI_QUERIES, _llm_returning(batch, log))
    )

    assert len(log) == 1
    assert [pointers for pointers, _ in resolutions] == [{0: (1, 1)}, {1: (1, 1)}]


def test_resolve_pointers_multi_returns_one_slot_per_source_in_order():
    """paired_verifier zips the result against its own source list, so the alignment is the
    contract — a source the model ignored still gets its (empty) slot."""
    batch = _MultiSourcePointers(pointers=[
        _MultiCellPointer(source_index=1, query_index=1, found=True, row=1, col=1),
    ])

    resolutions = asyncio.run(
        resolve_pointers_multi(_MULTI_SOURCES, _MULTI_QUERIES, _llm_returning(batch))
    )

    assert len(resolutions) == len(_MULTI_SOURCES)
    assert resolutions[0][0] == {}


def test_resolve_pointers_multi_lets_two_sources_answer_the_same_query():
    """Asking each source separately meant a claim could be answered by any of them; the
    caller still takes the first source whose cells all read, so both answers must survive."""
    sources = [("A.xls / S1", _GRID_A, [0]), ("B.xls / S2", _GRID_A, [0])]
    batch = _MultiSourcePointers(pointers=[
        _MultiCellPointer(source_index=0, query_index=0, found=True, row=1, col=1),
        _MultiCellPointer(source_index=1, query_index=0, found=True, row=1, col=1),
    ])

    resolutions = asyncio.run(
        resolve_pointers_multi(sources, _MULTI_QUERIES, _llm_returning(batch))
    )

    assert [pointers for pointers, _ in resolutions] == [{0: (1, 1)}, {0: (1, 1)}]


def test_resolve_pointers_multi_drops_unusable_pointers():
    batch = _MultiSourcePointers(pointers=[
        _MultiCellPointer(source_index=0, query_index=0, found=False, row=1, col=1),
        _MultiCellPointer(source_index=9, query_index=0, found=True, row=1, col=1),
        _MultiCellPointer(source_index=0, query_index=7, found=True, row=1, col=1),
        _MultiCellPointer(source_index=1, query_index=1, found=True, row=None, col=1),
    ])

    resolutions = asyncio.run(
        resolve_pointers_multi(_MULTI_SOURCES, _MULTI_QUERIES, _llm_returning(batch))
    )

    assert [pointers for pointers, _ in resolutions] == [{}, {}]


def test_resolve_pointers_multi_rejects_a_query_the_source_was_not_offered():
    """The per-source filter is what keeps a sheet from answering about a metric it cannot
    hold; a pointer outside that offer would smuggle the filtered query back in."""
    batch = _MultiSourcePointers(pointers=[
        _MultiCellPointer(source_index=0, query_index=1, found=True, row=1, col=1),
    ])

    resolutions = asyncio.run(
        resolve_pointers_multi(_MULTI_SOURCES, _MULTI_QUERIES, _llm_returning(batch))
    )

    assert resolutions[0][0] == {}


def test_resolve_pointers_multi_maps_units_per_source():
    batch = _MultiSourcePointers(
        units=[_SheetUnit(source_index=1, unit=" Miliar Rp ")],
        pointers=[],
    )

    resolutions = asyncio.run(
        resolve_pointers_multi(_MULTI_SOURCES, _MULTI_QUERIES, _llm_returning(batch))
    )

    assert [unit for _, unit in resolutions] == [None, "Miliar Rp"]


def test_resolve_pointers_multi_skips_a_source_with_nothing_to_ask():
    """A source whose queries were all filtered out costs a snapshot for nothing."""
    sources = [("A.xls / S1", _GRID_A, [0]), ("B.xls / S2", _GRID_B, [])]
    batch = _MultiSourcePointers(pointers=[])
    log = []

    resolutions = asyncio.run(
        resolve_pointers_multi(sources, _MULTI_QUERIES, _llm_returning(batch, log))
    )

    assert len(resolutions) == 2
    assert "B.xls / S2" not in str(log[0])


def test_resolve_pointers_multi_no_source_has_queries_skips_the_llm():
    llm = Mock()

    resolutions = asyncio.run(
        resolve_pointers_multi([("A.xls / S1", _GRID_A, [])], _MULTI_QUERIES, llm)
    )

    assert resolutions == [({}, None)]
    llm.with_structured_output.assert_not_called()


def test_resolve_pointers_multi_swallows_llm_failure():
    resolutions = asyncio.run(
        resolve_pointers_multi(_MULTI_SOURCES, _MULTI_QUERIES, _llm_raising())
    )

    assert resolutions == [({}, None), ({}, None)]


def test_resolve_pointers_multi_uses_fallback_llm_when_primary_fails():
    batch = _MultiSourcePointers(pointers=[
        _MultiCellPointer(source_index=0, query_index=0, found=True, row=1, col=1),
    ])

    resolutions = asyncio.run(
        resolve_pointers_multi(
            _MULTI_SOURCES, _MULTI_QUERIES, _llm_raising(), fallback_llm=_llm_returning(batch)
        )
    )

    assert resolutions[0][0] == {0: (1, 1)}


# ---------------------------------------------------------------------------
# Column guard: a pointer must land under the period that was asked for
# ---------------------------------------------------------------------------

def _snippet_grid():
    """A snippet table: two level columns, then two '%, yoy' columns for the SAME months."""
    return [
        ["Tabel 9. Komponen Uang Primer adjusted", None, None, None, None],
        [None, 2026, 2026, None, None],
        [None, "Mar", "Apr*", "Mar'26", "Apr'26*"],
        ["Uang Primer adjusted", 2396.5, 2232.2, 16.8, 14.3],
    ]


def test_pointer_column_guard_rejects_a_growth_cell_offered_as_a_prior_year_level():
    from cell_pointer import pointer_column_matches

    grid = _snippet_grid()
    # The yoy denominator for April 2025 does not exist in this table at all. The model pointed
    # at the "Apr'26*" growth cell; code read 14,3 and reported 2.232,2 / 14,3 = 15.509% yoy.
    assert pointer_column_matches(grid, row=3, col=4, year=2025, month="Apr") is False
    # The column that really is April 2026 still passes.
    assert pointer_column_matches(grid, row=3, col=2, year=2026, month="Apr") is True


def test_pointer_column_guard_accepts_a_column_with_no_legible_header():
    from cell_pointer import pointer_column_matches

    # The pointer pass exists for sheets no parser understands; demanding a readable header
    # there would switch it off entirely.
    grid = [["something"], [None, None], ["Metric", 1.0, 2.0]]
    assert pointer_column_matches(grid, row=2, col=1, year=2025, month="Apr") is True
