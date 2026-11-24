"""A whole check through the strict path. verify_paired hands every check over, with no flag: Word or
PDF, time-series or category tables. The label-matching path runs only when a test asks for it
(legacy_label_path), until it is deleted."""
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
def test_every_check_goes_strict_without_a_flag(mock_parse, mock_extract, monkeypatch):
    monkeypatch.delenv("STRICT_EXTRACTION", raising=False)
    mock_parse.return_value = _table()

    response = _verify(llm=_llm([]))

    assert not mock_extract.called
    assert [r.resolved_via for r in response.results] == ["strict"]


@patch("paired_verifier.extract_structured_facts_async", new_callable=AsyncMock, return_value=[])
@patch("paired_verifier.parse_bi_table")
def test_the_label_path_runs_only_when_asked(mock_parse, mock_extract):
    mock_parse.return_value = _table()

    response = _verify(llm=_llm([]), legacy_label_path=True)

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
def test_on_and_both_mode_goes_strict_on_the_excel_tables(mock_parse, mock_extract, monkeypatch):
    # Render 2026-10-27: the team checks the press release and the report together (mode "both",
    # the other document as a reference), and strict was skipped for every such check.
    from pdf_table_extraction import PdfTable
    monkeypatch.setenv("STRICT_EXTRACTION", "1")
    mock_parse.return_value = _table()
    reference = ("Laporan.docx", [PdfTable(page_number=1, caption="Tabel 1", unit="", grid=[["", "Q3 2025", "Q4 2025"], ["Aset lain", 1.0, 2.0]])])

    response = _verify(llm=_llm([]), mode="both", reference_tables=[reference])

    assert not mock_extract.called
    assert response.mode == "both" and response.results[0].resolved_via == "strict"


@patch("paired_verifier.extract_structured_facts_async", new_callable=AsyncMock, return_value=[])
@patch("paired_verifier.parse_bi_table")
def test_internal_mode_goes_strict_too(mock_parse, mock_extract):
    mock_parse.return_value = _table()

    response = _verify(llm=_llm([]), mode="internal")

    assert not mock_extract.called


SETTINGS_BLOCK = ("PUBLIKASI: PII\nFrasa yang biasa dipakai → baris tabel:\n"
                  "- \"posisi afln indonesia\" → baris 'Aset'\n\n")


@patch("paired_verifier.team_settings.for_check", return_value=({"posisi afln indonesia": "Aset"}, SETTINGS_BLOCK))
@patch("paired_verifier.parse_bi_table")
def test_the_settings_block_reaches_the_extraction_prompt(mock_parse, _for_check, monkeypatch):
    monkeypatch.setenv("STRICT_EXTRACTION", "1")
    mock_parse.return_value = _table()
    seen = []

    _verify(llm=_llm(seen), publication="pii")

    assert "- \"posisi afln indonesia\" → baris 'Aset'" in seen[0][1].content
    assert "PADANAN TIM" not in seen[0][1].content


@patch("ai_review.review_claims", new_callable=AsyncMock, side_effect=RuntimeError("down"))
@patch("paired_verifier.parse_bi_table")
def test_a_review_failure_keeps_the_verdicts(mock_parse, _review, monkeypatch):
    monkeypatch.setenv("STRICT_EXTRACTION", "1")
    mock_parse.return_value = _table()

    response = _verify(llm=_llm([]), review_llm=Mock())

    assert [r.verdict for r in response.results] == ["Entailed"]
    assert response.ai_notes == ["Tinjauan AI tidak selesai; semua klaim memakai vonis dari pencocokan kode."]


@patch("ai_review.review_claims", new_callable=AsyncMock)
@patch("paired_verifier.parse_bi_table")
def test_claims_the_review_left_unanswered_are_named(mock_parse, mock_review, monkeypatch):
    from ai_review import ReviewStats
    monkeypatch.setenv("STRICT_EXTRACTION", "1")
    mock_parse.return_value = _table()
    mock_review.side_effect = lambda results, *a, **k: (results, ReviewStats(candidates=3, answered=1, unanswered=2))

    response = _verify(llm=_llm([]), review_llm=Mock())

    assert response.ai_notes == ["Tinjauan AI tidak menjawab 2 klaim; klaim itu memakai vonis dari pencocokan kode."]


@patch("ai_review.review_claims", new_callable=AsyncMock)
@patch("paired_verifier.parse_bi_table")
def test_a_finished_review_leaves_no_note(mock_parse, mock_review, monkeypatch):
    from ai_review import ReviewStats
    monkeypatch.setenv("STRICT_EXTRACTION", "1")
    mock_parse.return_value = _table()
    mock_review.side_effect = lambda results, *a, **k: (results, ReviewStats(candidates=1, answered=1))

    assert _verify(llm=_llm([]), review_llm=Mock()).ai_notes == []


@patch("strict.pipeline.review_pointers", new_callable=AsyncMock, side_effect=RuntimeError("stalled"))
@patch("paired_verifier.parse_bi_table")
def test_a_pointer_review_failure_is_named(mock_parse, _pointers, monkeypatch):
    monkeypatch.setenv("STRICT_EXTRACTION", "1")
    mock_parse.return_value = _table()

    response = _verify(llm=_llm([]))

    assert [r.verdict for r in response.results] == ["Entailed"]
    assert response.ai_notes and response.ai_notes[0].startswith("Rujukan tabel/grafik")


@patch("paired_verifier.extract_structured_facts_async", new_callable=AsyncMock, return_value=[])
@patch("paired_verifier.parse_bi_table")
def test_a_category_sheet_picked_along_keeps_the_check_strict(mock_parse, mock_extract, monkeypatch):
    monkeypatch.setenv("STRICT_EXTRACTION", "1")
    items = BITableData(title="Suku Bunga", unit="%", row_labels=["Bank Persero"])
    items.axis_type, items.col_labels = "categorical", ["1 bulan"]
    items._data[("Bank Persero", "1 bulan")] = 0.0
    mock_parse.side_effect = [_table(), items]
    seen = []

    response = _verify(llm=_llm(seen), excel_sources=[(b"x", "5.39", "t.xls"), (b"y", "Suku Bunga", "ub.xlsx")])

    assert not mock_extract.called
    [result] = response.results
    assert (result.verdict, result.resolved_via) == ("Entailed", "strict")
    # The category table is shown too, with its columns in place of periods.
    assert "[S2: Suku Bunga — tabel kategori tanpa periode, kolom: '1 bulan'" in seen[0][1].content


RATES_TEXT = ("[== Halaman 1 ==]\nSuku bunga deposito Bank Persero tenor 1 bulan tercatat 7,5 persen, "
              "lebih rendah 0,4 persen dari tenor 3 bulan (selisih relatif 5,3 persen).\n")


def _category_llm(cells, operation="value", value="7,5", unit="persen"):
    claim = StrictClaim(anchor_quote=f"{value} persen", subject="suku bunga deposito Bank Persero", operation=operation,
                        claimed_value_raw=value, unit=unit, cells=cells)
    llm = Mock()
    llm.with_structured_output = Mock(return_value=RunnableLambda(lambda _p: StrictClaims(claims=[claim])))
    return llm


def _rates():
    items = BITableData(title="Suku Bunga", unit="%", row_labels=["Bank Persero", "BPD"])
    items.axis_type, items.col_labels = "categorical", ["1 bulan", "3 bulan"]
    items._data.update({("Bank Persero", "1 bulan"): 7.5, ("Bank Persero", "3 bulan"): 7.9,
                        ("BPD", "1 bulan"): 6.8, ("BPD", "3 bulan"): 7.0})
    return items


@patch("paired_verifier.parse_bi_table")
def test_a_category_table_alone_is_checked_by_strict(mock_parse):
    mock_parse.return_value = _rates()
    llm = _category_llm([CellRef(table="S1", row="Bank Persero", period="1 bulan")])

    response = _verify(llm=llm, excel_sources=[(b"y", "Suku Bunga", "ub.xlsx")],
                       narrative_text=RATES_TEXT)

    [result] = response.results
    assert (result.verdict, result.resolved_via) == ("Entailed", "strict")
    [cell] = result.periods
    assert (cell.metric_label, cell.col_label, cell.year, cell.excel_value) == ("Bank Persero", "1 bulan", None, 7.5)
    assert "S1 · Bank Persero · 1 bulan = 7.5" in result.reasoning


@patch("paired_verifier.parse_bi_table")
def test_a_category_table_compares_across_columns_but_has_no_change_over_time(mock_parse):
    mock_parse.return_value = _rates()
    diff = _category_llm([CellRef(table="S1", row="Bank Persero", period="1 bulan"),
                          CellRef(table="S1", row="Bank Persero", period="3 bulan")], operation="diff", value="0,4")
    yoy = _category_llm([CellRef(table="S1", row="Bank Persero", period="1 bulan"),
                         CellRef(table="S1", row="Bank Persero", period="3 bulan")], operation="yoy_growth", value="5,3")
    narrative = RATES_TEXT

    [by_diff] = _verify(llm=diff, excel_sources=[(b"y", "Suku Bunga", "ub.xlsx")], narrative_text=narrative).results
    [by_yoy] = _verify(llm=yoy, excel_sources=[(b"y", "Suku Bunga", "ub.xlsx")], narrative_text=narrative).results

    assert by_diff.verdict == "Entailed"
    assert (by_yoy.verdict, by_yoy.reason_code) == ("Inconclusive", "jenis")


@patch("paired_verifier.parse_bi_table")
def test_an_unknown_column_is_tidak_cukup_data(mock_parse):
    mock_parse.return_value = _rates()
    llm = _category_llm([CellRef(table="S1", row="Bank Persero", period="12 bulan")])

    [result] = _verify(llm=llm, excel_sources=[(b"y", "Suku Bunga", "ub.xlsx")],
                       narrative_text=RATES_TEXT).results

    assert (result.verdict, result.reason_code) == ("Inconclusive", "tidak_ditemukan")


def test_an_unreadable_sheet_is_named_in_the_notes():
    from paired_verifier import _ExcelSource
    from strict.tables import build_views, unreadable_sheets

    blank = BITableData(title="?", unit="", row_labels=[])
    unread = _ExcelSource(table=blank, filename="ub.xlsx", sheet="Rekap", pointer_only=True)
    series = _ExcelSource(table=_table(), filename="t.xls", sheet="5.39")

    assert [v.source.sheet for v in build_views([unread, series])] == ["5.39"]
    assert unreadable_sheets([unread, series]) == ["ub.xlsx › Rekap"]


def test_document_tables_alone_are_strict_views():
    from paired_verifier import _ExcelSource
    from strict.tables import build_views

    own = _ExcelSource(table=_table(), filename="Laporan.docx", sheet="Lampiran 1", origin="pdf")

    assert [v.code for v in build_views([own])] == ["S1"]


# --- two stages: the workbooks first, the documents' tables only for what they lack ----------------

TWO_SENTENCES = ("[== Halaman 1 ==]\nPosisi AFLN Indonesia pada triwulan IV 2025 tercatat 558,5 miliar dolar AS. "
                 "Kewajiban neto tercatat 272,6 miliar dolar AS.\n")


def _staged_llm(seen, excel_answer, document_answer):
    def _respond(prompt_value):
        messages = prompt_value.to_messages()
        seen.append(messages)
        return StrictClaims(claims=document_answer if "HANYA KALIMAT INI" in messages[1].content else excel_answer)

    llm = Mock()
    llm.with_structured_output = Mock(return_value=RunnableLambda(_respond))
    return llm


AFLN = StrictClaim(anchor_quote="tercatat 558,5 miliar dolar AS", subject="Posisi AFLN", operation="value",
                   claimed_value_raw="558,5", unit="miliar USD", cells=[CellRef(table="S1", row="Aset", period="Q4 2025")])
NETO_ABSENT = StrictClaim(anchor_quote="tercatat 272,6 miliar dolar AS", subject="Kewajiban neto", operation="value",
                          not_in_tables=True, reason="tidak ada di workbook")
NETO_IN_DOC = StrictClaim(anchor_quote="tercatat 272,6 miliar dolar AS", subject="Kewajiban neto", operation="value",
                          claimed_value_raw="272,6", unit="miliar USD",
                          cells=[CellRef(table="S2", row="Kewajiban neto", period="Q4 2025")])
AFLN_IN_DOC = StrictClaim(anchor_quote="tercatat 558,5 miliar dolar AS", subject="Posisi AFLN", operation="value",
                          claimed_value_raw="558,5", unit="miliar USD",
                          cells=[CellRef(table="S2", row="Kewajiban neto", period="Q4 2025")])


def _reference():
    from pdf_table_extraction import PdfTable
    grid = [["", "Q3 2025", "Q4 2025"], ["Kewajiban neto", 245.1, 272.6]]
    return ("Siaran Pers PII.docx", [PdfTable(page_number=1, caption="Tabel 1. PII", unit="Miliar USD", grid=grid)])


@patch("paired_verifier.parse_bi_table")
def test_a_figure_the_workbooks_lack_is_read_from_the_documents_tables(mock_parse, monkeypatch):
    monkeypatch.setenv("STRICT_EXTRACTION", "1")
    mock_parse.return_value = _table()
    seen = []

    response = _verify(narrative_text=TWO_SENTENCES, mode="both", reference_tables=[_reference()],
                       llm=_staged_llm(seen, [AFLN, NETO_ABSENT], [NETO_IN_DOC]))

    assert len(seen) == 2
    first, second = seen[0][1].content, seen[1][1].content
    assert "tabel di dokumen" not in first                       # stage 1: the workbooks only
    assert "tabel di dokumen lain (Siaran Pers PII.docx)" in second and "Posisi Investasi" not in second
    assert "Kewajiban neto tercatat 272,6 miliar dolar AS." in second
    verdicts = sorted((r.verdict, r.matched_excel_source.split(" / ")[0]) for r in response.results)
    assert verdicts == [("Entailed", "Siaran Pers PII.docx"), ("Entailed", "t.xls")]


@patch("paired_verifier.parse_bi_table")
def test_a_figure_the_workbooks_answer_never_goes_to_the_documents(mock_parse, monkeypatch):
    # Option B as first built let the model check M2 against the report's own table although SEKI
    # held it: a typo in that table, copied into the narrative, would then pass.
    monkeypatch.setenv("STRICT_EXTRACTION", "1")
    mock_parse.return_value = _table()
    seen = []

    response = _verify(narrative_text=TWO_SENTENCES, mode="both", reference_tables=[_reference()],
                       llm=_staged_llm(seen, [AFLN, NETO_ABSENT], [NETO_IN_DOC, AFLN_IN_DOC]))

    afln = [r for r in response.results if r.claimed_value == 558.5]
    assert len(afln) == 1 and afln[0].matched_excel_source.startswith("t.xls")


@patch("paired_verifier.parse_bi_table")
def test_no_second_stage_when_the_workbooks_answer_everything(mock_parse, monkeypatch):
    monkeypatch.setenv("STRICT_EXTRACTION", "1")
    mock_parse.return_value = _table()
    seen = []

    _verify(narrative_text=TWO_SENTENCES, mode="both", reference_tables=[_reference()],
            llm=_staged_llm(seen, [AFLN], [NETO_IN_DOC]))

    assert len(seen) == 1


@patch("paired_verifier.parse_bi_table")
def test_run_check_fills_chart_checks(mock_parse, monkeypatch):
    import chart_check
    from chart_images import ChartImage
    from schemas import ChartCheck

    mock_parse.return_value = _table()
    seen = {}

    async def fake_check(charts, scanned, views, llm, on_progress=None):
        seen["charts"], seen["codes"] = [c.caption for c in charts], [v.code for v in views]
        return ([ChartCheck(caption="Grafik 1", title="", verdict="Entailed", label_count=1, entailed_count=1,
                            refuted_count=0, inconclusive_count=0, drawing="masuk akal")], ["Grafik 2"])

    monkeypatch.setattr(chart_check, "check_charts", fake_check)

    response = _verify(llm=_llm([]), charts=[ChartImage("Grafik 1", "", None, b"png"), ChartImage("Grafik 2", "", None, b"png")])

    assert seen == {"charts": ["Grafik 1", "Grafik 2"], "codes": ["S1"]}
    assert [c.caption for c in response.chart_checks] == ["Grafik 1"]
    assert any("Grafik 2" in note for note in response.ai_notes)


@patch("paired_verifier.parse_bi_table")
def test_no_charts_means_no_chart_check(mock_parse, monkeypatch):
    import chart_check
    mock_parse.return_value = _table()
    monkeypatch.setattr(chart_check, "check_charts", Mock(side_effect=AssertionError("called")))

    response = _verify(llm=_llm([]))

    assert response.chart_checks == []


@patch("paired_verifier.parse_bi_table")
def test_the_chart_check_stops_when_the_check_is_cancelled(mock_parse, monkeypatch):
    # Batalkan cancels the pipeline; the chart task must not go on calling the model (credits).
    import time
    import chart_check
    from chart_images import ChartImage
    from paired_verifier import verify_paired

    mock_parse.return_value = _table()
    state = {"cancelled": False}

    async def slow_check(charts, scanned, views, llm, on_progress=None):
        state["started"].set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            state["cancelled"] = True
            raise
        return [], []

    monkeypatch.setattr(chart_check, "check_charts", slow_check)

    def slow_extraction(prompt_value):
        time.sleep(1.0)                                   # extraction still running when Batalkan comes
        return StrictClaims(claims=[])

    llm = Mock()
    llm.with_structured_output = Mock(return_value=RunnableLambda(slow_extraction))

    async def scenario():
        state["started"] = asyncio.Event()
        check = asyncio.create_task(verify_paired(narrative_text=NARRATIVE, excel_sources=[(b"x", "5.39", "t.xls")],
                                                  llm=llm, charts=[ChartImage("Grafik 1", "", None, b"png")]))
        await state["started"].wait()
        await asyncio.sleep(0.05)
        check.cancel()
        with pytest.raises(asyncio.CancelledError):
            await check
        await asyncio.sleep(0.05)
        return state["cancelled"]                         # before asyncio.run cancels what is left

    assert asyncio.run(scenario()) is True
