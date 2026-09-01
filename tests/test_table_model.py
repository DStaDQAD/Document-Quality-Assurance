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
    # Most tables name no universe at all and must stay comparable with anything.
    assert subject("Tabel 6. Perkembangan Kredit Berdasarkan Jenis") is None
    # A word that merely contains the abbreviation is not the abbreviation.
    assert subject("Laporan M0X eksperimental") is None


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
