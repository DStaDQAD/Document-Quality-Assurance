"""Strict extraction: the prompt shows the tables without values, and each answer becomes a Claim
whose cells are resolved against the tables and whose sentence and page come from the text."""
import asyncio
from unittest.mock import Mock

from langchain_core.runnables import RunnableLambda

from excel_parser_bi import BITableData
from paired_verifier import _ExcelSource
from strict.claims import CellRef, StrictClaim, StrictClaims
from strict.extract import extract_claims
from strict.tables import build_views

NARRATIVE = ("[== Halaman 7 ==]\nHal tersebut didorong oleh peningkatan posisi AFLN investasi lainnya "
             "sebesar 233 juta dolar AS. Kontraksi penjualan tercatat sebesar 9,0% (yoy).\n")


def _views():
    rows = ["Aset", "Aset > Investasi Lainnya"]
    table = BITableData(title="Posisi Investasi Internasional", unit="Juta USD", row_labels=rows)
    for row in rows:
        table._data[(row, 2025, "Q3")] = 216664.6201
        table._data[(row, 2025, "Q4")] = 216897.8201
    return build_views([_ExcelSource(table=table, filename="t.xls", sheet="5.39")])


def _llm(answers, seen):
    def _respond(prompt_value):
        seen.append(prompt_value.to_messages())
        return StrictClaims(claims=list(answers))

    llm = Mock()
    llm.with_structured_output = Mock(return_value=RunnableLambda(_respond))
    return llm


def _diff(**overrides):
    fields = dict(anchor_quote="sebesar 233 juta dolar AS", subject="AFLN investasi lainnya",
                  operation="diff", claimed_value_raw="233", unit="juta USD",
                  cells=[CellRef(table="S1", row="aset > investasi lainnya", period="Triwulan III 2025"),
                         CellRef(table="S1", row="Aset > Investasi Lainnya", period="Q4 2025")])
    fields.update(overrides)
    return StrictClaim(**fields)


def _run(answers, **kw):
    seen = []
    claims = asyncio.run(extract_claims(NARRATIVE, _views(), _llm(answers, seen), **kw))
    return claims, seen


def test_the_prompt_has_the_tables_without_values():
    _, seen = _run([])

    human = seen[0][1].content
    assert "[S1: Posisi Investasi Internasional — per triwulan, Q3 2025 s.d. Q4 2025, satuan Juta USD]" in human
    assert "- Aset > Investasi Lainnya" in human
    assert "216664" not in human and "216.664" not in human and "216897" not in human


def test_cells_resolve_to_rows_and_periods():
    [claim], _ = _run([_diff()])

    assert [(c.row, c.key) for c in claim.cells] == [
        ("Aset > Investasi Lainnya", (2025, "Q3")), ("Aset > Investasi Lainnya", (2025, "Q4"))]
    assert claim.cells[0].row_text == "aset > investasi lainnya"


def test_an_unknown_code_row_or_period_stays_unresolved():
    [claim], _ = _run([_diff(cells=[CellRef(table="S9", row="Aset", period="Q4 2025"),
                                    CellRef(table="S1", row="AFLN", period="Q4 2025"),
                                    CellRef(table="S1", row="Aset", period="akhir tahun")])])

    assert [(c.row, c.key) for c in claim.cells] == [(None, (2025, "Q4")), (None, (2025, "Q4")),
                                                     ("Aset", None)]


def test_the_sentence_and_page_come_from_the_text():
    [claim], _ = _run([_diff()])

    assert claim.context_quote == ("Hal tersebut didorong oleh peningkatan posisi AFLN investasi lainnya "
                                   "sebesar 233 juta dolar AS.")
    assert claim.page_number == 7 and claim.claimed_value == 233.0 and claim.unit == "juta USD"


def test_a_contraction_keeps_its_minus():
    [claim], _ = _run([StrictClaim(anchor_quote="sebesar 9,0% (yoy)", subject="penjualan", operation="value",
                                   claimed_value_raw="-9,0", unit="persen",
                                   cells=[CellRef(table="S1", row="Aset", period="Q4 2025")])])

    assert claim.claimed_value == -9.0


def test_not_in_tables_needs_no_cells_or_number():
    [claim], _ = _run([StrictClaim(anchor_quote="sebesar 9,0% (yoy)", subject="penjualan", operation="value",
                                   not_in_tables=True, reason="penjualan tidak ada di tabel")])

    assert claim.not_in_tables and claim.cells == [] and claim.claimed_value is None
    assert claim.reason == "penjualan tidak ada di tabel"


def test_a_number_claim_without_a_readable_number_is_dropped():
    claims, _ = _run([_diff(claimed_value_raw=None), _diff(claimed_value_raw="dua ratus")])

    assert claims == []


def test_notes_and_hints_reach_every_chunk():
    two_pages = NARRATIVE + "[== Halaman 8 ==]\nIPR Juni 2026 tercatat sebesar 225,0 dengan kenaikan.\n"
    seen = []
    asyncio.run(extract_claims(two_pages, _views(), _llm([], seen), notes="PUBLIKASI: PII",
                               hints="\"ipr\" → 'INDEKS TOTAL'", max_chars_per_chunk=150))

    assert len(seen) == 2
    assert all("PUBLIKASI: PII" in m[1].content and "\"ipr\" → 'INDEKS TOTAL'" in m[1].content for m in seen)
    assert "RELATIVE PERIODS" in seen[0][0].content
