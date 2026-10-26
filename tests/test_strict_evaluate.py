"""The strict engine: a claim judged on the cells it names, nothing guessed. Every Sesuai case has a
Tidak Sesuai twin, so a test cannot pass by an engine that agrees with everything."""
import ast
from pathlib import Path

import pytest

from excel_parser_bi import BITableData
from paired_verifier import _ExcelSource
from strict.claims import Cell, Claim
from strict.evaluate import evaluate_claim
from strict.tables import build_views

ROOT = Path(__file__).resolve().parents[1]


def _views(*tables):
    """tables: (unit, {(row, year, period): value}) per table, in S1, S2 order."""
    sources = []
    for unit, data in tables:
        rows = list(dict.fromkeys(k[0] for k in data))
        table = BITableData(title="Tabel", unit=unit, row_labels=rows)
        table._data.update(data)
        sources.append(_ExcelSource(table=table, filename="t.xls", sheet=f"sheet{len(sources) + 1}"))
    return build_views(sources)


def _cell(row, year, period, code="S1"):
    return Cell(code=code, row_text=row, period_text=f"{period} {year}", row=row, key=(year, period))


def _claim(operation, cells, value=None, unit=None, quote="Kalimat klaim.", **kw):
    return Claim(operation=operation, subject="subjek", cells=cells, context_quote=quote,
                 claimed_value=value, unit=unit, **kw)


PII = ("Juta USD", {("Aset", 2025, "Q4"): 558496.4949, ("Aset", 2025, "Q3"): 545541.7343})


@pytest.mark.parametrize("value, verdict", [(558.5, "Entailed"), (558.6, "Refuted")])
def test_value_rounds_as_printed(value, verdict):
    printed = f"{value}".replace(".", ",")
    result = evaluate_claim(_claim("value", [_cell("Aset", 2025, "Q4")], value, "miliar USD",
                                   quote=f"Posisi AFLN tercatat {printed} miliar dolar AS."), _views(PII))

    assert result.verdict == verdict and result.resolved_via == "strict"


@pytest.mark.parametrize("value, verdict", [(5.0, "Entailed"), (6.0, "Refuted")])
def test_yoy_from_two_levels(value, verdict):
    views = _views(("Miliar Rp", {("M2", 2024, "Dec"): 9000.1234, ("M2", 2025, "Dec"): 9450.1296}))
    claim = _claim("yoy_growth", [_cell("M2", 2024, "Dec"), _cell("M2", 2025, "Dec")], value, "persen",
                   quote=f"M2 tumbuh {value:.1f}% (yoy).".replace(".", ",", 1))

    assert evaluate_claim(claim, views).verdict == verdict


@pytest.mark.parametrize("value, verdict", [(7.6, "Entailed"), (7.9, "Refuted")])
def test_yoy_from_a_growth_cell(value, verdict):
    views = _views(("%, yoy", {("INDEKS TOTAL", 2026, "Jun"): 7.6123}))
    claim = _claim("yoy_growth", [_cell("INDEKS TOTAL", 2026, "Jun")], value, "persen",
                   quote=f"IPR tumbuh {value}% (yoy).".replace(".", ","))

    assert evaluate_claim(claim, views).verdict == verdict


@pytest.mark.parametrize("value, verdict", [(-16.8, "Entailed"), (16.8, "Refuted")])
def test_diff_is_signed(value, verdict):
    views = _views(("Miliar USD", {("Modal Ekuitas", 2026, "Q1"): 300.0, ("Modal Ekuitas", 2026, "Q2"): 283.2}))
    claim = _claim("diff", [_cell("Modal Ekuitas", 2026, "Q1"), _cell("Modal Ekuitas", 2026, "Q2")],
                   value, "miliar USD")

    assert evaluate_claim(claim, views).verdict == verdict


@pytest.mark.parametrize("value, verdict", [(50.0, "Entailed"), (55.0, "Refuted")])
def test_ratio_sums_numerators(value, verdict):
    views = _views(("Juta USD", {("A", 2026, "Q2"): 30.0, ("B", 2026, "Q2"): 20.0, ("Total", 2026, "Q2"): 100.0}))
    claim = _claim("ratio", [_cell("A", 2026, "Q2"), _cell("B", 2026, "Q2"), _cell("Total", 2026, "Q2")],
                   value, "persen")

    assert evaluate_claim(claim, views).verdict == verdict


@pytest.mark.parametrize("operation, value, verdict", [
    ("sum", 30.0, "Entailed"), ("sum", 31.0, "Refuted"), ("average", 15.0, "Entailed"), ("average", 16.0, "Refuted")])
def test_sum_and_average(operation, value, verdict):
    views = _views(("Triliun Rp", {("X", 2026, "Jan"): 10.0, ("X", 2026, "Feb"): 20.0}))
    claim = _claim(operation, [_cell("X", 2026, "Jan"), _cell("X", 2026, "Feb")], value, "triliun Rp")

    assert evaluate_claim(claim, views).verdict == verdict


@pytest.mark.parametrize("b_now, verdict", [(4.0, "Entailed"), (2.0, "Refuted")])
def test_trends_per_row(b_now, verdict):
    views = _views(("Indeks", {("A", 2026, "Jun"): 1.0, ("A", 2026, "Jul"): 2.0,
                               ("B", 2026, "Jun"): 3.0, ("B", 2026, "Jul"): b_now}))
    claim = _claim("is_increasing", [_cell("A", 2026, "Jun"), _cell("A", 2026, "Jul"),
                                     _cell("B", 2026, "Jun"), _cell("B", 2026, "Jul")])

    assert evaluate_claim(claim, views).verdict == verdict


@pytest.mark.parametrize("now, verdict", [(10.3, "Entailed"), (12.0, "Refuted")])
def test_stable_uses_the_band(now, verdict):
    views = _views(("persen", {("Rasio", 2026, "Jun"): 10.0, ("Rasio", 2026, "Jul"): now}))
    claim = _claim("is_stable", [_cell("Rasio", 2026, "Jun"), _cell("Rasio", 2026, "Jul")])

    assert evaluate_claim(claim, views).verdict == verdict


@pytest.mark.parametrize("second, verdict", [(51.0, "Entailed"), (49.0, "Refuted")])
def test_thresholds_every_cell(second, verdict):
    views = _views(("%, Indeks", {("Volume Produksi", 2026, "Q1"): 52.0, ("Volume Pesanan", 2026, "Q1"): second}))
    claim = _claim("above_threshold", [_cell("Volume Produksi", 2026, "Q1"), _cell("Volume Pesanan", 2026, "Q1")],
                   50.0)

    assert evaluate_claim(claim, views).verdict == verdict


@pytest.mark.parametrize("claimed, verdict", [(1.0, "Entailed"), (2.0, "Refuted")])
def test_formula_count(claimed, verdict):
    views = _views(("persen", {("Kota > A", 2025, "Q3"): 1.0, ("Kota > A", 2025, "Q4"): 0.8,
                               ("Kota > B", 2025, "Q3"): 2.0, ("Kota > B", 2025, "Q4"): 2.5}))
    claim = _claim("formula", [_cell("Kota > A", 2025, "Q3"), _cell("Kota > A", 2025, "Q4"),
                               _cell("Kota > B", 2025, "Q3"), _cell("Kota > B", 2025, "Q4")],
                   claimed, formula="(c2<c1)+(c4<c3)", formula_unit="jumlah")

    assert evaluate_claim(claim, views).verdict == verdict


@pytest.mark.parametrize("now, verdict", [(-1.0, "Entailed"), (-5.0, "Refuted")])
def test_formula_condition(now, verdict):
    views = _views(("persen", {("Swasta", 2026, "Mar"): -3.0, ("Swasta", 2026, "Jun"): now}))
    claim = _claim("formula", [_cell("Swasta", 2026, "Mar"), _cell("Swasta", 2026, "Jun")],
                   formula="c2<0 and abs(c2)<abs(c1)", formula_unit="persen")

    assert evaluate_claim(claim, views).verdict == verdict


def test_formula_outside_the_grammar_is_inconclusive():
    claim = _claim("formula", [_cell("Aset", 2025, "Q4")], 1.0, formula="__import__('os')", formula_unit="jumlah")

    result = evaluate_claim(claim, _views(PII))

    assert result.verdict == "Inconclusive" and result.reason_code == "klaim_tidak_lengkap"


def test_the_reasons_for_tidak_cukup_data():
    views = _views(PII)
    cases = {
        "di_luar_tabel": _claim("value", [], 1.0, not_in_tables=True, reason="sektor tidak ada"),
        "klaim_tidak_lengkap": _claim("value", [], 1.0),
        "tidak_ditemukan": _claim("value", [Cell("S1", "AFLN", "Q4 2025", None, (2025, "Q4"))], 1.0),
        "periode": _claim("value", [Cell("S1", "Aset", "akhir tahun", "Aset", None)], 1.0),
    }
    for code, claim in cases.items():
        result = evaluate_claim(claim, views)
        assert (result.verdict, result.reason_code) == ("Inconclusive", code), code
    empty = evaluate_claim(_claim("value", [_cell("Aset", 2020, "Q1")], 1.0), views)
    assert (empty.verdict, empty.reason_code) == ("Inconclusive", "periode")
    unknown_table = evaluate_claim(_claim("value", [_cell("Aset", 2025, "Q4", code="S7")], 1.0), views)
    assert unknown_table.reason_code == "tidak_ditemukan" and "S7" in unknown_table.reasoning


def test_all_members_must_list_every_member():
    data = {(f"Kota > {c}", 2025, period): v for c in "ABCD" for period, v in (("Q3", 1.0), ("Q4", 2.0))}
    views = _views(("Indeks", data))
    cells = [_cell(f"Kota > {c}", 2025, p) for c in "ABC" for p in ("Q3", "Q4")]

    every = evaluate_claim(_claim("is_increasing", cells, all_members=True), views)
    named = evaluate_claim(_claim("is_increasing", cells, all_members=False), views)

    assert (every.verdict, every.reason_code) == ("Inconclusive", "klaim_tidak_lengkap")
    assert "Kota > D" in every.reasoning
    assert named.verdict == "Entailed"


@pytest.mark.parametrize("value, verdict", [(40.0, "Entailed"), (45.0, "Refuted")])
def test_cells_from_two_tables_convert_each_unit(value, verdict):
    views = _views(("Juta USD", {("ULN", 2026, "Q2"): 400000.0}), ("Miliar USD", {("PDB", 2026, "Q2"): 1000.0}))
    claim = _claim("ratio", [_cell("ULN", 2026, "Q2"), _cell("PDB", 2026, "Q2", code="S2")], value, "persen")

    assert evaluate_claim(claim, views).verdict == verdict


def test_units_that_cannot_be_compared_are_jenis():
    views = _views(("Juta USD", {("ULN", 2026, "Q2"): 400000.0}), ("persen", {("Rasio", 2026, "Q2"): 30.0}))
    claim = _claim("sum", [_cell("ULN", 2026, "Q2"), _cell("Rasio", 2026, "Q2", code="S2")], 1.0, "juta USD")

    result = evaluate_claim(claim, views)

    assert (result.verdict, result.reason_code) == ("Inconclusive", "jenis")


def test_a_contraction_claimed_positive_is_refuted():
    views = _views(("%, yoy", {("Kredit Kendaraan", 2026, "Jul"): -9.0}))
    claim = _claim("value", [_cell("Kredit Kendaraan", 2026, "Jul")], 9.0, "persen",
                   quote="Kredit kendaraan terkontraksi sebesar 9,0% (yoy).")

    assert evaluate_claim(claim, views).verdict == "Refuted"


def test_reasoning_lists_the_cells():
    result = evaluate_claim(_claim("value", [_cell("Aset", 2025, "Q4")], 558.5, "miliar USD",
                                   quote="Posisi AFLN tercatat 558,5 miliar dolar AS."), _views(PII))

    assert "S1 · Aset · Q4 2025 = 558496.4949" in result.reasoning
    assert [(p.metric_label, p.year, p.month, p.excel_value) for p in result.periods] == \
        [("Aset", 2025, "Q4", 558496.4949)]


def test_strict_imports_no_matching_or_guards():
    allowed = {"_ExcelSource", "MATCH_TOLERANCE", "_unit_factor", "_parse_scale_unit", "_numeric_verdict",
               "_value_verdict", "_growth_tolerance", "_stable_band", "_period_ordinal"}
    imported = set()
    for path in (ROOT / "strict").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.module == "paired_verifier":
                imported |= {alias.name for alias in node.names}
            if isinstance(node, ast.Import):
                assert all(alias.name != "paired_verifier" for alias in node.names), path

    assert imported <= allowed, imported - allowed
