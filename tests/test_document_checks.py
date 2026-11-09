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
    """The Tidak Sesuai cards the checks add (a pointer that holds adds a Sesuai card)."""
    return [r for r in results if r.resolved_via == dc.RESOLVED_VIA and r.verdict == "Refuted"]


def _held(results):
    return [r for r in results if r.resolved_via == dc.RESOLVED_VIA and r.verdict == "Entailed"]


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


# --- a change from one number to another ("menurun dari", "naik dari ... menjadi") -------------

def test_a_change_worded_the_other_way_to_its_numbers_is_tidak_sesuai():
    # Planted in NPI Tw II-2026: the workbook has no gas row, so only the sentence itself shows it.
    sentence = ("Impor gas pada triwulan II 2026 tercatat sebesar 1,5 miliar dolar AS, menurun dari 0,7 miliar "
                "dolar AS pada triwulan I 2026.")
    found = _findings(dc.check_document(_narrative(sentence), []))
    assert len(found) == 1 and found[0].verdict == "Refuted" and found[0].operation == "comparison"
    assert "menurun dari 0,7 ke 1,5" in found[0].reasoning


def test_from_and_into_are_read_in_that_order():
    assert dc._wrong_directions("Ekspor NG turun dari 0,5 miliar dolar AS menjadi 0,7 miliar dolar AS.") == [
        (0.7, 0.5, "turun")]
    assert dc._wrong_directions("Ekspor NG naik dari 0,5 miliar dolar AS menjadi 0,7 miliar dolar AS.") == []
    assert dc._wrong_directions(
        "Imbal hasil tercatat meningkat dari 6,3 miliar dolar AS menjadi 6,5 miliar dolar AS.") == []


def test_a_change_the_numbers_bear_out_or_cannot_judge_is_left_alone():
    assert dc._wrong_directions("Inflasi 2,3% (yoy), turun dari bulan sebelumnya 2,5% (yoy).") == []
    # SBank Tw I-2026: an index from below zero ("negatif sebesar 2,59") up to 0,15.
    assert dc._wrong_directions("ILS bernilai positif sebesar 0,15, meningkat dari triwulan IV 2025 yang "
                                "bernilai negatif sebesar 2,59 (Grafik 5).") == []
    # A share against a level, and a change with no second figure: nothing to compare.
    assert dc._wrong_directions("Kredit tumbuh 8,2%, naik dari Rp7.900 triliun.") == []
    assert dc._wrong_directions("Penjualan 120,5 naik dari bulan sebelumnya.") == []


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
    out = dc.check_document(_narrative(sentence, appendix=APPENDIX), [], PRINTED)
    assert _findings(out) == []
    # It shows as a Sesuai card, so the reader sees the pointer was checked.
    [held] = _held(out)
    assert held.operation == "reference" and held.metric_label == "Rujukan Tabel 3"
    assert held.context_quote == sentence
    assert held.reasoning.startswith("Kalimat merujuk Tabel 3 ('Kondisi Keuangan dan Akses Kredit'); "
                                     "angkanya (6,35, 4,12) tercetak di tabel itu")


def test_a_tie_with_another_table_of_the_same_kind_is_no_proof():
    # UB Sep 2026 (planted): "Posisi M2 … (Tabel 2)", but Tabel 2 (Faktor yang Memengaruhi Uang
    # Beredar) starts with the same M2 row as Tabel 1. The numbers cannot tell them apart.
    appendix = [(1, "Uang Beredar dan Komponennya"), (2, "Faktor yang Memengaruhi Uang Beredar"),
                (3, "Dana Pihak Ketiga")]
    printed = {"Tabel 1": "Uang Beredar Luas (M2) 10.447,6 10.587,9 8,2 8,3 Uang Kuasi 4.498,8",
               "Tabel 2": "Uang Beredar (M2) 10.447,6 10.587,9 8,2 8,3 Aktiva Luar Negeri Bersih 2.106,2",
               "Tabel 3": "DPK 9.895,8 8,2"}
    sentence = "Posisi M2 tercatat sebesar Rp10.587,9 triliun, atau tumbuh sebesar 8,3% (Tabel 2 dan Grafik 1)."
    out = dc.check_document(_narrative(sentence, appendix=appendix), [], printed)
    assert _findings(out) == [] and _held(out) == []
    [pointer] = [p for p in dc._pointers(_narrative(sentence, appendix=appendix), printed).pointers if p.cited]
    assert pointer.status == "open" and pointer.tied == ["Tabel 1"]
    # A tie with an appendix table that prints the body's figures again still holds.
    with_lampiran = {**printed, "Tabel 2": "Aktiva Luar Negeri Bersih 2.106,2",
                     "Lampiran 1": "M2 10.587,9 8,3"}
    one = "Posisi M2 tercatat sebesar Rp10.587,9 triliun, atau tumbuh sebesar 8,3% (Tabel 1)."
    assert [r.metric_label for r in _held(dc.check_document(_narrative(one, appendix=appendix), [],
                                                            with_lampiran))] == ["Rujukan Tabel 1"]


def test_a_sentence_citing_another_table_than_the_one_printing_its_figures_is_tidak_sesuai():
    sentence = "Responden yang menjawab lebih mudah sebesar 6,35% dan lebih sulit 4,12% (Lampiran Tabel 1)."
    [found] = _findings(dc.check_document(_narrative(sentence, appendix=APPENDIX), [], PRINTED))
    assert found.operation == "reference" and found.verdict == "Refuted" and found.context_quote == sentence
    assert found.metric_label == "Rujukan Tabel 1"
    assert "merujuk Tabel 1, tetapi angkanya (6,35, 4,12) tidak ada di tabel itu; angka tersebut ada di Tabel 3" \
        in found.reasoning


def test_a_cited_table_printing_only_some_figures_while_another_prints_all_is_tidak_sesuai():
    # NPI: "10,3" also sits in the cited Tabel 4 by chance; Tabel 3 prints both figures.
    printed = {**PRINTED, "Tabel 4": PRINTED["Tabel 4"] + " 4,12"}
    sentence = "Responden yang menjawab lebih mudah sebesar 6,35% dan lebih sulit 4,12% (Lampiran Tabel 4)."
    [found] = _findings(dc.check_document(_narrative(sentence, appendix=APPENDIX), [], printed))
    assert "ada di Tabel 3" in found.reasoning


def test_figures_some_other_table_does_not_print_all_of_are_left_alone():
    printed = {**PRINTED, "Tabel 4": PRINTED["Tabel 4"] + " 4,12", "Tabel 3": "Akses Kredit 6,35 4,12"}
    sentence = "Lebih mudah 6,35%, lebih sulit 4,12%, netral 89,53% (Lampiran Tabel 4)."
    assert _findings(dc.check_document(_narrative(sentence, appendix=APPENDIX), [], printed)) == []


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


def test_without_any_table_read_a_pointer_is_only_checked_against_the_captions():
    sentence = "Responden yang menjawab lebih mudah sebesar 6,35% dan lebih sulit 4,12% (Lampiran Tabel 1)."
    assert _findings(dc.check_document(_narrative(sentence, appendix=APPENDIX), [], {})) == []
    missing = "Margin usaha terendah sebesar 11,29% (Lampiran Tabel 12)."
    assert len(_findings(dc.check_document(_narrative(missing, appendix=APPENDIX), [], {}))) == 1


def test_a_pdf_has_no_numbering_or_pointer_check():
    # Its text layer keeps few captions whole: Tabel 12 would read as missing.
    sentence = "Margin usaha terendah sebesar 11,29% (Lampiran Tabel 12)."
    appendix = APPENDIX[:2] + [(4, "Penggunaan Tenaga Kerja"), (4, "Upah")]
    assert _findings(dc.check_document(_narrative(sentence, appendix=appendix), [])) == []


def test_a_sentence_citing_a_table_the_document_lacks_is_tidak_sesuai():
    sentence = "Margin usaha terendah sebesar 11,29% (Lampiran Tabel 12)."
    [found] = _findings(dc.check_document(_narrative(sentence, appendix=APPENDIX), [], PRINTED))
    assert "tidak ada judul Tabel 12" in found.reasoning


def test_a_number_printed_twice_and_one_skipped_is_one_finding():
    appendix = APPENDIX[:2] + [(4, "Kondisi Keuangan dan Akses Kredit"), (4, "Penggunaan Tenaga Kerja")]
    sentence = "Responden yang menjawab lebih mudah sebesar 6,35% (Lampiran Tabel 3)."
    [found] = _findings(dc.check_document(_narrative(sentence, appendix=appendix), [], {}))
    assert found.metric_label == "Nomor tabel"
    assert "Tabel 4 dipakai 2 kali" in found.reasoning
    assert "tidak ada Tabel 3, padahal dirujuk 1 kalimat" in found.reasoning


def test_numbering_that_starts_again_ends_the_reports_tables():
    # NPI: the report's Tabel 1-4, then a box or the appendix numbered from 1 again. Past that
    # point "Tabel 1" is another table: its captions are not counted (no "Tabel 1 dipakai 2 kali")
    # and a sentence there is not checked against the report's Tabel 1.
    body = "Responden yang menjawab lebih mudah sebesar 6,35% dan lebih sulit 4,12% (Tabel 1)."
    # With an appendix numbered apart, "Lampiran Tabel 1" is the appendix's table, not the report's.
    to_appendix = "Responden yang menjawab lebih baik sebesar 6,35% dan 4,12% (Lampiran Tabel 1)."
    box = "Perbandingan publikasi menunjukkan revisi sebesar 6,35 dan 4,12 (Tabel 1)."
    text = _narrative(body, to_appendix, appendix=APPENDIX) + "\n".join(
        ["Boks 1", "Tabel 1", "Perbandingan Publikasi NPI", box]) + "\n"
    found = _findings(dc.check_document(text, [], PRINTED))
    assert [(f.metric_label, f.context_quote) for f in found] == [("Rujukan Tabel 1", body)]


def test_a_list_of_tables_is_not_a_run_of_captions():
    # NPI's Daftar Tabel in two columns reads 1, 7, 2, 8 … each title followed by its page.
    contents = ["DAFTAR TABEL", "Tabel 1", "Ekspor Nonmigas", "5", "Tabel 7", "Impor Minyak", "11",
                "Tabel 2", "Ekspor Komoditas", "7"]
    text = "\n".join(["[== Halaman 1 ==]"] + contents) + "\n" + _narrative(
        "Responden yang menjawab lebih mudah sebesar 6,35% dan lebih sulit 4,12% (Lampiran Tabel 1).",
        appendix=APPENDIX)
    assert dc.table_captions(text) == APPENDIX
    [found] = _findings(dc.check_document(text, [], PRINTED))
    assert found.metric_label == "Rujukan Tabel 1" and "ada di Tabel 3" in found.reasoning


def test_a_document_with_few_captions_has_no_missing_caption_check():
    sentence = "Margin usaha terendah sebesar 11,29% (Lampiran Tabel 12)."
    assert _findings(dc.check_document(_narrative(sentence, appendix=APPENDIX[:2]), [], {})) == []


# --- charts: captions, numbering, pointers (never their content) ------------------------------

CHARTS = ["Grafik 1. Neraca Pembayaran Indonesia", "Grafik 2", "Transaksi Berjalan",
          "Grafik 3. Neraca Perdagangan Nonmigas", "Grafik 4. Pertumbuhan Ekspor Nonmigas"]


def _with_charts(*sentences, charts=CHARTS):
    return _narrative(*sentences, appendix=APPENDIX) + "\n".join(charts) + "\n"


def test_chart_captions_are_read_and_a_list_of_charts_is_not():
    contents = ["DAFTAR GRAFIK", "Grafik 1", "Neraca Pembayaran Indonesia", "3", "Grafik 13", "Posisi PMI", "14"]
    text = "\n".join(["[== Halaman 1 ==]"] + contents) + "\n" + _with_charts("Teks.")
    assert dc.chart_titles(text) == {"Grafik 1": "Neraca Pembayaran Indonesia", "Grafik 2": "Transaksi Berjalan",
                                     "Grafik 3": "Neraca Perdagangan Nonmigas",
                                     "Grafik 4": "Pertumbuhan Ekspor Nonmigas"}


def test_a_sentence_citing_a_chart_the_document_lacks_is_tidak_sesuai():
    sentence = "Ekspor nonmigas tumbuh 10,5% (Grafik 12)."
    [found] = _findings(dc.check_document(_with_charts(sentence), [], PRINTED))
    assert found.metric_label == "Rujukan Grafik 12" and "tidak ada judul Grafik 12" in found.reasoning


def test_chart_numbering_is_checked_like_table_numbering():
    charts = CHARTS[:4] + ["Grafik 3. Pertumbuhan Ekspor Nonmigas"]
    [found] = _findings(dc.check_document(_with_charts("Teks.", charts=charts), [], PRINTED))
    assert found.metric_label == "Nomor grafik" and "Grafik 3 dipakai 2 kali" in found.reasoning


def test_every_chart_pointer_with_a_caption_is_left_to_the_model():
    sentence = "Ekspor nonmigas tumbuh 10,5% (Tabel 1 dan Grafik 4)."
    titles, items = dc.open_pointers(_with_charts(sentence), PRINTED)
    assert titles["Grafik 4"] == "Pertumbuhan Ekspor Nonmigas"
    assert [keys for _, keys in items] == [["Tabel 1"], ["Grafik 4"]]


def test_a_pdf_has_no_chart_check_either():
    sentence = "Ekspor nonmigas tumbuh 10,5% (Grafik 12)."
    assert _findings(dc.check_document(_with_charts(sentence), [])) == []
