"""Every Tidak Cukup Data says why, as a machine-readable reason_code (docs/audit-2026-10.md, M8).

The verdict stays Inconclusive; the code tells the user (and the success metrics) whether the
claim is outside the uploaded tables, at a missing period, of a kind the row cannot answer, ...
"""

import asyncio
from unittest.mock import Mock, patch

from excel_parser_bi import BITableData
from paired_verifier import _ExcelSource, _evaluate_fact_safely, verify_paired
from schemas import FactVerificationResult
from structured_extractor import ExtractedFact, PeriodPoint


def _source(rows, unit="triliun Rp", title="Uang Beredar (M2)"):
    """rows: {(label, year, month): value}"""
    table = BITableData(title=title, unit=unit, row_labels=[])
    for (label, year, month), value in rows.items():
        if label not in table.row_labels:
            table.row_labels.append(label)
        table._data[(label, year, month)] = value
    return _ExcelSource(table=table, filename="TABEL1_1.xls", sheet="I.1")


def _fact(label="Uang Beredar Luas (M2)", operation="value", claimed=10355.1, unit="triliun Rp",
          year=2026, month="Apr", quote="Posisi M2 pada April 2026 tercatat Rp10.355,1 triliun.", **kw):
    return ExtractedFact(operation=operation, claimed_value=claimed, unit=unit, context_quote=quote,
                         page_number=1, periods=[PeriodPoint(metric_label=label, year=year, month=month)], **kw)


M2 = {("Uang Beredar Luas (M2)", 2026, "Apr"): 10355.1, ("Uang Beredar Luas (M2)", 2025, "Apr"): 9500.0}


def test_a_metric_no_table_has_is_tidak_ditemukan():
    fact = _fact(label="Suku Bunga Deposito", claimed=4.8, unit="%",
                 quote="Suku bunga deposito 1 bulan tercatat 4,8%.")
    result = _evaluate_fact_safely(fact, [_source(M2)])
    assert result.verdict == "Inconclusive"
    assert result.reason_code == "tidak_ditemukan"


def test_a_yoy_claim_without_the_prior_year_is_periode():
    rows = {("Uang Beredar Luas (M2)", 2026, "Apr"): 10355.1}   # no April 2025
    fact = _fact(operation="yoy_growth", claimed=9.2, unit="%",
                 quote="M2 pada April 2026 tumbuh sebesar 9,2% (yoy).")
    result = _evaluate_fact_safely(fact, [_source(rows)])
    assert result.verdict == "Inconclusive"
    assert result.reason_code == "periode"


def test_a_threshold_claim_without_a_bound_is_klaim_tidak_lengkap():
    rows = {("PMI-BI", 2026, "Q2"): 51.43}
    fact = _fact(label="PMI-BI", operation="above_threshold", claimed=None, unit="indeks",
                 year=2026, month="Q2", quote="PMI-BI berada pada fase ekspansi.")
    result = _evaluate_fact_safely(fact, [_source(rows, unit="indeks", title="PMI-BI")])
    assert result.verdict == "Inconclusive"
    assert result.reason_code == "klaim_tidak_lengkap"


def test_a_monthly_rate_claim_against_a_yoy_table_is_jenis():
    rows = {("Indeks Penjualan Riil", 2026, "Jul"): 1.1}
    fact = _fact(label="Indeks Penjualan Riil", claimed=-0.1, unit="%", year=2026, month="Jul",
                 quote="Penjualan eceran terkontraksi sebesar 0,1% (mtm) pada Juli 2026.")
    result = _evaluate_fact_safely(fact, [_source(rows, unit="%, yoy", title="Pertumbuhan Penjualan")])
    assert result.verdict == "Inconclusive"
    assert result.reason_code == "jenis"


@patch("paired_verifier._evaluate_fact_safely")
@patch("paired_verifier.extract_structured_facts_async")
@patch("paired_verifier.parse_bi_table")
def test_no_tidak_cukup_data_leaves_verify_paired_without_a_code(mock_parse, mock_extract, mock_eval):
    # Whatever path forgot to say why, the response still says something: 'lainnya'.
    mock_parse.return_value = _source(M2).table
    mock_extract.return_value = [_fact()]
    mock_eval.return_value = FactVerificationResult(
        operation="value", metric_label="M2", verdict="Inconclusive", reasoning="?", context_quote="q")
    response = asyncio.run(verify_paired(
        narrative_text="[== Halaman 1 ==]\n" + "x" * 250,
        excel_sources=[(b"xls-bytes", "I.1", "TABEL1_1.xls")], llm=Mock()))
    assert [r.reason_code for r in response.results] == ["lainnya"]


def test_a_matched_result_carries_no_reason_code():
    result = _evaluate_fact_safely(_fact(), [_source(M2)])
    assert result.verdict == "Entailed"
    assert result.reason_code is None
