"""Spelling in the document's own tables: captions, column headers and row names, never numbers."""
from unittest.mock import patch

from pdf_table_extraction import PdfTable
from schemas import TypoCheckResponse, TypoIssue
from typo_checker import IndexedTypoVerdict


def _verdicts(wrong):
    """A stand-in for the typo model: the candidates in `wrong` are misspelt, the rest are fine."""
    def escalate(candidates, _llm):
        return {c.index: (IndexedTypoVerdict(candidate_index=c.index, is_issue=True, category="ejaan",
                                             suggestion=wrong[c.display_text.lower()], explanation="salah ketik")
                          if c.display_text.lower() in wrong
                          else IndexedTypoVerdict(candidate_index=c.index, is_issue=False))
                for c in candidates}
    return escalate


def _npi_table(page=7):
    return PdfTable(page_number=page, caption="Tabel 3. Neraca Pendapatan Primer", unit="Juta USD",
                    grid=[["Komponen", "Q1 2026", "Q2 2026"],
                          ["Kompensansi tenaga kerja", 1250.5, -310.2],
                          ["Pendapatan investasi", 9.8, 10.1]])


# What the Word reader gets from NPI Tw II-2026's Tabel 1 picture (WordDocument.table_texts).
_NPI_WORD_TEXTS = {"Tabel 1": "TOTAL EKSPOR 100,0 100,0 11,2 3,5 A, General Marchandise 99,2 100,0 10,8 -3,4 "
                              "I, Produk Primer 59,1 58,7 (2,3) 12,3%"}


@patch("typo_checker._escalate_to_llm", side_effect=_verdicts({"kompensansi": "kompensasi"}))
def test_a_misspelt_row_name_in_a_pdf_table_is_reported_with_its_table_and_page(_escalate):
    from table_typos import check_table_typos, pdf_table_words

    [issue] = check_table_typos(pdf_table_words([_npi_table()]), llm=object())

    assert (issue.word, issue.suggestion, issue.category) == ("Kompensansi", "Kompensasi", "ejaan")
    assert (issue.location, issue.page_number) == ("Tabel 3", 7)


@patch("typo_checker._escalate_to_llm", side_effect=_verdicts({"marchandise": "merchandise"}))
def test_a_misspelt_name_in_a_word_picture_table_is_reported(_escalate):
    from table_typos import check_table_typos, word_table_words

    [issue] = check_table_typos(word_table_words(_NPI_WORD_TEXTS), llm=object())

    assert (issue.word, issue.location, issue.page_number) == ("Marchandise", "Tabel 1", None)


def test_a_word_table_is_cut_into_its_labels_at_the_numbers():
    from table_typos import word_table_words

    [table] = word_table_words(_NPI_WORD_TEXTS)

    assert table.cells == ["Tabel 1", "TOTAL EKSPOR", "A, General Marchandise", "I, Produk Primer"]


def test_only_the_pdf_tables_words_are_kept_never_their_numbers():
    from table_typos import pdf_table_words

    [table] = pdf_table_words([_npi_table()])

    assert table.cells == ["Tabel 3. Neraca Pendapatan Primer", "Komponen", "Q1 2026", "Q2 2026",
                           "Kompensansi tenaga kerja", "Pendapatan investasi"]


def test_neighbouring_cells_with_the_same_first_word_are_not_a_reduplication():
    from table_typos import TableWords, check_table_typos

    cells = ["Rupiah", "Rupiah dan Valas", "Giro", "Giro Rupiah"]

    assert check_table_typos([TableWords("Tabel 2", 3, cells)], llm=None) == []


@patch("typo_checker._escalate_to_llm", side_effect=_verdicts({}))
def test_month_abbreviations_are_not_sent_to_the_model(escalate):
    from table_typos import TableWords, check_table_typos

    check_table_typos([TableWords("Tabel 1", 1, ["Agu", "Sep", "Okt", "Des"])], llm=object())

    escalate.assert_not_called()


@patch("typo_checker._escalate_to_llm", side_effect=_verdicts({"kompensansi": "kompensasi"}))
def test_the_same_word_in_one_table_is_reported_once(_escalate):
    from table_typos import check_table_typos, pdf_table_words

    table = _npi_table()
    table.grid.append(["Kompensansi lainnya", 1.0, 2.0])

    assert len(check_table_typos(pdf_table_words([table]), llm=object())) == 1


def test_a_lampiran_keeps_its_own_name_and_a_caption_less_table_its_start():
    from table_typos import location_of

    assert location_of("Lampiran 2. Posisi Investasi") == "Lampiran 2"
    assert location_of("Posisi cadangan devisa menurut komponen") == "Tabel: Posisi cadangan devisa menurut…"


def test_table_issues_join_the_narrative_ones_in_the_counts():
    from table_typos import merge_typos

    narrative = TypoCheckResponse(total_issues=1, ejaan_count=0, tidak_baku_count=1, grammar_count=0,
                                  summary="x", issues=[TypoIssue(word="resiko", start=4, end=10, category="tidak_baku",
                                                                 suggestion="risiko", explanation="", page_number=2)])
    table = TypoIssue(word="Kompensansi", start=0, end=11, category="ejaan", suggestion="kompensasi",
                      explanation="", page_number=None, location="Tabel 3")

    merged = merge_typos(narrative, [table])

    assert (merged.total_issues, merged.ejaan_count, merged.tidak_baku_count) == (2, 1, 1)
    assert [i.word for i in merged.issues] == ["resiko", "Kompensansi"]
    assert merged.summary == "Ditemukan 2 isu: 1 ejaan, 1 kata tidak baku, 0 tata bahasa (1 di tabel dokumen)."


def test_no_tables_means_no_call():
    from table_typos import check_table_typos

    with patch("typo_checker._escalate_to_llm") as escalate:
        assert check_table_typos([], llm=object()) == []
    escalate.assert_not_called()


def test_two_capitalised_copies_side_by_side_are_two_cells_not_a_reduplication():
    from table_typos import TableWords, check_table_typos

    cells = ["Kelompok Barang Barang Konsumsi", "Total Total"]

    assert check_table_typos([TableWords("Tabel 1", None, cells)], llm=None) == []


def test_a_real_unhyphenated_reduplication_in_a_table_is_still_caught():
    from table_typos import TableWords, check_table_typos

    [issue] = check_table_typos([TableWords("Tabel 1", None, ["Negara negara mitra dagang"])], llm=None)

    assert (issue.word, issue.suggestion, issue.location) == ("Negara negara", "Negara-negara", "Tabel 1")
