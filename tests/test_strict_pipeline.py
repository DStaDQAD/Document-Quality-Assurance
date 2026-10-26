"""A whole check through the strict path, handed over by verify_paired only when STRICT_EXTRACTION
is on and the check is an Excel-only one; otherwise verify_paired runs exactly as before."""
import asyncio
from unittest.mock import AsyncMock, Mock, patch

import pytest
from langchain_core.runnables import RunnableLambda

from excel_parser_bi import BITableData
from strict.claims import CellRef, StrictClaim, StrictClaims

NARRATIVE = "[== Halaman 1 ==]\nPosisi AFLN Indonesia pada triwulan IV 2025 tercatat 558,5 miliar dolar AS.\n"


def _table():
    table = BITableData(title="Posisi Investasi Internasional", unit="Juta USD", row_labels=["Aset", "Kewajiban"])
    table._data[("Aset", 2025, "Q4")] = 558496.4949
    table._data[("Kewajiban", 2025, "Q4")] = 831121.5700
    return table


def _llm(seen):
    claim = StrictClaim(anchor_quote="tercatat 558,5 miliar dolar AS", subject="Posisi AFLN Indonesia",
                        operation="value", claimed_value_raw="558,5", unit="miliar USD",
                        cells=[CellRef(table="S1", row="Aset", period="Q4 2025")])

    def _respond(prompt_value):
        seen.append(prompt_value.to_messages())
        return StrictClaims(claims=[claim])

    llm = Mock()
    llm.with_structured_output = Mock(return_value=RunnableLambda(_respond))
    return llm


def _verify(**kw):
    from paired_verifier import verify_paired
    args = dict(narrative_text=NARRATIVE, excel_sources=[(b"x", "5.39", "t.xls")])
    args.update(kw)
    return asyncio.run(verify_paired(**args))


@patch("paired_verifier.extract_structured_facts_async", new_callable=AsyncMock, return_value=[])
@patch("paired_verifier.parse_bi_table")
def test_off_verify_paired_is_unchanged(mock_parse, mock_extract, monkeypatch):
    monkeypatch.delenv("STRICT_EXTRACTION", raising=False)
    mock_parse.return_value = _table()

    response = _verify(llm=_llm([]))

    assert mock_extract.called and response.strict_claims == []


@patch("paired_verifier.extract_structured_facts_async", new_callable=AsyncMock, return_value=[])
@patch("paired_verifier.parse_bi_table")
def test_on_a_check_goes_strict(mock_parse, mock_extract, monkeypatch):
    monkeypatch.setenv("STRICT_EXTRACTION", "1")
    mock_parse.return_value = _table()

    response = _verify(llm=_llm([]))

    assert not mock_extract.called
    [result] = response.results
    assert (result.verdict, result.resolved_via) == ("Entailed", "strict")
    assert response.entailed_count == 1 and response.total_facts == 1
    assert len(response.strict_claims) == 1 and response.strict_claims[0]["cells"][0]["row"] == "Aset"


@patch("paired_verifier.extract_structured_facts_async", new_callable=AsyncMock, return_value=[])
@patch("paired_verifier.parse_bi_table")
def test_on_but_internal_mode_stays_old(mock_parse, mock_extract, monkeypatch):
    monkeypatch.setenv("STRICT_EXTRACTION", "1")
    mock_parse.return_value = _table()

    response = _verify(llm=_llm([]), mode="internal")

    assert mock_extract.called and response.strict_claims == []


@patch("paired_verifier.team_settings.for_check", return_value=({"posisi afln indonesia": "Aset"}, ""))
@patch("paired_verifier.parse_bi_table")
def test_team_aliases_become_hints_not_substitutions(mock_parse, _for_check, monkeypatch):
    monkeypatch.setenv("STRICT_EXTRACTION", "1")
    mock_parse.return_value = _table()
    seen = []

    _verify(llm=_llm(seen))

    assert "\"posisi afln indonesia\" → 'Aset'" in seen[0][1].content


@patch("ai_review.review_claims", new_callable=AsyncMock, side_effect=RuntimeError("down"))
@patch("paired_verifier.parse_bi_table")
def test_a_review_failure_keeps_the_verdicts(mock_parse, _review, monkeypatch):
    monkeypatch.setenv("STRICT_EXTRACTION", "1")
    mock_parse.return_value = _table()

    response = _verify(llm=_llm([]), review_llm=Mock())

    assert [r.verdict for r in response.results] == ["Entailed"]


@pytest.mark.parametrize("value, on", [("1", True), (" ON ", True), ("yes", True), ("", False), ("0", False)])
def test_the_flag(monkeypatch, value, on):
    from strict.pipeline import enabled
    monkeypatch.setenv("STRICT_EXTRACTION", value)

    assert enabled() is on


def test_charts_or_a_category_sheet_keep_the_old_path():
    from paired_verifier import _ExcelSource
    from strict.pipeline import applies

    series = _ExcelSource(table=_table(), filename="t.xls", sheet="5.39")
    items = BITableData(title="Barang", unit="Rp", row_labels=["Laptop"])
    items.axis_type, items.col_labels = "categorical", ["Harga"]
    items._data[("Laptop", "Harga")] = 7.5
    category = _ExcelSource(table=items, filename="b.xlsx", sheet="Barang")

    assert applies([series], "excel", has_pdf_tables=False, has_charts=False)
    assert not applies([series], "excel", has_pdf_tables=False, has_charts=True)
    assert not applies([series, category], "excel", has_pdf_tables=False, has_charts=False)
