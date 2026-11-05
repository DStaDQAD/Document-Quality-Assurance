"""Cek Terjemahan: an English publication against its Indonesian version, paragraph by paragraph.
Code compares the numbers of each pair; the LLM looks for differences in meaning."""
import asyncio
from collections import Counter
from unittest.mock import Mock

import pytest
from langchain_core.runnables import RunnableLambda

from translation_check import (HeadingFinding, HeadingFindings, MeaningFinding, MeaningFindings, Pair,
                               StretchAnswer, StretchAnswers, StretchLink, align, align_by_meaning,
                               check_translation, located_headings, number_finding, numbers, paragraphs)

AUG_ID = ("Suku bunga simpanan berjangka mengalami kenaikan pada tenor 1 bulan, 3 bulan, 6 bulan, dan 24 bulan, "
          "masing-masing menjadi sebesar 5,1%, 5,8%, 5,6%, dan 4,2% dibandingkan bulan sebelumnya sebesar "
          "5,0%, 5,5%, 5,4%, dan 5,8% (Grafik 6).")
AUG_EN = ("Time deposit rates increased across the 1-month, 3-month, 6-month and 24-month tenors to 5.1%, 5.8%, "
          "5.6% and 4.2%, respectively, from 5.0%, 5.5%, 5.4% and 4.0% in the previous month (Graph 6).")


def test_numbers_read_each_language_format():
    assert numbers("Rp10.448,1 triliun atau 8,2% (yoy)", "id") == Counter([10448.1, 8.2])
    assert numbers("Rp10,448.1 trillion or 8.2% (yoy)", "en") == Counter([10448.1, 8.2])
    assert numbers("sebesar 9.388", "id") == Counter([9388.0])


def test_a_dollar_prefix_glued_to_the_figure_is_read():
    # PII Tw II-2026 English: "stood at USD197.4 billion, down from USD223.0 billion".
    assert numbers("stood at USD197.4 billion, down from US$223.0 billion", "en") == Counter([197.4, 223.0])
    assert numbers("sebesar 197,4 miliar dolar AS", "id") == Counter([197.4])


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
    assert paragraphs("[== Halaman 1 ==]\nJudul uang beredar bulan ini\ns\n\nM2 tumbuh 8,2%.") == [
        "Judul uang beredar bulan ini", "M2 tumbuh 8,2%."]


def test_title_case_headings_are_kept_out_of_the_pairing():
    # SKDU Tw III-2026: the versions title sections and charts differently; with no number to go by
    # these lines paired by position and slid out of step, so headings are checked apart.
    text = ("Kapasitas Produksi Terpakai\nKapasitas produksi terpakai tetap terjaga sebesar 73,75%.\n"
            "Lapangan Usaha Perdagangan Besar dan Eceran, dan Reparasi Mobil dan Motor\n"
            "Sumber: SKDU (BI), PDB (BPS)\n**Sektor Lainnya mencakup Pemda, Koperasi, Yayasan\n"
            "Kegiatan usaha pada triwulan III terindikasi tetap terjaga")
    assert paragraphs(text) == ["Kapasitas produksi terpakai tetap terjaga sebesar 73,75%.",
                                "**Sektor Lainnya mencakup Pemda, Koperasi, Yayasan",
                                "Kegiatan usaha pada triwulan III terindikasi tetap terjaga"]
    assert [h.text for h in located_headings(text)] == [
        "Kapasitas Produksi Terpakai", "Lapangan Usaha Perdagangan Besar dan Eceran, dan Reparasi Mobil dan Motor",
    ]


def test_a_title_naming_a_quarter_is_still_a_heading():
    # PII Tw II-2026: "… Decreased in Q2/2026" kept its "2" and stayed a paragraph, shifting every pair.
    text = ("Indonesia's Net Liability International Investment Position Decreased in Q2/2026\n"
            "At the end of the second quarter of 2026, net liability stood at USD197.4 billion.")
    assert paragraphs(text) == ["At the end of the second quarter of 2026, net liability stood at USD197.4 billion."]
    assert [h.text for h in located_headings(text)] == [
        "Indonesia's Net Liability International Investment Position Decreased in Q2/2026"]
    # A line with a real figure stays a paragraph.
    assert paragraphs("LU Pengadaan Listrik dan Gas (80,89%)") == ["LU Pengadaan Listrik dan Gas (80,89%)"]


def test_extra_headings_on_one_side_do_not_shift_the_sentences():
    ids = paragraphs("Kinerja Lapangan Usaha\nLapangan Usaha Pertanian\nPerkembangan Kegiatan Usaha LU Pertanian\n"
                     "Kegiatan usaha LU Pertanian tetap tumbuh dan diprakirakan berlanjut.\n"
                     "Perkembangan Tenaga Kerja\nLU Pertambangan tumbuh 0,46%.")
    ens = paragraphs("Agricultural Sector\nAgriculture activity kept growing and is expected to continue.\n"
                     "Mining grew 0.46%.")
    assert [(p.id_text[:12], p.en_text[:12]) for p in align(ids, ens)] == [
        ("Kegiatan usa", "Agriculture "), ("LU Pertamban", "Mining grew ")]


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


def _llm(answers, seen=None, headings=(), heading_seen=None, stretches=None, stretch_seen=None):
    """A model that answers the meaning pass with `answers`, the heading check with `headings` and
    the stretch pairing with `stretches` (None: it answers no stretch)."""
    def _structured(schema):
        def _respond(prompt_value):
            if schema is StretchAnswers:
                if stretch_seen is not None:
                    stretch_seen.append(prompt_value.to_messages())
                if isinstance(stretches, Exception):
                    raise stretches
                return StretchAnswers(stretches=list(stretches or ()))
            if schema is HeadingFindings:
                if heading_seen is not None:
                    heading_seen.append(prompt_value.to_messages())
                return HeadingFindings(findings=list(headings))
            if seen is not None:
                seen.append(prompt_value.to_messages())
            return MeaningFindings(findings=list(answers))
        return RunnableLambda(_respond)
    llm = Mock()
    llm.with_structured_output = Mock(side_effect=_structured)
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
    # UB Agu plant: "giro valas" rendered as "foreign currency time deposits" was missed without it.
    assert "giro = demand deposits" in system
    # Riwayat run 2026-10-28: "Third Party Deposits" for DPK was flagged as a wrong term.
    assert "another English term for the same component" in system and "is not a difference" in system
    # UB Jul real report: M0's two drivers named in reverse order in EN was once flagged.
    assert "listed in another order" in system
    assert "[PAIR 1]\nID: Tenor 24 bulan" in human and "EN: The 24-month tenor" in human


def test_a_finding_for_a_pair_outside_its_batch_is_dropped():
    report = _check(_llm([MeaningFinding(pair=7, id_quote="x", en_quote="y", explanation="z")]))
    assert report.n_meaning == 0


def test_a_document_with_no_readable_narrative_is_an_error_not_a_clean_result():
    # A scanned PDF with no text layer reads as nothing; "no differences" would look clean.
    with pytest.raises(ValueError, match="versi Inggris"):
        asyncio.run(check_translation(ID_DOC, "[== Halaman 1 ==]\n", _llm([]), document="laporan",
                                      id_filename="id.docx", en_filename="en.pdf"))


def test_a_pdf_paragraph_carries_its_page():
    id_pdf = "[== Halaman 1 ==]\nM2 tumbuh 8,2% pada bulan ini.\n[== Halaman 2 ==]\nTenor 24 bulan naik dari 5,8% menjadi 4,2%."
    en_pdf = "[== Halaman 1 ==]\nM2 grew 8.2% this month.\n[== Halaman 3 ==]\nThe 24-month tenor rose from 4.0% to 4.2%."
    report = asyncio.run(check_translation(id_pdf, en_pdf, _llm([]), document="laporan",
                                           id_filename="id.pdf", en_filename="en.pdf"))
    assert [(p.id_page, p.en_page) for p in report.pairs] == [(1, 1), (2, 3)]
    assert report.pairs[1].id_section is None


def test_a_word_paragraph_carries_its_section_not_its_page():
    # Word has no fixed pages: its [== Halaman N ==] blocks are not pages (UB Agu: off by up to 9).
    id_word = ("[== Halaman 1 ==]\nPERKEMBANGAN SUKU BUNGA\nTenor 24 bulan naik dari 5,8% menjadi 4,2%.\n"
               "[== Halaman 2 ==]\nPERKEMBANGAN KREDIT\nKredit tumbuh 13,3% pada bulan ini.")
    en_word = ("LENDING AND DEPOSIT RATES\nThe 24-month tenor rose from 4.0% to 4.2%.\n"
               "LOANS\nLoans grew 13.3% this month.")
    report = asyncio.run(check_translation(id_word, en_word, _llm([]), document="laporan", id_filename="id.docx",
                                           en_filename="en.docx", id_is_word=True, en_is_word=True))
    assert [(p.id_section, p.en_section) for p in report.pairs] == [
        ("PERKEMBANGAN SUKU BUNGA", "LENDING AND DEPOSIT RATES"), ("PERKEMBANGAN KREDIT", "LOANS")]
    assert report.pairs[0].id_page is None


# --- the heading check -------------------------------------------------------------------------

ID_HEADED = ("PERKEMBANGAN KEGIATAN USAHA\nLapangan Usaha Pengadaan Listrik dan Gas\n"
             "Kapasitas produksi LU listrik tercatat 80,89%.\nPerkembangan Kapasitas Utilisasi")
EN_HEADED = "BUSINESS ACTIVITY\nElectricity Supply Sector\nCapacity utilisation in electricity was 80.89%."


def _headed(llm):
    return asyncio.run(check_translation(ID_HEADED, EN_HEADED, llm, document="laporan", id_filename="id.docx",
                                         en_filename="en.docx", id_is_word=True, en_is_word=True))


def test_a_heading_the_model_finds_different_is_a_pair_of_its_own():
    # SKDU Tw III-2026: "LU Pengadaan Listrik dan Gas" is "Electricity Supply" in the English report.
    seen = []
    report = _headed(_llm([], headings=[HeadingFinding(id_no=2, en_no=2, explanation="Versi EN tidak menyebut gas.")],
                          heading_seen=seen))
    assert [(p.id_text, p.en_text) for p in report.pairs] == [
        ("Kapasitas produksi LU listrik tercatat 80,89%.", "Capacity utilisation in electricity was 80.89%."),
        ("Lapangan Usaha Pengadaan Listrik dan Gas", "Electricity Supply Sector")]
    heading = report.pairs[1]
    assert heading.index == 1 and heading.id_section == "Judul" and heading.findings[0].kind == "makna"
    assert report.n_meaning == 1
    lists = seen[0][1].content
    assert "1. PERKEMBANGAN KEGIATAN USAHA" in lists and "3. Perkembangan Kapasitas Utilisasi" in lists
    assert "2. Electricity Supply Sector" in lists
    assert "a heading with no counterpart" in seen[0][0].content.lower()


def test_headings_go_to_the_heading_model_when_given():
    main_seen, review_seen = [], []
    asyncio.run(check_translation(ID_HEADED, EN_HEADED, _llm([], heading_seen=main_seen), document="laporan",
                                  id_filename="id.docx", en_filename="en.docx",
                                  heading_llm=_llm([], heading_seen=review_seen)))
    assert main_seen == [] and len(review_seen) == 1


def test_a_heading_finding_numbered_outside_the_lists_is_dropped():
    report = _headed(_llm([], headings=[HeadingFinding(id_no=9, en_no=1, explanation="x")]))
    assert len(report.pairs) == 1 and report.n_meaning == 0


def test_a_failed_heading_check_is_one_unchecked_entry():
    def _structured(schema):
        def _respond(prompt_value):
            if schema is HeadingFindings:
                raise RuntimeError("provider down")
            return MeaningFindings(findings=[])
        return RunnableLambda(_respond)
    llm = Mock()
    llm.with_structured_output = Mock(side_effect=_structured)

    report = _headed(llm)

    assert report.n_unchecked == 1 and report.pairs[-1].checked is False
    assert report.pairs[-1].id_text.startswith("Judul")


def test_no_heading_on_one_side_asks_nothing():
    seen = []
    asyncio.run(check_translation(ID_HEADED, "Capacity utilisation in electricity was 80.89%.",
                                  _llm([], heading_seen=seen), document="laporan", id_filename="id.docx",
                                  en_filename="en.docx"))
    assert seen == []


# --- the stretches between number anchors, paired by the model -------------------------------------

STRETCH_ID = ["M2 tumbuh 8,2% pada bulan ini.", "Kredit tumbuh positif secara umum pada bulan ini.",
              "DPK tumbuh stabil secara umum pada bulan ini.", "M1 tumbuh 9,0% pada bulan ini."]
STRETCH_EN = ["M2 grew 8.2% this month.", "A note printed only in the English version here.",
              "Loans grew positively in general this month.", "Deposits grew steadily in general this month.",
              "M1 grew 9.0% this month."]


def _by_meaning(llm):
    return asyncio.run(align_by_meaning(STRETCH_ID, STRETCH_EN, llm))


def _texts(pairs):
    return [(p.id_text, p.en_text) for p in pairs]


def test_a_line_only_one_side_prints_no_longer_shifts_the_pairs_after_it():
    # PII Tw II-2026: one English line read as a paragraph put the next six pairs out of step.
    seen = []
    pairs = _by_meaning(_llm([], stretches=[StretchAnswer(stretch=1, pairs=[
        StretchLink(id_no=1, en_no=2), StretchLink(id_no=2, en_no=3)])], stretch_seen=seen))
    assert _texts(pairs) == [(STRETCH_ID[0], STRETCH_EN[0]), (None, STRETCH_EN[1]), (STRETCH_ID[1], STRETCH_EN[2]),
                             (STRETCH_ID[2], STRETCH_EN[3]), (STRETCH_ID[3], STRETCH_EN[4])]
    assert [p.index for p in pairs] == [0, 1, 2, 3, 4]
    assert [(p.id_pos, p.en_pos) for p in pairs][1:3] == [(None, 1), (1, 2)]
    human = seen[0][1].content
    # Whole paragraphs, the anchors around the stretch as context, the stretch's own numbering.
    assert "[STRETCH 1]" in human and "BEFORE (paired): ID: M2 tumbuh 8,2% pada bulan ini." in human
    assert "ID 2: DPK tumbuh stabil secara umum pada bulan ini." in human
    assert "EN 1: A note printed only in the English version here." in human
    assert "AFTER (paired): EN: M1 grew 9.0% this month." in human
    assert "pairs never cross" in seen[0][0].content


def test_anchors_are_never_sent_and_one_against_one_asks_nothing():
    seen = []
    pairs = asyncio.run(align_by_meaning([STRETCH_ID[0], STRETCH_ID[1], STRETCH_ID[3]],
                                         [STRETCH_EN[0], STRETCH_EN[2], STRETCH_EN[4]], _llm([], stretch_seen=seen)))
    assert len(pairs) == 3 and seen == []  # Kredit/Loans stands between two anchors alone: sure enough


def test_a_failed_pairing_call_keeps_the_pairing_by_position():
    assert _texts(_by_meaning(_llm([], stretches=RuntimeError("provider down")))) == _texts(align(STRETCH_ID, STRETCH_EN))


def test_a_stretch_the_model_does_not_answer_keeps_its_pairing_by_position():
    assert _texts(_by_meaning(_llm([], stretches=[]))) == _texts(align(STRETCH_ID, STRETCH_EN))


def test_a_crossing_or_out_of_range_pair_from_the_model_is_dropped():
    pairs = _by_meaning(_llm([], stretches=[StretchAnswer(stretch=1, pairs=[
        StretchLink(id_no=2, en_no=1), StretchLink(id_no=1, en_no=3), StretchLink(id_no=5, en_no=1)])]))
    # (1, 3) is kept first in reading order; (2, 1) would cross it; ID 5 does not exist.
    assert _texts(pairs)[1:5] == [(None, STRETCH_EN[1]), (None, STRETCH_EN[2]), (STRETCH_ID[1], STRETCH_EN[3]),
                                  (STRETCH_ID[2], None)]
    assert sum(1 for p in pairs if p.id_text) == 4 and sum(1 for p in pairs if p.en_text) == 5


def test_the_report_pairs_with_the_stretch_answer():
    id_doc, en_doc = "\n".join(STRETCH_ID), "\n".join(STRETCH_EN)
    report = asyncio.run(check_translation(id_doc, en_doc, _llm([], stretches=[StretchAnswer(stretch=1, pairs=[
        StretchLink(id_no=1, en_no=2), StretchLink(id_no=2, en_no=3)])]), document="siaran_pers",
        id_filename="id.docx", en_filename="en.docx"))
    assert report.pairs[1].id_text is None and report.pairs[2].en_text == STRETCH_EN[2]
    assert report.n_meaning == 1  # only the English note stands alone
