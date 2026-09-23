"""Tests for the categorical axis of TableData.

The temporal axis (lookup / lookup_fuzzy / available_periods) is exercised extensively via
tests/test_excel_parser_bi.py — BITableData is an alias of TableData — so this file focuses
on the categorical lookups and the axis guards.
"""

from table_model import TableData


def _make_categorical():
    table = TableData(
        title="Daftar Barang Elektronik",
        unit="",
        row_labels=["Laptop ASUS", "Laptop HP", "Mouse Logitech"],
        col_labels=["Harga", "Stok"],
        axis_type="categorical",
    )
    table._data = {
        ("Laptop ASUS", "Harga"): 7_500_000.0,
        ("Laptop HP", "Harga"): 6_200_000.0,
        ("Laptop HP", "Stok"): 12.0,
        ("Mouse Logitech", "Harga"): 250_000.0,
        ("Mouse Logitech", "Stok"): 45.0,
    }
    return table


def test_lookup_cell_returns_exact_value():
    table = _make_categorical()

    assert table.lookup_cell("Laptop ASUS", "Harga") == 7_500_000.0
    assert table.lookup_cell("Mouse Logitech", "Stok") == 45.0


def test_lookup_cell_returns_none_for_missing_row_or_column():
    table = _make_categorical()

    assert table.lookup_cell("Laptop ASUS", "Stok") is None  # row exists, cell empty
    assert table.lookup_cell("Keyboard", "Harga") is None


def test_lookup_cell_fuzzy_matches_both_axes_case_insensitively():
    table = _make_categorical()

    row, col, value = table.lookup_cell_fuzzy("laptop asus", "harga")

    assert row == "Laptop ASUS"
    assert col == "Harga"
    assert value == 7_500_000.0


def test_lookup_cell_fuzzy_matches_row_by_containment():
    table = _make_categorical()

    row, col, value = table.lookup_cell_fuzzy("mouse", "stok")

    assert row == "Mouse Logitech"
    assert col == "Stok"
    assert value == 45.0


def test_lookup_cell_fuzzy_skips_row_candidate_without_data_for_the_column():
    # 'laptop' matches both laptops; Laptop ASUS has no Stok value, so the lookup must
    # settle on the row that actually carries the requested attribute.
    table = _make_categorical()

    row, col, value = table.lookup_cell_fuzzy("laptop", "stok")

    assert row == "Laptop HP"
    assert value == 12.0


def test_lookup_cell_fuzzy_returns_none_triple_when_nothing_matches():
    table = _make_categorical()

    assert table.lookup_cell_fuzzy("Printer Canon", "Harga") == (None, None, None)
    assert table.lookup_cell_fuzzy("Laptop ASUS", "Garansi") == (None, None, None)


def test_available_periods_is_empty_for_categorical_tables():
    # Categorical keys are 2-tuples — there are no (year, month) periods to enumerate,
    # and the guard keeps the 3-tuple unpacking from crashing.
    table = _make_categorical()

    assert table.available_periods("Laptop ASUS") == []


# ---------------------------------------------------------------------------
# Cosmetic spacing, qualifier coverage, and match scoring (temporal axis)
# ---------------------------------------------------------------------------

from table_model import label_match_score


def _make_temporal(labels):
    table = TableData(title="Indeks", unit="", row_labels=list(labels))
    table._data = {(label, 2026, "Jun"): float(i + 1) for i, label in enumerate(labels)}
    return table


def test_lookup_fuzzy_ignores_spacing_around_punctuation():
    # The narrative writes 'Rp4,1-5 juta'; the sheet writes 'Rp4,1 - 5 juta'.
    table = _make_temporal([
        "Indeks Ekspektasi Kegiatan Usaha (IEKU) > Pengeluaran Rp4,1 - 5 juta",
    ])

    matched, value = table.lookup_fuzzy(
        "Indeks Ekspektasi Kegiatan Usaha (IEKU) > Pengeluaran Rp4,1-5 juta", 2026, "Jun"
    )

    assert matched == "Indeks Ekspektasi Kegiatan Usaha (IEKU) > Pengeluaran Rp4,1 - 5 juta"
    assert value == 1.0


def test_lookup_fuzzy_rejects_a_qualified_query_answered_by_the_bare_parent():
    # 'IKLK' IS contained in the query, but answering a claim about one education level
    # with the national aggregate compares the wrong series.
    table = _make_temporal(["Indeks Ketersediaan Lapangan Kerja (IKLK)"])

    assert table.lookup_fuzzy(
        "Indeks Ketersediaan Lapangan Kerja (IKLK) > tingkat pendidikan lainnya", 2026, "Jun"
    ) == (None, None)


def test_lookup_fuzzy_rejects_a_leaf_match_that_names_a_different_quantity():
    """M2-Juni-2026: "suku bunga simpanan berjangka tenor 1 bulan" (4,77%) was answered by the
    'Simpanan Berjangka' leaf of a DPK table, which holds balances — reported Tidak Sesuai
    against 6,3. The report carries no interest-rate table at all, so the honest answer is no
    data. _query_is_about_the_label already guards the label-in-query tier against exactly this
    sentence; the leaf tier reached the same row without asking."""
    table = _make_temporal(["Total Jenis Simpanan > Simpanan Berjangka"])

    assert table.lookup_fuzzy(
        "suku bunga simpanan berjangka tenor 1 bulan", 2026, "Jun"
    ) == (None, None)


def test_lookup_fuzzy_still_answers_a_leaf_query_that_adds_no_new_subject():
    """The guard must not close the tier it protects: a query naming the same quantity still
    resolves."""
    table = _make_temporal(["Total Jenis Simpanan > Simpanan Berjangka"])

    matched, value = table.lookup_fuzzy("simpanan berjangka", 2026, "Jun")

    assert matched == "Total Jenis Simpanan > Simpanan Berjangka"
    assert value == 1.0


def test_lookup_fuzzy_sees_past_a_footnote_marker_on_a_leaf():
    """M2-Juni-2026 Tabel 4 marks its 'Lainnya' rows with a footnote: 'Total > Lainnya**'. The
    narrative says "DPK lainnya meningkat dari 3,5% (yoy) pada Mei 2026 menjadi 12,2% (yoy)", and
    the table holds exactly 3,5 and 12,2 — yet both claims came back Tidak Cukup Data, because
    the stars broke the leaf's substring test: 'lainnya**' is not inside 'dpk lainnya'. With the
    stars removed the same lookup resolves at once. A footnote marker is cosmetic; it must not
    decide whether a row exists."""
    table = _make_temporal([
        "Giro > Lainnya**",
        "Tabungan > Lainnya**",
        "Simpanan Berjangka > Lainnya**",
        "Total > Lainnya**",
    ])

    matched, value = table.lookup_fuzzy("DPK Lainnya", 2026, "Jun")

    # The aggregate parent is what a bare claim means — not giro's or tabungan's share.
    assert matched == "Total > Lainnya**", "and the label shown keeps its marker"
    assert value == 4.0


def test_lookup_fuzzy_footnote_markers_do_not_merge_distinct_rows():
    """Removing the stars must not blur what the markers sit beside: a claim naming one section
    still resolves to that section, not to whichever 'Lainnya' comes first."""
    table = _make_temporal(["Giro > Lainnya**", "Tabungan > Lainnya**", "Total > Lainnya**"])

    matched, _ = table.lookup_fuzzy("tabungan lainnya", 2026, "Jun")

    assert matched == "Tabungan > Lainnya**"


def test_lookup_fuzzy_rejects_a_leaf_match_on_a_single_shared_word():
    # 'Lainnya' shares one word out of three with 'tingkat pendidikan lainnya'.
    table = _make_temporal(["Lainnya", "Tabungan/deposito"])

    assert table.lookup_fuzzy(
        "Indeks Ketersediaan Lapangan Kerja (IKLK) > tingkat pendidikan lainnya", 2026, "Jun"
    ) == (None, None)


def test_lookup_fuzzy_keeps_a_qualified_query_whose_leaf_survives():
    table = _make_temporal(["Indeks Ketersediaan Lapangan Kerja (IKLK) > Sarjana"])

    matched, _ = table.lookup_fuzzy(
        "Indeks Ketersediaan Lapangan Kerja (IKLK) > Sarjana", 2026, "Jun"
    )

    assert matched == "Indeks Ketersediaan Lapangan Kerja (IKLK) > Sarjana"


def test_label_match_score_is_one_for_an_exact_match():
    assert label_match_score("Indeks Keyakinan Konsumen (IKK)",
                             "Indeks Keyakinan Konsumen (IKK)") == 1.0


def test_label_match_score_penalises_a_dropped_qualifier():
    query = "Indeks Keyakinan Konsumen (IKK) > Pengeluaran >Rp5 juta"

    exact = label_match_score(query, "Indeks Keyakinan Konsumen (IKK) > Pengeluaran >Rp5 juta")
    coarse = label_match_score(query, "Indeks Keyakinan Konsumen (IKK)")

    assert exact == 1.0
    assert coarse < exact


def test_label_match_score_penalises_an_over_specific_label():
    query = "Indeks Kondisi Ekonomi Saat Ini (IKE)"

    national = label_match_score(query, "Indeks Kondisi Ekonomi Saat Ini (IKE)")
    one_city = label_match_score(query, "15. Mataram > Indeks Kondisi Ekonomi Saat Ini (IKE)")

    assert national > one_city


# ---------------------------------------------------------------------------
# The label-in-query tier: extra words in the query must not name another quantity
# ---------------------------------------------------------------------------

def _growth_table():
    """A %-yoy DPK table, as printed in the M2 report's Lampiran 2."""
    return TableData(
        title="Lampiran 2. Pertumbuhan Uang Beredar dan Faktor yang Memengaruhinya",
        unit="%, yoy",
        row_labels=["Simpanan Berjangka", "Kredit", "Lainnya"],
        _data={("Simpanan Berjangka", 2026, "Apr"): 3.7, ("Kredit", 2026, "Apr"): 9.4},
    )


def test_an_interest_rate_claim_does_not_bind_to_a_balance_row():
    # The report states "suku bunga simpanan berjangka tenor 1 bulan 4,20%" but carries no
    # interest-rate table at all; answering from the DPK growth row produced a confident
    # Refuted (4,20 vs 3,7) on ten claims in one run.
    table = _growth_table()

    assert table._resolve_label("Suku bunga simpanan berjangka tenor 1 bulan") is None
    assert table.lookup_fuzzy("Suku bunga simpanan berjangka tenor 24 bulan", 2026, "Apr") == (None, None)


def test_a_qualified_deposit_claim_does_not_bind_to_a_bare_generic_row():
    table = _growth_table()

    assert table._resolve_label("DPK nasabah lainnya") is None


def test_words_the_table_title_accounts_for_do_not_block_a_match():
    # "pertumbuhan" is this table's own subject, not a different quantity.
    table = _growth_table()

    assert table._resolve_label("pertumbuhan kredit") == "Kredit"
    assert table._resolve_label("Kredit") == "Kredit"


def test_a_verbose_query_still_binds_when_the_label_carries_most_of_it():
    table = _growth_table()

    assert table._resolve_label("posisi Simpanan Berjangka") == "Simpanan Berjangka"


# ---------------------------------------------------------------------------
# Labels the PDF split mid-word (BI text layers embed zero-width spaces)
# ---------------------------------------------------------------------------

def _make_growth_snippet():
    """Tabel 6's growth half, spelled the way the text layer hands it over."""
    table = TableData(
        title="Tabel 6. Perkembangan Kredit Berdasarkan Jenis Penggunaan (%, yoy)",
        unit="%, yoy",
        row_labels=["Kredit M odal Kerja (KM K)", "Kredit Investasi (KI)",
                    "Kredit Konsum si (KK)", "Kredit M ultiguna"],
    )
    for label, value in [("Kredit M odal Kerja (KM K)", 5.8), ("Kredit Investasi (KI)", 18.4),
                         ("Kredit Konsum si (KK)", 6.0), ("Kredit M ultiguna", 8.5)]:
        table._data[(label, 2026, "Apr")] = value
    return table


def test_lookup_fuzzy_matches_a_label_the_pdf_split_mid_word():
    table = _make_growth_snippet()
    # The claim spells the metric normally; the row does not. Before spacing-tolerant matching
    # this fell through to a generic 'Kredit' row in a DIFFERENT table (9,4% total credit).
    assert table.lookup_fuzzy("kredit multiguna", 2026, "Apr") == ("Kredit M ultiguna", 8.5)
    assert table.lookup_fuzzy("Kredit Konsumsi (KK)", 2026, "Apr") == ("Kredit Konsum si (KK)", 6.0)


def test_label_match_score_is_not_punished_by_the_split():
    from table_model import label_match_score

    # The split row must not score BELOW an unrelated-but-clean generic row, or source ranking
    # hands the claim to the wrong table.
    assert label_match_score("kredit multiguna", "Kredit M ultiguna") > \
           label_match_score("kredit multiguna", "Kredit")


def test_a_split_label_still_does_not_bind_a_distinct_metric():
    # Spacing tolerance must not become a licence to match anything: 'Uang Beredar Digital'
    # shares two words with 'Uang Beredar Luas(M 2)' and is a different series.
    table = TableData(title="Uang Beredar dan faktor-faktornya", unit="Miliar Rp",
                      row_labels=["Uang Beredar Luas(M 2)"])
    table._data[("Uang Beredar Luas(M 2)", 2026, "Jan")] = 10116181.856
    assert table.lookup_fuzzy("Uang Beredar Digital", 2026, "Jan") == (None, None)


def test_a_row_that_only_repeats_the_title_loses_to_the_breakdown_it_asked_for():
    table = TableData(
        title="Tabel 7. Kredit Properti (triliun Rp) (%, yoy)", unit="%, yoy",
        row_labels=["Kredit Properti", "KPR dan KPA", "Konstruksi"],
    )
    for label, value in [("Kredit Properti", 17.5), ("KPR dan KPA", 4.8), ("Konstruksi", 46.0)]:
        table._data[(label, 2026, "Apr")] = value
    # Both rows are contained in the claim; the title already says "Kredit Properti", so only
    # 'KPR dan KPA' accounts for anything the claim adds.
    assert table.lookup_fuzzy("kredit properti KPR dan KPA", 2026, "Apr") == ("KPR dan KPA", 4.8)
    # A claim that really is about the table-wide series still reaches it.
    assert table.lookup_fuzzy("Penyaluran kredit properti", 2026, "Apr") == ("Kredit Properti", 17.5)


def test_query_coverage_zeroes_a_source_that_never_names_the_subject():
    credit = TableData(title="Tabel 5. Perkembangan Kredit Berdasarkan Golongan Debitur",
                       unit="%, yoy", row_labels=["Korporasi"])
    dpk = TableData(title="Tabel 4. Penghimpunan Dana Pihak Ketiga Berdasarkan Golongan Nasabah",
                    unit="%, yoy", row_labels=["Korporasi"])
    # 'Korporasi' matches equally well in both; only the title says which one is about DPK.
    assert credit.query_coverage("DPK korporasi", "Korporasi") == 0.0
    assert dpk.query_coverage("DPK korporasi", "Korporasi") == 1.0


def test_table_subject_recognises_both_the_spelt_out_name_and_the_abbreviation():
    from table_model import TableData

    def subject(title):
        return TableData(title=title, unit="", row_labels=[]).table_subject()

    assert subject("Lampiran 6. Tabel Uang Primer dan Faktor-Faktor yang Memengaruhinya") == "m0"
    assert subject("Tabel 9. Komponen M0 adjusted") == "m0"
    assert subject("Tabel 1. Uang Beredar dan Komponennya") == "m2"
    assert subject("Lampiran 2. Pertumbuhan Uang Beredar (M2)") == "m2"
    # DPK is a WIDER aggregate than M2's uang kuasi and prints rows of the same name, so it has
    # to be its own universe: 'Simpanan Berjangka' is 3.429,1 T in the DPK appendix and
    # 3.236,9 T in the M2 one.
    assert subject("Tabel 4. Penghimpunan Dana Pihak Ketiga Berdasarkan Golongan Nasabah") == "dpk"
    assert subject("Lampiran 3. Tabel Dana Pihak Ketiga di Perbankan") == "dpk"
    assert subject("Tabel 3. Penghimpunan DPK") == "dpk"
    # Most tables name no universe at all and must stay comparable with anything.
    assert subject("Tabel 6. Perkembangan Kredit Berdasarkan Jenis") is None
    # A word that merely contains the abbreviation is not the abbreviation.
    assert subject("Laporan M0X eksperimental") is None


def test_a_title_broken_by_a_stray_space_still_names_its_universe():
    from table_model import TableData

    def subject(title):
        return TableData(title=title, unit="", row_labels=[]).table_subject()

    # BI's PDFs break words on a stray space, and the break lands INSIDE the word: page 8 of
    # sample_data/M2-Juli-2026.pdf is captioned 'Pertumbuhan U ang Beredar dan Faktor yang Mem
    # engaruhinya'. 'uang\s*beredar' cannot match 'U ang Beredar', so that table classified as
    # None and the cross-universe guard was silently dead on it.
    assert subject("Lampiran 2. Pertumbuhan U ang Beredar dan Faktor yang Mem engaruhinya") == "m2"
    assert subject("Lampiran 6. Tabel Uang Prim er dan Faktor-Faktor yang Memengaruhinya") == "m0"
    assert subject("Lampiran 3. Tabel Dana Pihak Ket iga di Perbankan") == "dpk"
    # Closing the gap must not open a new one: squeezing the spaces out cannot invent an
    # abbreviation that was never printed as a word of its own.
    assert subject("Laporan M 0X eksperimental") is None


def test_a_bare_breakdown_claim_takes_the_aggregate_section():
    from table_model import TableData

    # Tabel 4 of the M2 report prints 'Korporasi' under each of Giro, Tabungan, Simpanan
    # Berjangka and Total. "DPK korporasi" names no section, so it means the whole DPK's.
    table = TableData(
        title="Tabel 4. Penghimpunan Dana Pihak Ketiga Berdasarkan Golongan Nasabah",
        unit="%, yoy",
        row_labels=["Giro", "Giro > Korporasi", "Total", "Total > Korporasi"],
    )
    table._data.update({
        ("Giro", 2026, "Jul"): 10.5, ("Giro > Korporasi", 2026, "Jul"): 13.1,
        ("Total", 2026, "Jul"): 7.7, ("Total > Korporasi", 2026, "Jul"): 12.5,
    })
    assert table.lookup_fuzzy("DPK korporasi", 2026, "Jul") == ("Total > Korporasi", 12.5)
    # Naming the section still picks that section.
    assert table.lookup_fuzzy("giro korporasi", 2026, "Jul") == ("Giro > Korporasi", 13.1)


def test_two_words_that_differ_only_by_a_derivational_prefix_are_one_word():
    from table_model import _same_root

    # The prose writes "kredit KEpemilikan rumah"; the table row reads "Kredit PEmilikan Rumah".
    assert _same_root("kepemilikan", "pemilikan")
    # But a stemmer would collapse 'perusahaan' to 'usaha' and let a claim about "skala usaha
    # mikro" match the sector row "Jasa Perusahaan". Requiring prefix + whole word keeps them apart.
    assert not _same_root("usaha", "perusahaan")
    assert not _same_root("rumah", "perumahan")


def test_a_claim_naming_two_members_of_a_family_takes_neither():
    from table_model import TableData

    # "Simpanan Berjangka (Rupiah & Valas)" names both halves, so it means their combination.
    # Lampiran 2 nests them one way and Lampiran 3 the other; both must fall through to the
    # combined row rather than answer with a sub-total.
    for rows, expected in (
        (["Simpanan Berjangka", "Simpanan Berjangka > Rupiah", "Simpanan Berjangka > Valas"],
         "Simpanan Berjangka"),
        (["Simpanan Berjangka", "Rupiah > Simpanan Berjangka", "Valas > Simpanan Berjangka"],
         "Simpanan Berjangka"),
    ):
        table = TableData(title="Lampiran", unit="%, yoy", row_labels=rows)
        for i, row in enumerate(rows):
            table._data[(row, 2026, "Jul")] = float(i)
        matched, _value = table.lookup_fuzzy("Simpanan Berjangka (Rupiah & Valas)", 2026, "Jul")
        assert matched == expected

    # Naming only ONE member still picks that member — the case the leaf tier exists for.
    table = TableData(
        title="Lampiran", unit="%, yoy",
        row_labels=["Tabungan Lainnya (Rupiah dan Valas) > Rupiah",
                    "Simpanan Berjangka (Rupiah dan Valas) > Rupiah"],
    )
    table._data[("Tabungan Lainnya (Rupiah dan Valas) > Rupiah", 2026, "Jul")] = 7.0
    table._data[("Simpanan Berjangka (Rupiah dan Valas) > Rupiah", 2026, "Jul")] = 1.0
    assert table.lookup_fuzzy("tabungan lainnya rupiah", 2026, "Jul")[1] == 7.0


# Tabel 5 of 'Tabel Series SK Juni 2026.xlsx', in sheet order: one Konsumsi / Cicilan / Tabungan
# block per expenditure group, the groups told apart only by the figures in their names.
_SK_TABEL_5 = [
    "Total > Konsumsi", "Total > Tabungan",
    "Rp 1 - 2 juta > Konsumsi", "Rp 1 - 2 juta > Tabungan",
    "Rp 2,1 - 3 juta > Konsumsi", "Rp 2,1 - 3 juta > Tabungan",
    "Rp 3,1 - 4 juta > Konsumsi", "Rp 3,1 - 4 juta > Tabungan",
    "Rp 4,1 - 5 juta > Konsumsi", "Rp 4,1 - 5 juta > Tabungan",
    "> Rp 5 juta > Konsumsi", "> Rp 5 juta > Tabungan",
]


def test_lookup_fuzzy_tells_expenditure_groups_apart_by_their_figures():
    """SK-Juni-2026: "Proporsi konsumsi … meningkat pada kelompok pengeluaran Rp2,1-3 juta
    (75,2%), Rp4,1-5 juta (71,8%), dan >Rp5 juta (70,9%)". All three claims came back Tidak
    Sesuai against 74,6 — the 'Rp 1 - 2 juta' row — because the leaf 'Konsumsi' matched every
    group equally and the figures that separate them are not significant words. The first group
    in the sheet won the tie, every time."""
    table = _make_temporal(_SK_TABEL_5)

    assert table.lookup_fuzzy("Konsumsi > Rp2,1 - 3 juta", 2026, "Jun")[0] == "Rp 2,1 - 3 juta > Konsumsi"
    assert table.lookup_fuzzy("Konsumsi > Rp4,1 - 5 juta", 2026, "Jun")[0] == "Rp 4,1 - 5 juta > Konsumsi"
    assert table.lookup_fuzzy("Konsumsi > > Rp5 juta", 2026, "Jun")[0] == "> Rp 5 juta > Konsumsi"
    assert table.lookup_fuzzy("Tabungan > Rp2,1-3 juta", 2026, "Jun")[0] == "Rp 2,1 - 3 juta > Tabungan"


def test_lookup_fuzzy_finds_nothing_for_a_group_the_table_does_not_break_out():
    """The report groups ages as ">41 tahun"; the sheet only has 41-50, 51-60 and >60. No row is
    that group, so the honest answer is no data — not the 41-50 row, and not the parent."""
    table = _make_temporal([
        "Indeks Ketersediaan Lapangan Kerja (IKLK)",
        "Indeks Ketersediaan Lapangan Kerja (IKLK) > Usia 20-30 th",
        "Indeks Ketersediaan Lapangan Kerja (IKLK) > Usia 41-50 th",
        "Indeks Ketersediaan Lapangan Kerja (IKLK) > Usia 51-60 th",
        "Indeks Ketersediaan Lapangan Kerja (IKLK) > Usia >60 th",
    ])

    assert table.lookup_fuzzy(
        "Indeks Ketersediaan Lapangan Kerja (IKLK) > Usia >41 th", 2026, "Jun"
    ) == (None, None)


def test_figures_that_are_not_group_bounds_do_not_block_a_match():
    from table_model import numbers_agree

    # A footnote marker on the row, and a year or a series code in the claim, say nothing about
    # which row is meant.
    assert numbers_agree("Uang Primer (M0) adjusted 2026", "Uang Prim er Adjusted 1)")
    assert numbers_agree("Giro Bank Umum di BI Adjusted", "Giro Bank Umum di BI Adjusted 2)")
    assert numbers_agree("M2", "Uang Beredar Luas (M2)")
    # A group bound the claim never states rules the row out.
    assert not numbers_agree("Konsumsi > Rp2,1 - 3 juta", "Rp 1 - 2 juta > Konsumsi")
    assert not numbers_agree("Usia >41 th", "Usia 41-50 th")
    # Spacing and separator style inside a figure do not matter.
    assert numbers_agree("Rp2 ,1-3 juta", "Rp 2,1 - 3 juta")


def test_a_group_row_of_a_different_series_does_not_answer_a_qualified_claim():
    """SK-Juni-2026 Tabel 2 breaks every index down by the same expenditure groups. "porsi
    pendapatan yang ditabung > Pengeluaran Rp2,1-3 juta" (15,6%) matched the IKK group row on
    its group half alone and was reported Tidak Sesuai against an index of 112,4. The series
    half of a qualified claim has to be accounted for too."""
    table = _make_temporal([
        "Indeks Keyakinan Konsumen (IKK) > Pengeluaran Rp1 - 2 juta",
        "Indeks Keyakinan Konsumen (IKK) > Pengeluaran Rp2,1 - 3 juta",
        "Indeks Kondisi Ekonomi (IKE) > Pengeluaran Rp2,1 - 3 juta",
    ])

    assert table.lookup_fuzzy(
        "porsi pendapatan yang ditabung > Pengeluaran Rp2,1-3 juta", 2026, "Jun"
    ) == (None, None)
    # The series the row does belong to still resolves, by name or by abbreviation.
    assert table.lookup_fuzzy(
        "Indeks Kondisi Ekonomi (IKE) > Pengeluaran Rp2,1-3 juta", 2026, "Jun"
    )[0] == "Indeks Kondisi Ekonomi (IKE) > Pengeluaran Rp2,1 - 3 juta"
    assert table.lookup_fuzzy("IKK > Pengeluaran Rp2,1-3 juta", 2026, "Jun")[0] == (
        "Indeks Keyakinan Konsumen (IKK) > Pengeluaran Rp2,1 - 3 juta"
    )


def test_a_short_row_is_read_with_its_table_title():
    """M2-Juli-2026 Tabel 8 is captioned 'Kredit UMKM' and its rows are bare: 'Investasi' holds
    14,2. "ekspansi kredit investasi UMKM sebesar 14,2% (yoy)" could not reach it — one word out
    of four is under the label-in-query floor — so the claim was answered by Tabel 6's
    economy-wide 'Kredit Investasi (KI)' row (23,1) and refuted. The words a row leaves to its
    caption are still accounted for."""
    umkm = TableData(
        title="Tabel 8. Kredit UMKM (triliun Rp) (%, yoy)", unit="%, yoy",
        row_labels=["Mikro", "Modal Kerja", "Investasi", "Total UMKM"],
    )
    umkm._data[("Investasi", 2026, "Jul")] = 14.2
    umkm._data[("Total UMKM", 2026, "Jul")] = 1.6

    assert umkm.lookup_fuzzy("ekspansi kredit investasi UMKM", 2026, "Jul") == ("Investasi", 14.2)


def test_a_title_does_not_license_a_row_about_something_else():
    # The guard the floor exists for: the report carries no interest-rate table, and a DPK
    # balance must not answer a claim about a rate just because both say 'simpanan berjangka'.
    dpk = TableData(
        title="Lampiran 3. Tabel Dana Pihak Ketiga di Perbankan", unit="%, yoy",
        row_labels=["Total Jenis Simpanan > Simpanan Berjangka"],
    )
    dpk._data[("Total Jenis Simpanan > Simpanan Berjangka", 2026, "Jul")] = 6.3

    assert dpk.lookup_fuzzy(
        "suku bunga simpanan berjangka tenor 1 bulan", 2026, "Jul"
    ) == (None, None)


def test_the_survey_report_ratio_names_reach_their_indonesian_rows():
    """SK-Juni-2026 states the household-income split in English — "average propensity to consume
    ratio … 73,0%" — while Tabel 5 names the same rows 'Konsumsi', 'Cicilan pinjaman' and
    'Tabungan'. The two share no word, so seven claims came back Tidak Cukup Data against a sheet
    holding exactly 73,0, 10,0 and 17,0."""
    table = TableData(
        title="Tabel 5. Table 5.", unit="persen",
        row_labels=["Total > Konsumsi", "Total > Cicilan pinjaman", "Total > Tabungan",
                    "Rp 1 - 2 juta > Konsumsi", "Rp 1 - 2 juta > Tabungan"],
    )
    for label, value in (("Total > Konsumsi", 73.0), ("Total > Cicilan pinjaman", 10.0),
                         ("Total > Tabungan", 17.0), ("Rp 1 - 2 juta > Konsumsi", 74.6),
                         ("Rp 1 - 2 juta > Tabungan", 17.2)):
        table._data[(label, 2026, "Jun")] = value

    assert table.lookup_fuzzy("average propensity to consume ratio", 2026, "Jun") == (
        "Total > Konsumsi", 73.0)
    assert table.lookup_fuzzy("debt installment to income ratio", 2026, "Jun") == (
        "Total > Cicilan pinjaman", 10.0)
    assert table.lookup_fuzzy("saving to income ratio", 2026, "Jun") == (
        "Total > Tabungan", 17.0)
    # The claim naming a group still gets that group, not the aggregate.
    assert table.lookup_fuzzy("saving to income ratio > Rp1 - 2 juta", 2026, "Jun")[0] == (
        "Rp 1 - 2 juta > Tabungan")


def test_a_sheet_that_holds_an_english_ratio_row_counts_as_discussing_it():
    """Resolving the row is not enough: paired_verifier drops a source whose coverage of the
    claim's scope words is zero, and 'average propensity to consume ratio' shares none with
    Tabel 5 until the term is read as the row name it stands for."""
    table = TableData(title="Tabel 5. Table 5.", unit="persen",
                      row_labels=["Total > Konsumsi", "Total > Tabungan"])

    assert table.query_coverage("average propensity to consume ratio", "Total > Konsumsi") == 1.0
    assert table.query_coverage("saving to income ratio", "Total > Tabungan") == 1.0
