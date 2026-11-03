"""document_checks: the report checked against itself — its own comparisons, its 'Tabel N'
pointers (against what each table prints) and its table numbering — with no LLM."""
from schemas import FactVerificationResult, PeriodResult

import document_checks as dc


def _narrative(*sentences, appendix=()):
    lines = ["[== Halaman 1 ==]", " ".join(sentences)]
    if appendix:
        lines.append("[== Halaman 2 ==]")
        lines.append("LAMPIRAN")
        for number, title in appendix:
            lines += [f"Tabel {number}", title, "Keterangan: *) Angka prakiraan"]
    return "\n".join(lines) + "\n"


def _value(quote, sheet, value=1.0):
    return FactVerificationResult(operation="value", metric_label="x", verdict="Entailed", reasoning="",
                                  context_quote=quote, claimed_value=value,
                                  matched_excel_source=f"Tabel SKDU.xlsx / {sheet}")


def _findings(results):
    return [r for r in results if r.resolved_via == dc.RESOLVED_VIA]


# --- a comparison between two numbers of the same sentence -----------------------------------

def test_a_comparison_the_two_numbers_contradict_is_tidak_sesuai():
    sentence = ("Persentase responden yang menjawab rentabilitas lebih baik sebanyak 22,68%, lebih rendah "
                "dibandingkan 22,34% pada triwulan sebelumnya.")
    found = _findings(dc.check_document(_narrative(sentence), []))
    assert len(found) == 1 and found[0].verdict == "Refuted" and found[0].operation == "comparison"
    assert "22,68 lebih rendah dibandingkan 22,34" in found[0].reasoning
    assert found[0].context_quote == sentence and found[0].page_number == 1


def test_a_year_between_the_figure_and_the_words_is_not_the_figure():
    sentence = "SB akses kredit sebesar 1,84% pada triwulan III 2026, meski lebih tinggi dibandingkan SB 2,45% pada triwulan II 2026."
    assert dc._wrong_comparisons(sentence) == [(1.84, 2.45, "tinggi")]


def test_a_comparison_the_numbers_bear_out_is_not_a_finding():
    sentence = "SBT kegiatan usaha sebesar 11,46%, meski lebih rendah dari 12,97% pada triwulan II 2026 (Grafik 2)."
    assert dc._wrong_comparisons(sentence) == []


def test_figures_given_respectively_are_compared_respectively():
    sentence = ("Giro rupiah serta uang kartal masing-masing sebesar 7,1% (yoy) dan 12,9% (yoy), lebih rendah "
                "dari pertumbuhan bulan Juli 2026 sebesar 10,9% (yoy) dan 15,7% (yoy).")
    assert dc._wrong_comparisons(sentence) == []
    wrong = sentence.replace("15,7%", "11,7%")
    assert dc._wrong_comparisons(wrong) == [(12.9, 11.7, "rendah")]


def test_a_contraction_is_a_negative_figure():
    sentence = ("Penjualan rumah tipe menengah tumbuh sebesar 4,84% (yoy), lebih tinggi dari triwulan "
                "sebelumnya yang terkontraksi sebesar 12,27% (yoy).")
    assert dc._wrong_comparisons(sentence) == []


def test_two_contractions_compared_by_size_are_left_alone():
    sentence = ("ULN swasta mengalami kontraksi 0,6% (yoy), lebih rendah dibandingkan kontraksi 1,3% (yoy) "
                "pada triwulan I 2026.")
    assert dc._wrong_comparisons(sentence) == []


def test_a_bound_and_a_fall_word_are_not_judged():
    bound = "IKE sebesar 109,4 berada pada level optimis (indeks >100), lebih tinggi dibandingkan bulan sebelumnya sebesar 107,9."
    assert dc._wrong_comparisons(bound) == []
    fall = "Harga turun 1,2%, lebih rendah dibandingkan 0,8% pada bulan sebelumnya."
    assert dc._wrong_comparisons(fall) == []


def test_the_trend_claim_that_read_the_numbers_instead_of_the_words_is_replaced():
    sentence = "SB akses kredit sebesar 1,84% pada triwulan III 2026, meski lebih tinggi dibandingkan SB 2,45% pada triwulan II 2026."
    trend = FactVerificationResult(
        operation="is_decreasing", metric_label="SB akses kredit", verdict="Entailed", reasoning="",
        context_quote=sentence, periods=[PeriodResult(metric_label="SB", year=2026, month="Q2", excel_value=2.45),
                                         PeriodResult(metric_label="SB", year=2026, month="Q3", excel_value=1.84)])
    value = _value(sentence, "T3 Keuangan dan Akses Kredit", 1.84)
    out = dc.check_document(_narrative(sentence), [trend, value])
    assert trend not in out and value in out
    assert [r.operation for r in _findings(out)] == ["comparison"]


def test_a_comparison_already_refuted_by_its_trend_claim_is_not_shown_twice():
    sentence = "Persentase responden sebanyak 22,68%, lebih rendah dibandingkan 22,34% pada triwulan sebelumnya."
    trend = FactVerificationResult(
        operation="is_decreasing", metric_label="x", verdict="Refuted", reasoning="", context_quote=sentence,
        periods=[PeriodResult(metric_label="x", year=2026, month="Q2", excel_value=22.34),
                 PeriodResult(metric_label="x", year=2026, month="Q3", excel_value=22.68)])
    out = dc.check_document(_narrative(sentence), [trend])
    assert out == [trend]


# --- table captions, their numbering and the sentences citing them ---------------------------

APPENDIX = [(1, "Kegiatan Usaha"), (2, "Kapasitas Produksi Terpakai"), (3, "Kondisi Keuangan dan Akses Kredit"),
            (4, "Penggunaan Tenaga Kerja")]
# What each table's picture prints, as word_extraction reads it.
PRINTED = {
    "Tabel 1": "Kegiatan Usaha SBT Tw II-2026 Tw III-2026 11,46 12,97 Pertanian -0,58",
    "Tabel 2": "Kapasitas Produksi Terpakai 73,75 73,16 Industri 70,11",
    "Tabel 3": "Akses Kredit Lebih Mudah 6,35 7,03 Lebih Sulit 4,12 Likuiditas 23,42",
    "Tabel 4": "Penggunaan Tenaga Kerja 0,16 -0,36 Industri 0,12",
}


def test_captions_are_read_on_one_line_or_with_the_title_on_the_next():
    text = "Tabel 1\nKegiatan Usaha\nTabel 2. Kapasitas Produksi\nTabel 3 menunjukkan kenaikan pada seluruh LU.\n"
    assert dc.table_captions(text) == [(1, "Kegiatan Usaha"), (2, "Kapasitas Produksi")]


def test_a_sentence_citing_the_table_that_prints_its_figures_is_not_a_finding():
    sentence = "Responden yang menjawab lebih mudah sebesar 6,35% dan lebih sulit 4,12% (Lampiran Tabel 3)."
    assert _findings(dc.check_document(_narrative(sentence, appendix=APPENDIX), [], PRINTED)) == []


def test_a_sentence_citing_another_table_than_the_one_printing_its_figures_is_tidak_sesuai():
    sentence = "Responden yang menjawab lebih mudah sebesar 6,35% dan lebih sulit 4,12% (Lampiran Tabel 1)."
    [found] = _findings(dc.check_document(_narrative(sentence, appendix=APPENDIX), [], PRINTED))
    assert found.operation == "reference" and found.verdict == "Refuted" and found.context_quote == sentence
    assert found.metric_label == "Rujukan Tabel 1"
    assert "merujuk Tabel 1, tetapi angkanya (6,35, 4,12) tidak ada di tabel itu; angka tersebut ada di Tabel 3" \
        in found.reasoning


def test_one_figure_another_table_also_prints_is_not_enough():
    sentence = "Responden yang menjawab lebih mudah sebesar 6,35% (Lampiran Tabel 1)."
    assert _findings(dc.check_document(_narrative(sentence, appendix=APPENDIX), [], PRINTED)) == []


def test_a_figure_the_sentence_rounded_from_the_cited_table_counts_as_printed_there():
    printed = {**PRINTED, "Tabel 3": PRINTED["Tabel 3"] + " 73,8 73,2"}
    sentence = "Kapasitas produksi terpakai sebesar 73,8%, dari 73,2% pada triwulan II 2026 (Lampiran Tabel 2)."
    assert _findings(dc.check_document(_narrative(sentence, appendix=APPENDIX), [], printed)) == []


def test_figures_printed_equally_in_two_other_tables_are_left_alone():
    printed = {**PRINTED, "Tabel 4": PRINTED["Tabel 4"] + " 6,35 4,12"}
    sentence = "Responden yang menjawab lebih mudah sebesar 6,35% dan lebih sulit 4,12% (Lampiran Tabel 1)."
    assert _findings(dc.check_document(_narrative(sentence, appendix=APPENDIX), [], printed)) == []


def test_lampiran_n_is_tabel_n_when_the_document_has_no_lampiran_captions():
    right = "Responden yang menjawab lebih mudah sebesar 6,35% dan lebih sulit 4,12% (Lampiran 3)."
    assert _findings(dc.check_document(_narrative(right, appendix=APPENDIX), [], PRINTED)) == []
    wrong = right.replace("(Lampiran 3)", "(Lampiran 1)")
    [found] = _findings(dc.check_document(_narrative(wrong, appendix=APPENDIX), [], PRINTED))
    assert found.metric_label == "Rujukan Tabel 1" and "ada di Tabel 3" in found.reasoning


def test_a_list_of_lampiran_is_checked_against_all_of_them():
    printed = {**PRINTED, "Lampiran 4": "Kredit 7.800,8 7.812,4", "Lampiran 5": "Pertumbuhan Kredit 10,92 11,36"}
    sentence = "Kredit tumbuh 10,92% (yoy), dari 11,36% (yoy) pada bulan sebelumnya (Lampiran 4 dan 5)."
    assert _findings(dc.check_document(_narrative(sentence, appendix=APPENDIX), [], printed)) == []


def test_a_cited_table_whose_picture_was_not_read_is_left_alone():
    printed = {key: text for key, text in PRINTED.items() if key != "Tabel 1"}
    sentence = "Responden yang menjawab lebih mudah sebesar 6,35% dan lebih sulit 4,12% (Lampiran Tabel 1)."
    assert _findings(dc.check_document(_narrative(sentence, appendix=APPENDIX), [], printed)) == []


def test_without_the_tables_text_a_pointer_is_only_checked_against_the_captions():
    sentence = "Responden yang menjawab lebih mudah sebesar 6,35% dan lebih sulit 4,12% (Lampiran Tabel 1)."
    assert _findings(dc.check_document(_narrative(sentence, appendix=APPENDIX), [])) == []


def test_a_sentence_citing_a_table_the_document_lacks_is_tidak_sesuai():
    sentence = "Margin usaha terendah sebesar 11,29% (Lampiran Tabel 12)."
    [found] = _findings(dc.check_document(_narrative(sentence, appendix=APPENDIX), [], PRINTED))
    assert "tidak ada judul Tabel 12" in found.reasoning


def test_a_number_printed_twice_and_one_skipped_is_one_finding():
    appendix = APPENDIX[:2] + [(4, "Kondisi Keuangan dan Akses Kredit"), (4, "Penggunaan Tenaga Kerja")]
    sentence = "Responden yang menjawab lebih mudah sebesar 6,35% (Lampiran Tabel 3)."
    [found] = _findings(dc.check_document(_narrative(sentence, appendix=appendix), []))
    assert found.metric_label == "Nomor tabel"
    assert "Tabel 4 dipakai 2 kali" in found.reasoning
    assert "tidak ada Tabel 3, padahal dirujuk 1 kalimat" in found.reasoning


def test_numbering_that_starts_again_is_not_checked():
    appendix = APPENDIX + [(1, "Neraca Pembayaran"), (2, "Ekspor"), (3, "Impor")]
    sentence = "Responden yang menjawab lebih mudah sebesar 6,35% dan lebih sulit 4,12% (Lampiran Tabel 1)."
    assert _findings(dc.check_document(_narrative(sentence, appendix=appendix), [], PRINTED)) == []


def test_a_document_with_few_captions_has_no_missing_caption_check():
    sentence = "Margin usaha terendah sebesar 11,29% (Lampiran Tabel 12)."
    assert _findings(dc.check_document(_narrative(sentence, appendix=APPENDIX[:2]), [])) == []
