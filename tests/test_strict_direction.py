"""A trend's direction comes from the words the extractor copied out of the sentence, not from its
reading of them: SKDU Tw III-2026's "1,84% …, meski lebih tinggi dibandingkan SB 2,45%" came back
is_decreasing and passed as Sesuai."""
import asyncio
from unittest.mock import Mock

from langchain_core.runnables import RunnableLambda

from excel_parser_bi import BITableData
from paired_verifier import _ExcelSource
from strict.claims import CellRef, StrictClaim, StrictClaims, claim_from_dict, claim_to_dict
from strict.direction import direction_of, in_sentence
from strict.evaluate import evaluate_claim
from strict.extract import extract_claims
from strict.prompt import SYSTEM_PROMPT
from strict.tables import build_views

SENTENCE = ("Hal ini tecermin dari SB akses kredit sebesar 1,84% pada triwulan III 2026, meski lebih tinggi "
            "dibandingkan SB 2,45% pada triwulan II 2026.")
NARRATIVE = f"[== Halaman 1 ==]\n{SENTENCE}\n"


def _views():
    table = BITableData(title="Keuangan dan Akses Kredit", unit="%", row_labels=["Saldo Bersih"])
    table._data[("Saldo Bersih", 2026, "Q2")] = 2.45
    table._data[("Saldo Bersih", 2026, "Q3")] = 1.84
    return build_views([_ExcelSource(table=table, filename="t.xlsx", sheet="T3")])


def _trend(operation, words):
    return StrictClaim(anchor_quote="meski lebih tinggi dibandingkan SB", subject="SB akses kredit",
                       operation=operation, direction_words=words,
                       cells=[CellRef(table="S1", row="Saldo Bersih", period="Q2 2026"),
                              CellRef(table="S1", row="Saldo Bersih", period="Q3 2026")])


def _extract(*answers):
    llm = Mock()
    llm.with_structured_output = Mock(return_value=RunnableLambda(lambda _p: StrictClaims(claims=list(answers))))
    views = _views()
    return asyncio.run(extract_claims(NARRATIVE, views, llm)), views


def test_the_words_name_the_direction():
    assert direction_of("lebih tinggi dibandingkan") == "is_increasing"
    assert direction_of("meski lebih rendah dari") == "is_decreasing"
    assert direction_of("melambat") == "is_decreasing"
    assert direction_of("relatif stabil") == "is_stable"
    assert direction_of("tidak berubah") == "is_stable"


def test_no_direction_from_a_negation_both_ways_or_nothing():
    assert direction_of("tidak meningkat") is None
    assert direction_of("meningkat, sedangkan lainnya menurun") is None
    assert direction_of("") is None and direction_of(None) is None
    assert direction_of("berada pada level optimis") is None


def test_words_are_found_whatever_their_case_and_spacing():
    assert in_sentence("Lebih  tinggi dibandingkan", SENTENCE)
    assert not in_sentence("menurun", SENTENCE)


def test_a_trend_read_against_its_words_takes_the_words_direction_and_is_tidak_sesuai():
    [claim], views = _extract(_trend("is_decreasing", "lebih tinggi dibandingkan"))
    assert claim.operation == "is_increasing" and claim.extractor_operation == "is_decreasing"
    result = evaluate_claim(claim, views)
    assert result.verdict == "Refuted" and result.operation == "is_increasing"
    assert "Arah dibaca dari kata kalimat 'lebih tinggi dibandingkan' (naik)" in result.reasoning
    assert "AI membacanya sebagai turun" in result.reasoning


def test_a_trend_that_agrees_with_its_words_is_unchanged():
    [claim], _ = _extract(_trend("is_increasing", "lebih tinggi dibandingkan"))
    assert claim.operation == "is_increasing" and claim.extractor_operation is None
    assert claim.direction_words == "lebih tinggi dibandingkan"


def test_words_that_are_not_in_the_sentence_decide_nothing():
    [claim], _ = _extract(_trend("is_decreasing", "menurun"))
    assert claim.operation == "is_decreasing" and claim.extractor_operation is None


def test_a_trend_without_words_keeps_the_extractors_direction():
    [claim], _ = _extract(_trend("is_decreasing", None))
    assert claim.operation == "is_decreasing" and claim.extractor_operation is None


def test_the_words_survive_the_saved_claim_and_an_old_saved_claim_still_loads():
    [claim], _ = _extract(_trend("is_decreasing", "lebih tinggi dibandingkan"))
    assert claim_from_dict(claim_to_dict(claim)) == claim
    old = claim_to_dict(claim)
    del old["direction_words"], old["extractor_operation"]
    assert claim_from_dict(old).extractor_operation is None


def test_the_prompt_says_direction_words_are_copied_never_corrected():
    assert "DIRECTION WORDS ARE COPIED, NEVER CORRECTED" in SYSTEM_PROMPT
    assert "even when the sentence's own numbers say the opposite" in SYSTEM_PROMPT
    fields = list(StrictClaim.model_fields)
    assert fields.index("direction_words") < fields.index("operation")
