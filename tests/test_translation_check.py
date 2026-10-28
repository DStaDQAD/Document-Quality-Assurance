"""Cek Terjemahan: an English publication against its Indonesian version, paragraph by paragraph.
Code compares the numbers of each pair; the LLM looks for differences in meaning."""
import asyncio
from collections import Counter
from unittest.mock import Mock

from langchain_core.runnables import RunnableLambda

from translation_check import (MeaningFinding, MeaningFindings, Pair, align, check_translation, number_finding,
                               numbers, paragraphs)

AUG_ID = ("Suku bunga simpanan berjangka mengalami kenaikan pada tenor 1 bulan, 3 bulan, 6 bulan, dan 24 bulan, "
          "masing-masing menjadi sebesar 5,1%, 5,8%, 5,6%, dan 4,2% dibandingkan bulan sebelumnya sebesar "
          "5,0%, 5,5%, 5,4%, dan 5,8% (Grafik 6).")
AUG_EN = ("Time deposit rates increased across the 1-month, 3-month, 6-month and 24-month tenors to 5.1%, 5.8%, "
          "5.6% and 4.2%, respectively, from 5.0%, 5.5%, 5.4% and 4.0% in the previous month (Graph 6).")


def test_numbers_read_each_language_format():
    assert numbers("Rp10.448,1 triliun atau 8,2% (yoy)", "id") == Counter([10448.1, 8.2])
    assert numbers("Rp10,448.1 trillion or 8.2% (yoy)", "en") == Counter([10448.1, 8.2])
    assert numbers("sebesar 9.388", "id") == Counter([9388.0])


def test_quarters_and_ordinals_are_not_numbers_in_english():
    assert numbers("pada Triwulan II tumbuh 5,1%", "id") == Counter([5.1])
    assert numbers("grew 5.1% in Q2, the 2nd release", "en") == Counter([5.1])


def test_capitalised_section_headings_are_not_compared():
    # UB Agustus 2026: "PERKEMBANGAN DANA PIHAK KETIGA (DPK)" is "DEPOSITS" in the English report.
    assert paragraphs("PERKEMBANGAN DANA PIHAK KETIGA (DPK)\nDPK tumbuh 4,9%.") == ["DPK tumbuh 4,9%."]


def test_signs_do_not_count():
    assert numbers("turun −0,5% dan -1,2%", "id") == numbers("fell 0.5% and 1.2%", "en")


def test_the_august_deposit_rate_paragraph_is_a_number_finding():
    f = number_finding(Pair(0, AUG_ID, AUG_EN))
    assert f.kind == "angka" and f.only_id == ["5,8"] and f.only_en == ["4.0"]


def test_same_numbers_in_another_order_is_no_finding():
    assert number_finding(Pair(0, "M2 tumbuh 8,2% setelah 8,3% pada Juli", "Following 8.3% in July, M2 grew 8.2%")) is None


def test_a_missing_middle_paragraph_does_not_shift_later_pairs():
    ids = ["A naik 1,1.", "B naik 2,2.", "C naik 3,3.", "D naik 4,4."]
    ens = ["A rose 1.1.", "C rose 3.3.", "D rose 4.4."]
    pairs = align(ids, ens)
    assert [(p.id_text, p.en_text) for p in pairs] == [
        (ids[0], ens[0]), (ids[1], None), (ids[2], ens[1]), (ids[3], ens[2])]


def test_a_one_sided_paragraph_is_a_finding_of_its_own():
    assert number_finding(Pair(1, "B naik 2,2.", None)).only_id == ["2,2"]
    lone = number_finding(Pair(1, None, "Broad money remained solid."))
    assert lone.kind == "makna" and "EN" in lone.explanation


def test_trivial_paragraphs_and_page_markers_are_dropped():
    assert paragraphs("[== Halaman 1 ==]\nJudul Uang Beredar\ns\n\nM2 tumbuh 8,2%.") == ["Judul Uang Beredar", "M2 tumbuh 8,2%."]


def test_a_date_line_is_trivial_but_a_short_line_with_a_figure_is_not():
    # UB reports put "Agustus 2026" after the title in ID and "August 2026" before it in EN.
    assert paragraphs("Agustus 2026\nUang Beredar\nM2 8,2%") == ["M2 8,2%"]


def test_years_are_not_compared():
    # EN often names the month where ID says "bulan sebelumnya": "in July 2026".
    assert number_finding(Pair(0, "naik dibanding bulan sebelumnya sebesar 13,0%",
                                  "up from 13.0% in July 2026")) is None


def test_an_indonesian_number_in_the_english_text_is_read_and_flagged_as_format():
    f = number_finding(Pair(0, "tercatat sebesar Rp10.432,8 triliun", "reached Rp10.432,8 trillion"))
    assert f.kind == "angka" and f.only_id == [] and f.only_en == ["10.432,8"]
    assert "format" in f.explanation.lower()


# --- the LLM meaning pass and the report --------------------------------------------------------

ID_DOC = ("M1 tumbuh 9,0% dan uang kuasi 6,5% pada Agustus.\n"
          "Tenor 24 bulan naik dari 5,8% menjadi 4,2% pada bulan ini.\n"
          "Kredit tumbuh positif pada bulan ini secara umum.")
EN_DOC = ("M1 grew 6.5% and quasi-money 9.0% in August.\n"
          "The 24-month tenor rose from 4.0% to 4.2% this month.\n"
          "Loans grew positively this month in general.")


def _llm(answers, seen=None):
    def _respond(prompt_value):
        if seen is not None:
            seen.append(prompt_value.to_messages())
        return MeaningFindings(findings=list(answers))
    llm = Mock()
    llm.with_structured_output = Mock(return_value=RunnableLambda(_respond))
    return llm


def _failing_llm():
    def _respond(prompt_value):
        raise RuntimeError("provider down")
    llm = Mock()
    llm.with_structured_output = Mock(return_value=RunnableLambda(_respond))
    return llm


def _check(llm, **kw):
    return asyncio.run(check_translation(ID_DOC, EN_DOC, llm, document="laporan",
                                         id_filename="id.docx", en_filename="en.docx", **kw))


def test_meaning_findings_land_on_their_pair_and_numbers_are_still_checked_by_code():
    report = _check(_llm([MeaningFinding(pair=0, id_quote="M1 tumbuh 9,0%", en_quote="M1 grew 6.5%",
                                         explanation="Angka M1 dan uang kuasi tertukar")]))
    assert [f.kind for f in report.pairs[0].findings] == ["makna"]
    assert [f.kind for f in report.pairs[1].findings] == ["angka"]
    assert report.pairs[2].findings == []
    assert (report.n_number, report.n_meaning, report.n_unchecked) == (1, 1, 0)


def test_a_failed_batch_leaves_its_pairs_unchecked_but_keeps_number_findings():
    report = _check(_failing_llm())
    assert report.n_unchecked == len(report.pairs) == 3 and report.n_number == 1
    assert all(not p.checked for p in report.pairs)


def test_pairs_are_batched_under_max_chars():
    seen = []
    _check(_llm([], seen), max_chars=120)
    assert len(seen) == 3


def test_the_prompt_shows_each_pair_and_asks_for_every_number():
    seen = []
    _check(_llm([], seen))
    system, human = seen[0][0].content, seen[0][1].content
    assert "every number" in system.lower()
    assert "[PAIR 1]\nID: Tenor 24 bulan" in human and "EN: The 24-month tenor" in human


def test_a_finding_for_a_pair_outside_its_batch_is_dropped():
    report = _check(_llm([MeaningFinding(pair=7, id_quote="x", en_quote="y", explanation="z")]))
    assert report.n_meaning == 0
