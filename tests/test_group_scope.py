"""Quantified group claims: "naik di seluruh cabang", "kecuali X", "sebagian besar", "lainnya".

The extractor only says WHICH quantifier the sentence uses (ExtractedFact.group_scope); the
group set comes from the table — the sibling rows of the groups it named — and the code decides
whether the quantifier holds. Tables here are synthetic shop data, not BI series.
"""

from excel_parser_bi import BITableData
from paired_verifier import _ExcelSource, _attach_group_exclusions, _evaluate_fact
from structured_extractor import ExtractedFact, PeriodPoint

SEP = " > "
BRANCHES = ["Cabang Utara", "Cabang Selatan", "Cabang Timur", "Cabang Barat", "Cabang Tengah"]


def _source(rows, prefix="Penjualan", leaf_first=False, extra=None, title="Penjualan per cabang"):
    """rows: {branch: (Aug value, Sep value)} under 'prefix > branch' (or 'branch > prefix')."""
    table = BITableData(title=title, unit="Indeks", row_labels=[])
    def put(label, aug, sep):
        table.row_labels.append(label)
        table._data[(label, 2026, "Aug")] = aug
        table._data[(label, 2026, "Sep")] = sep
    for branch, (aug, sep) in rows.items():
        put(f"{branch}{SEP}{prefix}" if leaf_first else f"{prefix}{SEP}{branch}", aug, sep)
    for label, (aug, sep) in (extra or {}).items():
        put(label, aug, sep)
    return _ExcelSource(table=table, filename="toko.xlsx", sheet="T1")


def _fact(operation, members, scope=None, quote="q", value=None, prefix="Penjualan",
          leaf_first=False, exclude=None):
    label = (lambda m: f"{m}{SEP}{prefix}") if leaf_first else (lambda m: f"{prefix}{SEP}{m}")
    fact = ExtractedFact(
        operation=operation,
        periods=[PeriodPoint(label(m), 2026, "Sep") for m in members],
        claimed_value=value, unit=None, context_quote=quote, page_number=None,
        group_scope=scope,
    )
    if exclude:
        fact.group_exclude = [label(m) for m in exclude]
    return fact


ALL_UP = {b: (100.0, 101.0 + i) for i, b in enumerate(BRANCHES)}


def test_all_scope_holds_when_every_group_moved_that_way():
    q = "Penjualan meningkat di seluruh cabang."
    result = _evaluate_fact(_fact("is_increasing", BRANCHES, "all", q), [_source(ALL_UP)])
    assert result.verdict == "Entailed"


def test_all_scope_is_refuted_by_one_group_that_moved_the_other_way():
    rows = dict(ALL_UP, **{"Cabang Barat": (100.0, 97.0)})
    q = "Penjualan meningkat di seluruh cabang."
    result = _evaluate_fact(_fact("is_increasing", BRANCHES, "all", q), [_source(rows)])
    assert result.verdict == "Refuted"
    assert "Cabang Barat" in result.reasoning


def test_the_group_list_comes_from_the_table_not_from_the_extractor():
    """B2: the extractor listed two branches; the table has five, and one of the unlisted ones
    fell. 'seluruh' is about all five."""
    rows = dict(ALL_UP, **{"Cabang Tengah": (100.0, 95.0)})
    fact = _fact("is_increasing", ["Cabang Utara", "Cabang Selatan"], "all", "naik di seluruh cabang")
    assert _evaluate_fact(fact, [_source(rows)]).verdict == "Refuted"


def test_kecuali_removes_the_exception_from_the_all_scope():
    """'naik di seluruh cabang, kecuali Cabang Barat': the exception is its own (named) fact in
    the same sentence, so the 'all' fact is judged on the other four."""
    rows = dict(ALL_UP, **{"Cabang Barat": (100.0, 97.0)})
    q = "Penjualan naik di seluruh cabang, kecuali Cabang Barat yang turun."
    fact = _fact("is_increasing", BRANCHES, "all", q, exclude=["Cabang Barat"])
    assert _evaluate_fact(fact, [_source(rows)]).verdict == "Entailed"


def test_majority_holds_with_more_than_half():
    rows = dict(ALL_UP, **{"Cabang Barat": (100.0, 97.0), "Cabang Tengah": (100.0, 100.0)})
    q = "Penjualan naik pada sebagian besar kelompok cabang."
    assert _evaluate_fact(_fact("is_increasing", BRANCHES, "majority", q),
                          [_source(rows)]).verdict == "Entailed"


def test_majority_is_refuted_when_at_most_half_moved_that_way():
    rows = {b: (100.0, 99.0) for b in BRANCHES}
    rows["Cabang Utara"] = (100.0, 102.0)
    rows["Cabang Selatan"] = (100.0, 103.0)
    q = "Penjualan naik pada sebagian besar kelompok cabang."
    assert _evaluate_fact(_fact("is_increasing", BRANCHES, "majority", q),
                          [_source(rows)]).verdict == "Refuted"


def test_rest_scope_is_judged_on_the_groups_the_sentence_did_not_name():
    """'optimis di Cabang Utara, sedangkan cabang lainnya pesimis' (bound 100)."""
    rows = {b: (90.0, 95.0) for b in BRANCHES}
    rows["Cabang Utara"] = (100.0, 110.0)
    q = "Indeks optimis di Cabang Utara, sedangkan kelompok cabang lainnya pesimis."
    fact = _fact("below_threshold", BRANCHES[1:], "rest", q, value=100.0, exclude=["Cabang Utara"])
    assert _evaluate_fact(fact, [_source(rows)]).verdict == "Entailed"


def test_an_aggregate_row_is_not_one_of_the_groups():
    """'Penjualan > Total' sits among the branches in the table but is not a branch: it fell,
    and that must not refute a claim about every branch."""
    src = _source(ALL_UP, extra={f"Penjualan{SEP}Total": (100.0, 90.0)})
    fact = _fact("is_increasing", BRANCHES[:2], "all", "naik di seluruh cabang")
    assert _evaluate_fact(fact, [src]).verdict == "Entailed"


def test_the_group_can_be_the_first_level_of_the_row_name():
    """Some tables put the group first ('Cabang Utara > Penjualan'), next to other series of
    the same group ('Cabang Utara > Biaya'). Only the claimed series is read."""
    extra = {f"{b}{SEP}Biaya": (100.0, 80.0) for b in BRANCHES}
    src = _source(ALL_UP, leaf_first=True, extra=extra)
    fact = _fact("is_increasing", BRANCHES[:3], "all", "naik di seluruh cabang", leaf_first=True)
    assert _evaluate_fact(fact, [src]).verdict == "Entailed"


def test_a_group_set_that_cannot_be_determined_is_not_guessed():
    """One member and nothing else from the sentence: which level is the group is unknown."""
    fact = _fact("is_increasing", ["Cabang Utara"], "all", "naik di seluruh cabang")
    assert _evaluate_fact(fact, [_source(ALL_UP)]).verdict == "Inconclusive"


def test_exclusions_are_collected_from_named_facts_of_the_same_sentence():
    q = "Penjualan naik di Cabang Utara, sedangkan kelompok cabang lainnya turun."
    named = _fact("is_increasing", ["Cabang Utara"], None, q)
    rest = _fact("is_decreasing", BRANCHES[1:], "rest", q)
    other = _fact("is_increasing", ["Cabang Timur"], None, "kalimat lain")
    _attach_group_exclusions([named, rest, other])
    assert rest.group_exclude == [f"Penjualan{SEP}Cabang Utara"]
    assert named.group_exclude == []


# --- wiring: the result says it was quantified, and the AI passes leave it alone ------------

def test_the_result_records_the_quantifier():
    result = _evaluate_fact(_fact("is_increasing", BRANCHES, "all", "naik di seluruh cabang"),
                            [_source(ALL_UP)])
    assert result.group_scope == "all"


def test_tinjauan_ai_does_not_review_a_quantified_claim():
    """The review re-runs a claim on the rows the model cites; for "seluruh cabang" that is a
    subset chosen by the model, which could leave out exactly the branch that fell."""
    from ai_review import review_candidates
    rows = dict(ALL_UP, **{"Cabang Barat": (100.0, 97.0)})
    result = _evaluate_fact(_fact("is_increasing", BRANCHES, "all", "naik di seluruh cabang"),
                            [_source(rows)])
    assert result.verdict == "Refuted"
    assert review_candidates([result]) == []


def test_the_cell_pointer_does_not_reread_a_quantified_claim():
    import asyncio
    from unittest.mock import Mock
    from paired_verifier import _pointer_pass
    fact = _fact("is_increasing", ["Cabang Utara"], "all", "naik di seluruh cabang")
    result = _evaluate_fact(fact, [_source(ALL_UP)])
    assert result.verdict == "Inconclusive" and result.matched_excel_source is None
    src = _source(ALL_UP)
    src.grid = [["x"]]
    llm = Mock(side_effect=AssertionError("pointer LLM must not be called"))
    out, n = asyncio.run(_pointer_pass([fact], [result], [src], llm=llm))
    assert n == 0 and out == [result]


def test_deduplication_keeps_a_quantified_fact_beside_a_named_one():
    from paired_verifier import _deduplicate_facts
    named = _fact("is_increasing", BRANCHES[:2], None, "q")
    scoped = _fact("is_increasing", BRANCHES[:2], "majority", "q")
    assert len(_deduplicate_facts([named, scoped])) == 2


# --- exceptions read off the sentence itself --------------------------------------------------

def test_kecuali_in_the_sentence_excludes_the_group_even_without_a_fact_for_it():
    """The exception's own fact may be missing, or filed under a slightly different quote; the
    sentence still says which group is excepted."""
    rows = dict(ALL_UP, **{"Cabang Barat": (100.0, 97.0)})
    q = "Penjualan naik di seluruh cabang, kecuali Cabang Barat yang mengalami penurunan."
    assert _evaluate_fact(_fact("is_increasing", BRANCHES, "all", q), [_source(rows)]).verdict == "Entailed"


def test_kecuali_names_a_group_by_its_range():
    bands = ["Omzet Rp1 - 2 juta", "Omzet Rp2,1 - 3 juta", "Omzet Rp3,1 - 4 juta", "Omzet >Rp4 juta"]
    rows = {b: (100.0, 103.0) for b in bands}
    rows["Omzet Rp3,1 - 4 juta"] = (100.0, 98.0)
    q = "Penjualan naik di seluruh kelompok, kecuali kelompok Rp3,1-4 juta yang turun."
    fact = _fact("is_increasing", bands[:2], "all", q)
    assert _evaluate_fact(fact, [_source(rows)]).verdict == "Entailed"


def test_a_wrong_exception_is_still_refuted():
    """'kecuali Cabang Utara' when it was Cabang Barat that fell: Barat is in the 'all' set."""
    rows = dict(ALL_UP, **{"Cabang Barat": (100.0, 97.0)})
    q = "Penjualan naik di seluruh cabang, kecuali Cabang Utara yang turun."
    assert _evaluate_fact(_fact("is_increasing", BRANCHES, "all", q), [_source(rows)]).verdict == "Refuted"


def test_rest_excludes_the_groups_its_sentence_names():
    rows = {b: (100.0, 97.0) for b in BRANCHES}
    rows["Cabang Utara"] = (100.0, 104.0)
    q = "Penjualan naik di Cabang Utara, sedangkan kelompok cabang lainnya turun."
    assert _evaluate_fact(_fact("is_decreasing", BRANCHES[1:3], "rest", q),
                          [_source(rows)]).verdict == "Entailed"


def test_rest_excludes_the_groups_the_sentence_before_names():
    """SK Juli 2026: "Penurunan indeks terjadi pada kelompok … Rp2,1-3 juta dan Rp3,1-4 juta …
    Sementara itu, kelompok lainnya mengalami peningkatan indeks." The named groups sit in the
    sentence before, so the rest fact's own sentence excluded nothing and the two that fell
    refuted it."""
    rows = {b: (100.0, 104.0) for b in BRANCHES}
    rows["Cabang Utara"] = rows["Cabang Selatan"] = (100.0, 97.0)
    before = "Penurunan penjualan terjadi di Cabang Utara dan Cabang Selatan."
    rest = _fact("is_increasing", BRANCHES, "rest", "Sementara itu, cabang lainnya mengalami peningkatan.")
    facts = [_fact("is_decreasing", BRANCHES[:2], None, before), rest]

    _attach_group_exclusions(facts)

    assert rest.group_exclude == [f"Penjualan{SEP}{b}" for b in BRANCHES[:2]]
    assert _evaluate_fact(rest, [_source(rows)]).verdict == "Entailed"


# --- groups the extractor bundled or quantified although the sentence names them -------------

def test_named_sibling_groups_bundled_into_one_trend_are_judged_per_group():
    """'naik di Cabang Utara dan Cabang Selatan' as ONE fact with two rows of the same
    breakdown: each row's own trend, not a trend across the two."""
    q = "Penjualan naik di Cabang Utara dan Cabang Selatan."
    up = _evaluate_fact(_fact("is_increasing", BRANCHES[:2], None, q), [_source(ALL_UP)])
    assert up.verdict == "Entailed"
    rows = dict(ALL_UP, **{"Cabang Selatan": (100.0, 99.0)})
    down = _evaluate_fact(_fact("is_increasing", BRANCHES[:2], None, q), [_source(rows)])
    assert down.verdict == "Refuted"


def test_unrelated_series_bundled_into_one_trend_stay_inconclusive():
    table = BITableData(title="t", unit="Indeks", row_labels=["Omzet", "Biaya"])
    for label in ("Omzet", "Biaya"):
        table._data[(label, 2026, "Aug")] = 100.0
        table._data[(label, 2026, "Sep")] = 101.0
    fact = ExtractedFact("is_increasing", [PeriodPoint("Omzet", 2026, "Sep"), PeriodPoint("Biaya", 2026, "Sep")],
                         None, None, "Omzet dan biaya naik.")
    assert _evaluate_fact(fact, [_ExcelSource(table=table, filename="t.xlsx", sheet="T")]).verdict == "Inconclusive"


def test_a_quantified_list_the_sentence_names_is_judged_on_those_rows():
    """'seluruh komponen, yaitu Omzet dan Laba' names its members; they are not a breakdown."""
    table = BITableData(title="t", unit="Indeks", row_labels=["Indeks Omzet", "Indeks Laba", "Indeks Biaya"])
    for label, sep in (("Indeks Omzet", 120.0), ("Indeks Laba", 110.0), ("Indeks Biaya", 90.0)):
        table._data[(label, 2026, "Sep")] = sep
    q = "Optimisme bersumber dari seluruh komponen, yaitu Indeks Omzet dan Indeks Laba."
    fact = ExtractedFact("above_threshold", [PeriodPoint("Indeks Omzet", 2026, "Sep"),
                                              PeriodPoint("Indeks Laba", 2026, "Sep")],
                         100.0, None, q, group_scope="all")
    assert _evaluate_fact(fact, [_ExcelSource(table=table, filename="t.xlsx", sheet="T")]).verdict == "Entailed"


# --- a quantified claim that lists only its indicator ------------------------------------------

def _two_dimension_source(regions, ages):
    table = BITableData(title="t", unit="Indeks", row_labels=[])
    for name, (aug, sep) in list(regions.items()) + list(ages.items()):
        label = f"Penjualan{SEP}{name}"
        table.row_labels.append(label)
        table._data[(label, 2026, "Aug")] = aug
        table._data[(label, 2026, "Sep")] = sep
    return _ExcelSource(table=table, filename="t.xlsx", sheet="T")


REGIONS_UP = {"Wilayah Utara": (100.0, 103.0), "Wilayah Selatan": (100.0, 104.0)}
AGES_DOWN = {"Usia 20-30 th": (100.0, 95.0), "Usia 31-40 th": (100.0, 96.0)}


def test_the_sentence_names_the_breakdown_when_the_fact_lists_only_the_indicator():
    """'naik di seluruh wilayah' arrived as one fact on the bare indicator. The breakdown is
    the one whose rows share a word with the sentence ('wilayah'), not every row under it."""
    fact = ExtractedFact("is_increasing", [PeriodPoint("Penjualan", 2026, "Sep")], None, None,
                         "Penjualan naik di seluruh wilayah.", group_scope="all")
    assert _evaluate_fact(fact, [_two_dimension_source(REGIONS_UP, AGES_DOWN)]).verdict == "Entailed"


def test_listed_groups_keep_to_their_own_dimension_in_a_two_dimension_table():
    """The listed rows say 'Wilayah …'; the age rows under the same indicator are another
    breakdown and must not enter the 'seluruh' set."""
    fact = _fact("is_increasing", ["Wilayah Utara", "Wilayah Selatan"], "all", "naik di seluruh wilayah")
    assert _evaluate_fact(fact, [_two_dimension_source(REGIONS_UP, AGES_DOWN)]).verdict == "Entailed"


def test_a_quantified_claim_on_a_single_named_series_is_judged_on_that_series():
    """'optimisme seluruh komponen, yaitu Indeks Omzet …' came back as one 'all' fact on
    Indeks Omzet. The sentence names it and draws no breakdown, so its own row answers."""
    table = BITableData(title="t", unit="Indeks", row_labels=["Indeks Omzet", f"Indeks Omzet{SEP}Usia 20-30 th",
                                                              f"Indeks Omzet{SEP}Usia 31-40 th"])
    for label, sep in ((table.row_labels[0], 120.0), (table.row_labels[1], 90.0), (table.row_labels[2], 95.0)):
        table._data[(label, 2026, "Sep")] = sep
    fact = ExtractedFact("above_threshold", [PeriodPoint("Indeks Omzet", 2026, "Sep")], 100.0, None,
                         "Optimisme bersumber dari seluruh komponen, yaitu Indeks Omzet.", group_scope="all")
    assert _evaluate_fact(fact, [_ExcelSource(table=table, filename="t.xlsx", sheet="T")]).verdict == "Entailed"


# --- A4: a stand-in fact for a quantified claim -----------------------------------------------

def test_a_group_the_sentence_never_names_is_dropped_when_a_quantified_fact_covers_it():
    """'optimis di Cabang Utara, sedangkan cabang lainnya pesimis' came back with a 'rest' fact
    AND a below_threshold fact on a group of another breakdown ('Penjualan > Usia 20-30 th') —
    the model's stand-in for the same 'lainnya'. It is not a claim of the sentence."""
    from paired_verifier import _drop_group_stand_ins
    q = "Indeks optimis di Cabang Utara, sedangkan kelompok cabang lainnya pesimis."
    named = _fact("above_threshold", ["Cabang Utara"], None, q, value=100.0)
    rest = _fact("below_threshold", BRANCHES[1:], "rest", q, value=100.0)
    stand_in = _fact("below_threshold", ["Usia 20-30 th"], None, q, value=100.0)
    assert _drop_group_stand_ins([named, rest, stand_in]) == [named, rest]


def test_a_stand_in_is_kept_when_no_quantified_fact_covers_its_sentence():
    from paired_verifier import _drop_group_stand_ins
    q = "Indeks optimis pada sebagian besar kelompok cabang."
    stand_in = _fact("above_threshold", ["Cabang Barat"], None, q, value=100.0)
    assert _drop_group_stand_ins([stand_in]) == [stand_in]


# --- B4: superlatives are rankings, not just directions --------------------------------------

CITIES = {"Kota Utara": (100.0, 108.0), "Kota Selatan": (100.0, 106.0), "Kota Timur": (100.0, 101.0),
          "Kota Barat": (100.0, 96.0), "Kota Tengah": (100.0, 92.0)}
RANK_Q = ("Indeks mengalami peningkatan tertinggi di Kota Utara dan Kota Selatan, sementara "
          "penurunan terbesar terjadi di Kota Tengah dan Kota Barat.")


def _ranked(operation, cities, rows=CITIES, quote=RANK_Q):
    from paired_verifier import _attach_rankings
    facts = [_fact(operation, [c], None, quote) for c in cities]
    _attach_rankings(facts)
    src = _source(rows)
    return [_evaluate_fact(f, [src]).verdict for f in facts]


def test_the_named_groups_with_the_largest_moves_are_entailed():
    assert _ranked("is_increasing", ["Kota Utara", "Kota Selatan"]) == ["Entailed", "Entailed"]
    assert _ranked("is_decreasing", ["Kota Tengah", "Kota Barat"]) == ["Entailed", "Entailed"]


def test_a_group_that_rose_but_is_not_among_the_highest_is_refuted():
    q = RANK_Q.replace("Kota Selatan, sementara", "Kota Timur, sementara")
    assert _ranked("is_increasing", ["Kota Utara", "Kota Timur"], quote=q) == ["Entailed", "Refuted"]


def test_a_ranking_holds_when_it_holds_in_percent_even_if_not_in_points():
    rows = dict(CITIES, **{"Kota Timur": (50.0, 56.5)})   # +6,5 points (2nd) but +13% (1st)
    q = RANK_Q.replace("Kota Selatan, sementara", "Kota Timur, sementara")
    assert _ranked("is_increasing", ["Kota Utara", "Kota Timur"], rows=rows, quote=q) == ["Entailed", "Entailed"]


def test_a_superlative_about_a_level_is_not_read_as_a_ranking_of_changes():
    """'indeks tertinggi di Kota Timur' ranks levels; the trend fact keeps its plain check."""
    q = "Indeks meningkat di Kota Timur, dengan indeks tertinggi di Kota Timur."
    assert _ranked("is_increasing", ["Kota Timur"], quote=q) == ["Entailed"]


def test_the_number_ranked_comes_from_the_sentence_not_from_the_facts_extracted():
    """'peningkatan tertinggi di Kota Utara, Kota Selatan dan Kota Timur' with the Kota Selatan
    fact missing: Kota Timur is third, and the sentence names three — it is among them."""
    from paired_verifier import _attach_rankings
    rows = dict(CITIES, **{"Kota Timur": (100.0, 104.0)})
    q = "Indeks mengalami peningkatan tertinggi di Kota Utara, Kota Selatan, dan Kota Timur."
    facts = [_fact("is_increasing", [c], None, q) for c in ("Kota Utara", "Kota Timur")]
    _attach_rankings(facts)
    assert [_evaluate_fact(f, [_source(rows)]).verdict for f in facts] == ["Entailed", "Entailed"]


def test_range_groups_are_found_by_the_dimension_word_of_their_unit():
    """'porsi tabungan turun pada sebagian besar kelompok pengeluaran' on rows named only by a
    rupiah range ('Rp 1 - 2 juta > Tabungan'): the range's unit is what says 'pengeluaran'."""
    table = BITableData(title="Komposisi pendapatan", unit="%", row_labels=[])
    for band, (aug, sep) in {"Rp 1 - 2 juta": (10.0, 9.0), "Rp 2,1 - 3 juta": (12.0, 11.0),
                             "Rp 3,1 - 4 juta": (13.0, 13.0), "> Rp 4 juta": (15.0, 14.0)}.items():
        for leaf, shift in (("Tabungan", 0.0), ("Konsumsi", 50.0)):
            label = f"{band}{SEP}{leaf}"
            table.row_labels.append(label)
            table._data[(label, 2026, "Aug")] = aug + shift
            table._data[(label, 2026, "Sep")] = sep + shift
    fact = ExtractedFact("is_decreasing", [PeriodPoint("Tabungan", 2026, "Sep")], None, None,
                         "Porsi tabungan menurun pada sebagian besar kelompok pengeluaran.", group_scope="majority")
    result = _evaluate_fact(fact, [_ExcelSource(table=table, filename="t.xlsx", sheet="T")])
    assert result.verdict == "Entailed" and "dari 4 kelompok" in result.reasoning and "Konsumsi" not in result.reasoning
