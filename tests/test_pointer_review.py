"""pointer_review: the table pointers the numbers leave open, judged by a model from the sentence's
subject. The model is faked: what is tested is which sentences reach it and what its answer adds."""
import asyncio
from unittest.mock import Mock

from langchain_core.runnables import RunnableLambda

import document_checks as dc
import pointer_review as pr

APPENDIX = [(1, "Kegiatan Usaha"), (2, "Kapasitas Produksi Terpakai"), (3, "Kondisi Keuangan dan Akses Kredit"),
            (4, "Penggunaan Tenaga Kerja")]
PRINTED = {
    "Tabel 1": "Kegiatan Usaha 11,46 12,97",
    "Tabel 2": "Kapasitas Produksi 73,75 73,16",
    "Tabel 3": "Akses Kredit Lebih Mudah 6,35 Lebih Sulit 4,12",
    "Tabel 4": "Tenaga Kerja 0,16 -0,36",
}


def _narrative(*sentences):
    lines = ["[== Halaman 1 ==]", " ".join(sentences), "[== Halaman 2 ==]", "LAMPIRAN"]
    for number, title in APPENDIX:
        lines += [f"Tabel {number}", title, "Keterangan: *) Angka prakiraan"]
    return "\n".join(lines) + "\n"


def _llm(answer, calls):
    def _respond(prompt_value):
        calls.append(prompt_value.to_string())
        return pr._Judgements(items=answer)

    llm = Mock()
    llm.with_structured_output = Mock(return_value=RunnableLambda(_respond))
    return llm


# --- which pointers reach the model ----------------------------------------------------------

def test_a_pointer_whose_figures_only_the_cited_table_prints_is_settled():
    text = _narrative("Kapasitas produksi terpakai sebesar 73,75% (Lampiran Tabel 2).")
    assert dc.open_pointers(text, PRINTED)[1] == []


def test_a_pointer_without_figures_or_with_unprinted_ones_is_open():
    no_figure = "Peritel menerapkan potongan harga akhir tahun (Lampiran Tabel 2)."
    wrong_figure = "Kapasitas produksi terpakai sebesar 73,57% (Lampiran Tabel 2)."
    titles, opened = dc.open_pointers(_narrative(no_figure, wrong_figure), PRINTED)
    assert titles == {"Tabel 1": "Kegiatan Usaha", "Tabel 2": "Kapasitas Produksi Terpakai",
                      "Tabel 3": "Kondisi Keuangan dan Akses Kredit", "Tabel 4": "Penggunaan Tenaga Kerja"}
    assert [(p.sentence, keys) for p, keys in opened] == [(no_figure, ["Tabel 2"]), (wrong_figure, ["Tabel 2"])]


def test_figures_another_table_prints_more_of_or_as_many_leave_the_pointer_open():
    # NPI: the cited Tabel 4 prints "10,3" by chance, Tabel 2 and Tabel 5 both print "17,9" and
    # "10,3" (with one other table printing them, the code itself finds the pointer wrong).
    sentence = "Kapasitas produksi terpakai sebesar 73,75%, dari 73,16% (Lampiran Tabel 1)."
    more = {**PRINTED, "Tabel 1": PRINTED["Tabel 1"] + " 73,75", "Tabel 3": PRINTED["Tabel 3"] + " 73,75 73,16"}
    assert [keys for _, keys in dc.open_pointers(_narrative(sentence), more)[1]] == [["Tabel 1"]]
    # A tie (UB's Tabel 2 starts with Tabel 1's M2 row) proves nothing either: the model reads the
    # subject, told which table prints the figures too.
    tie = {**PRINTED, "Tabel 1": PRINTED["Tabel 1"] + " 73,75 73,16"}
    [(pointer, keys)] = dc.open_pointers(_narrative(sentence), tie)[1]
    assert keys == ["Tabel 1"] and pointer.tied == ["Tabel 2"]
    calls = []
    asyncio.run(pr.review_pointers(_narrative(sentence), [], tie,
                                   _llm([pr._Judgement(index=0, fits=True, table="Tabel 1", about="x")], calls)))
    assert "0. [Tabel 1] Kapasitas produksi terpakai sebesar 73,75%, dari 73,16% (Lampiran Tabel 1).\n" \
           "   angka juga di: Tabel 2" in calls[0]


def test_an_open_pointer_carries_the_sentences_before_it():
    lead = "Penggunaan tenaga kerja pada triwulan II 2026 tercatat menurun."
    sentence = "Hal tersebut tecermin dari SBT sebesar -0,45% (Lampiran Tabel 4)."
    [(opened, _)] = dc.open_pointers(_narrative(lead, sentence), PRINTED)[1]
    assert opened.sentence == sentence and opened.before == lead


def test_a_level_after_its_currency_is_a_figure():
    assert dc._sentence_figures("M2 tercatat Rp10.448,1 triliun, tumbuh 8,2% (Tabel 1 dan Grafik 1).") == {
        "10448,1": "10.448,1", "8,2": "8,2"}


def test_a_pointer_that_also_names_a_chart_is_left_to_the_model():
    # UB Juli 2026: Tabel 1 prints the levels, Grafik 1 the growth; Tabel 2 prints both.
    printed = {**PRINTED, "Tabel 1": "M2 10.371,1", "Tabel 2": "M2 10.371,1 8,3 8,7"}
    sentence = "M2 tercatat Rp10.371,1 triliun, tumbuh 8,3%, dari 8,7% (Tabel 1 dan Grafik 1)."
    assert [keys for _, keys in dc.open_pointers(_narrative(sentence), printed)[1]] == [["Tabel 1"]]
    assert [r for r in dc.check_document(_narrative(sentence), [], printed) if r.operation == "reference"] == []


def test_a_pointer_the_code_already_found_wrong_is_not_sent():
    sentence = "Lebih mudah sebesar 6,35% dan lebih sulit 4,12% (Lampiran Tabel 1)."
    assert dc.open_pointers(_narrative(sentence), PRINTED)[1] == []


def test_a_word_document_with_no_table_read_leaves_every_pointer_open_and_a_pdf_none():
    sentence = "Kapasitas produksi terpakai sebesar 73,75% (Lampiran Tabel 2)."
    assert len(dc.open_pointers(_narrative(sentence), {})[1]) == 1
    assert dc.open_pointers(_narrative(sentence)) == ({}, [])


# --- what the model's answer adds ------------------------------------------------------------

def test_a_pointer_the_model_says_names_another_table_is_tidak_sesuai():
    sentence = "Peritel menerapkan potongan harga akhir tahun (Lampiran Tabel 2)."
    calls = []
    llm = _llm([pr._Judgement(index=0, fits=False, table="Tabel 1", about="kegiatan usaha peritel")], calls)
    out = asyncio.run(pr.review_pointers(_narrative(sentence), [], PRINTED, llm))
    [card] = out
    assert card.operation == "reference" and card.verdict == "Refuted" and card.resolved_via == pr.RESOLVED_VIA
    assert card.metric_label == "Rujukan Tabel 2" and card.context_quote == sentence
    assert card.reasoning.startswith("Kalimat ini tentang kegiatan usaha peritel, yang ditampilkan di Tabel 1 "
                                     "('Kegiatan Usaha'), bukan di Tabel 2 ('Kapasitas Produksi Terpakai')")
    assert "Tabel 2: Kapasitas Produksi Terpakai | baris: Kapasitas Produksi" in calls[0]
    assert "0. [Tabel 2] Peritel menerapkan" in calls[0]


def test_an_unsure_answer_or_an_unlisted_or_cited_table_adds_no_finding():
    sentences = ["Peritel menerapkan potongan harga akhir tahun (Lampiran Tabel 2).",
                 "Kapasitas produksi terpakai sebesar 73,57% (Lampiran Tabel 2).",
                 "Tenaga kerja diprakirakan meningkat (Lampiran Tabel 4)."]
    answer = [pr._Judgement(index=0, fits=True, table="Tabel 1", about="diskon peritel"),
              pr._Judgement(index=1, fits=False, table="Tabel 9", about="x"),
              pr._Judgement(index=2, fits=False, table="Tabel 4", about="x"),
              pr._Judgement(index=7, fits=False, table="Tabel 1", about="x")]
    out = asyncio.run(pr.review_pointers(_narrative(*sentences), [], PRINTED, _llm(answer, [])))
    # The pointer the model found right shows as a Sesuai card; the others add nothing.
    [fits] = out
    assert fits.verdict == "Entailed" and fits.operation == "reference" and fits.context_quote == sentences[0]
    assert fits.reasoning.startswith("Kalimat ini tentang diskon peritel, yang ditampilkan di "
                                     "Tabel 2 ('Kapasitas Produksi Terpakai')")


def test_on_a_tie_the_models_pick_of_the_other_table_is_tidak_sesuai():
    # UB Sep 2026 (planted): the model answers fits=True ("Tabel 2 shows M2 as well") but picks
    # Tabel 1, the table printing the same figures that is about M2.
    sentence = "Kapasitas produksi terpakai sebesar 73,75%, dari 73,16% (Lampiran Tabel 1)."
    tie = {**PRINTED, "Tabel 1": PRINTED["Tabel 1"] + " 73,75 73,16"}
    picked = [pr._Judgement(index=0, fits=True, table="Tabel 2", about="kapasitas produksi")]
    [card] = asyncio.run(pr.review_pointers(_narrative(sentence), [], tie, _llm(picked, [])))
    assert card.verdict == "Refuted" and card.metric_label == "Rujukan Tabel 1"
    assert "angka kalimat tercetak di Tabel 1 dan di Tabel 2, jadi angka tidak menentukan" in card.reasoning
    # Picking the cited table keeps it, as a Sesuai card naming the tie.
    kept = [pr._Judgement(index=0, fits=True, table="Tabel 1", about="kapasitas produksi")]
    [card] = asyncio.run(pr.review_pointers(_narrative(sentence), [], tie, _llm(kept, [])))
    assert card.verdict == "Entailed" and "angkanya juga ada di Tabel 2" in card.reasoning


def test_a_chart_pointer_the_model_found_right_adds_no_card():
    sentence = "Ekspektasi konsumen meningkat (Grafik 1)."
    text = _narrative(sentence) + "Grafik 1. Ekspektasi Konsumen\nGrafik 2. Kredit\nGrafik 3. Harga\n"
    answer = [pr._Judgement(index=0, fits=True, table="Grafik 1", about="x")]
    assert asyncio.run(pr.review_pointers(text, [], PRINTED, _llm(answer, []))) == []


def test_no_open_pointer_means_no_call():
    calls = []
    text = _narrative("Kapasitas produksi terpakai sebesar 73,75% (Lampiran Tabel 2).")
    assert asyncio.run(pr.review_pointers(text, [], PRINTED, _llm([], calls))) == []
    assert calls == []


# --- charts and unreadable tables --------------------------------------------------------------

def _with_charts(*sentences):
    return _narrative(*sentences) + "Grafik 1. Kegiatan Usaha\nGrafik 2. Harga Jual\n"


def test_a_chart_pointer_the_model_says_names_another_chart_is_tidak_sesuai():
    sentence = "Harga jual diprakirakan meningkat (Grafik 1)."
    llm = _llm([pr._Judgement(index=0, fits=False, table="Grafik 2", about="harga jual")], [])
    [card] = asyncio.run(pr.review_pointers(_with_charts(sentence), [], PRINTED, llm))
    assert card.metric_label == "Rujukan Grafik 1"
    assert card.reasoning.startswith("Kalimat ini tentang harga jual, yang ditampilkan di Grafik 2 "
                                     "('Harga Jual'), bukan di Grafik 1 ('Kegiatan Usaha')")
    assert "judul grafik; isi grafik tidak dibaca" in card.reasoning


def test_a_chart_pointer_answered_with_a_table_adds_nothing():
    sentence = "Harga jual diprakirakan meningkat (Grafik 1)."
    llm = _llm([pr._Judgement(index=0, fits=False, table="Tabel 3", about="x")], [])
    assert asyncio.run(pr.review_pointers(_with_charts(sentence), [], PRINTED, llm)) == []


def test_a_card_on_an_unread_table_says_so():
    sentence = "Peritel menerapkan potongan harga akhir tahun (Lampiran Tabel 2)."
    printed = {key: text for key, text in PRINTED.items() if key != "Tabel 2"}
    unreadable = {"Tabel 2": "Tabel 2. Kapasitas Produksi Terpakai — gambar PNG, isinya tidak bisa dibaca"}
    llm = _llm([pr._Judgement(index=0, fits=False, table="Tabel 1", about="kegiatan usaha")], [])
    [card] = asyncio.run(pr.review_pointers(_narrative(sentence), [], printed, llm, unreadable))
    assert "isi Tabel 2 tidak terbaca (gambar PNG)" in card.reasoning


def test_a_chart_whose_title_names_what_the_sentence_names_keeps_its_pointer():
    # SK Agustus 2026: saving to income ratio (Grafik 18, "Komposisi Penggunaan Pendapatan …").
    sentence = "Proporsi pendapatan konsumen yang disimpan sebesar 15,8% (Grafik 1)."
    text = _narrative(sentence) + "Grafik 1. Komposisi Penggunaan Pendapatan Rumah Tangga\nGrafik 2. Rasio Konsumsi\n"
    llm = _llm([pr._Judgement(index=0, fits=False, table="Grafik 2", about="rasio tabungan")], [])
    assert asyncio.run(pr.review_pointers(text, [], PRINTED, llm)) == []
