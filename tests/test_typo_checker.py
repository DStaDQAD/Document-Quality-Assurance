from unittest.mock import Mock

from langchain_core.runnables import RunnableLambda

from typo_checker import (
    BatchTypoVerdicts,
    IndexedTypoVerdict,
    _collect_candidates_and_deterministic_issues,
    _strip_page_markers,
    check_typos,
)


def _llm_returning(verdicts):
    """A fake llm whose `.with_structured_output(...)` returns the given verdict list."""
    structured = RunnableLambda(lambda _prompt_value: BatchTypoVerdicts(verdicts=verdicts))
    llm = Mock()
    llm.with_structured_output = Mock(return_value=structured)
    return llm


# ---------------------------------------------------------------------------
# Deterministic tiers - no LLM involved
# ---------------------------------------------------------------------------

def test_baku_map_hit_is_flagged_without_an_llm():
    resp = check_typos("Resiko aktifitas ekonomi tetap terjaga.", llm=None)

    categories = {i.word.lower(): i.category for i in resp.issues}
    assert categories["resiko"] == "tidak_baku"
    assert categories["aktifitas"] == "tidak_baku"
    assert resp.tidak_baku_count == 2


def test_reduplication_without_hyphen_is_flagged():
    resp = check_typos("Rumah rumah di kompleks itu megah.", llm=None)

    assert resp.total_issues == 1
    issue = resp.issues[0]
    assert issue.category == "grammar"
    assert issue.suggestion == "Rumah-rumah"


def test_all_caps_heading_followed_by_titlecase_paragraph_is_not_reduplication():
    # Regression test: found via a real BI report where an ALL-CAPS section heading
    # ("PERKEMBANGAN KREDIT") sits directly above a new paragraph starting with the same
    # word in Title-case ("Kredit yang disalurkan..."). This must not be flagged as
    # reduplication - real reduplication never re-capitalises the repeated word.
    resp = check_typos("PERKEMBANGAN KREDIT\nKredit yang disalurkan oleh perbankan tumbuh positif.", llm=None)

    assert resp.total_issues == 0


def test_proper_noun_is_skipped_without_escalating_to_llm():
    llm = _llm_returning([])
    llm.with_structured_output = Mock(side_effect=AssertionError("should not be called"))

    resp = check_typos("Saya tinggal di Surabaya sejak lama.", llm=llm)

    assert resp.total_issues == 0


def test_clean_text_has_no_issues():
    resp = check_typos("Perekonomian tumbuh dengan baik pada triwulan ini.", llm=None)
    assert resp.total_issues == 0
    assert resp.summary == "Tidak ditemukan isu ejaan atau tata bahasa."


# ---------------------------------------------------------------------------
# Ambiguous cases - always escalated, never auto-flagged by suggestion ratio
# ---------------------------------------------------------------------------

def test_unknown_word_is_dropped_when_no_llm_available():
    # "asalasalan" is not in the dictionary and has no BAKU_MAP entry - with no llm it must be
    # dropped rather than guessed, per the conservative-default design.
    resp = check_typos("Kata asalasalan ini aneh sekali.", llm=None)
    assert resp.total_issues == 0


def test_unknown_word_resolved_as_valid_jargon_by_llm_produces_no_issue():
    # This is the key regression test for the false-positive risk found while building this
    # module: word-similarity ratio alone cannot tell "inflasi" (valid jargon) apart from a
    # real typo like "resiko" - so the LLM (not a ratio threshold) must make this call.
    llm = _llm_returning([
        IndexedTypoVerdict(candidate_index=0, is_issue=False, category="ejaan", suggestion="", explanation="istilah valid"),
    ])

    resp = check_typos("Inflasi tercatat rendah pada bulan ini.", llm=llm)

    assert resp.total_issues == 0


def test_unknown_word_confirmed_as_typo_by_llm_produces_ejaan_issue():
    llm = _llm_returning([
        IndexedTypoVerdict(candidate_index=0, is_issue=True, category="ejaan", suggestion="asal-asalan", explanation="typo"),
    ])

    resp = check_typos("Kata asalasalan ini aneh sekali.", llm=llm)

    assert resp.total_issues == 1
    assert resp.issues[0].category == "ejaan"
    assert resp.issues[0].suggestion == "asal-asalan"


def test_di_prefix_candidate_confirmed_as_grammar_issue_by_llm():
    llm = _llm_returning([
        IndexedTypoVerdict(candidate_index=0, is_issue=True, category="grammar", suggestion="dibaca", explanation="prefiks pasif"),
    ])

    resp = check_typos("Buku itu di baca oleh banyak orang.", llm=llm)

    assert resp.total_issues == 1
    issue = resp.issues[0]
    assert issue.category == "grammar"
    assert issue.word == "di baca"
    assert issue.suggestion == "dibaca"


def test_di_prefix_candidate_confirmed_as_correct_preposition_produces_no_issue():
    llm = _llm_returning([
        IndexedTypoVerdict(candidate_index=0, is_issue=False, category="grammar", suggestion="", explanation="preposisi benar"),
    ])

    resp = check_typos("Dia tinggal di sana.", llm=llm)

    assert resp.total_issues == 0


def test_ambiguous_candidates_dropped_when_llm_call_raises():
    # The real failure point is the .invoke() call (a network round-trip), not
    # with_structured_output() (which only configures the model) - mirrors how
    # excel_ingestion.py's equivalent escalation call can fail.
    failing_chain = RunnableLambda(lambda _p: (_ for _ in ()).throw(RuntimeError("boom")))
    llm = Mock()
    llm.with_structured_output = Mock(return_value=failing_chain)

    resp = check_typos("Kata asalasalan ini aneh sekali.", llm=llm)

    assert resp.total_issues == 0


# ---------------------------------------------------------------------------
# Deduplication - a repeated unknown word is escalated once, verdict applied to every occurrence
# ---------------------------------------------------------------------------

def test_repeated_unknown_word_is_escalated_only_once():
    # Three mid-sentence occurrences of one unknown word: escalation is deduplicated by
    # lowercased word, so the LLM is asked once and its verdict covers all three. (This once
    # had to avoid sentence-initial placement, which tier 4 skipped outright as a proper noun.
    # That is no longer so, but keeping every occurrence in one position keeps this test about
    # deduplication and nothing else.)
    clean_text, page_ranges = _strip_page_markers(
        "Kata itu asalasalan sekali. Katanya asalasalan lagi. Dan asalasalan lagi ketiga kalinya."
    )
    _issues, candidates = _collect_candidates_and_deterministic_issues(clean_text, page_ranges)

    unknown_word_candidates = [c for c in candidates if c.kind == "unknown_word"]
    assert len(unknown_word_candidates) == 1
    assert len(unknown_word_candidates[0].occurrences) == 3


def test_repeated_unknown_word_confirmed_as_typo_flags_every_occurrence():
    llm = _llm_returning([
        IndexedTypoVerdict(candidate_index=0, is_issue=True, category="ejaan", suggestion="asal-asalan", explanation="typo"),
    ])

    resp = check_typos("Kata itu asalasalan sekali. Katanya asalasalan lagi.", llm=llm)

    assert resp.total_issues == 2
    assert all(i.suggestion == "asal-asalan" for i in resp.issues)


# ---------------------------------------------------------------------------
# Page attribution
# ---------------------------------------------------------------------------

def test_issue_page_number_matches_the_page_marker_it_appears_under():
    text = (
        "[== Halaman 1 ==]\nSemua baik-baik saja di sini.\n\n"
        "[== Halaman 2 ==]\nResiko aktifitas ekonomi tetap terjaga."
    )

    resp = check_typos(text, llm=None)

    assert resp.total_issues == 2
    assert all(i.page_number == 2 for i in resp.issues)


def test_page_markers_are_stripped_and_not_treated_as_words():
    # "Halaman" itself must never show up as a flagged/considered token.
    text = "[== Halaman 1 ==]\nTeks yang bersih dan baku."
    resp = check_typos(text, llm=None)
    assert resp.total_issues == 0


# ---------------------------------------------------------------------------
# Optional verdict details — the model is asked to omit them when is_issue is false
# ---------------------------------------------------------------------------

def test_non_issue_verdict_needs_no_category_suggestion_or_explanation():
    # These fields are discarded for a non-issue, so the prompt tells the model to leave
    # them out entirely; a bare verdict must still parse and resolve cleanly.
    llm = _llm_returning([IndexedTypoVerdict(candidate_index=0, is_issue=False)])

    resp = check_typos("Inflasi tercatat rendah pada bulan ini.", llm=llm)

    assert resp.total_issues == 0


def test_issue_without_explanation_is_kept_with_an_empty_one():
    llm = _llm_returning([
        IndexedTypoVerdict(candidate_index=0, is_issue=True, category="ejaan",
                           suggestion="asal-asalan"),
    ])

    resp = check_typos("Kata asalasalan ini aneh sekali.", llm=llm)

    assert resp.total_issues == 1
    assert resp.issues[0].explanation == ""


def test_issue_without_a_suggestion_is_dropped_rather_than_shown_uncorrectable():
    llm = _llm_returning([
        IndexedTypoVerdict(candidate_index=0, is_issue=True, explanation="salah"),
    ])

    resp = check_typos("Kata asalasalan ini aneh sekali.", llm=llm)

    assert resp.total_issues == 0


# ---------------------------------------------------------------------------
# Tier 4: capitalisation at the start of a sentence is orthography, not a proper noun
# ---------------------------------------------------------------------------

def _candidate_texts(text: str):
    clean_text, page_ranges = _strip_page_markers(text)
    _issues, candidates = _collect_candidates_and_deterministic_issues(clean_text, page_ranges)
    return [c.display_text for c in candidates]


def test_misspelling_opening_a_bulleted_point_is_escalated():
    # The exact shape the blind spot was found in: every summary point of a BI release opens
    # with a bullet, so a misspelling there was skipped as a "proper noun" and never checked.
    cands = _candidate_texts("• Likuidiaftas perekonomian atau uang beredar tumbuh positif.")

    assert "Likuidiaftas" in cands


def test_misspelling_starting_a_sentence_after_a_full_stop_is_escalated():
    cands = _candidate_texts("Kondisi membaik. Pertumbuhhan kredit tetap kuat pada April 2026.")

    assert "Pertumbuhhan" in cands


def test_proper_noun_the_document_also_uses_mid_sentence_is_not_escalated():
    # The document's own evidence: a word that appears capitalised where capitalisation is NOT
    # required is a proper noun, so its sentence-initial occurrence must stay untouched.
    cands = _candidate_texts(
        "Makassar mencatat penurunan indeks. Penurunan juga terjadi di Makassar pada Juni 2026."
    )

    assert not [c for c in cands if c.lower() == "makassar"]


def test_correctly_spelled_word_opening_a_sentence_is_not_escalated():
    # 'Perekonomian' is an ordinary word capitalised by position - escalating it would spend an
    # LLM slot to be told what the dictionary already knows.
    assert _candidate_texts("Perekonomian tumbuh dengan baik pada triwulan ini.") == []


def test_table_fragment_on_its_own_line_is_not_escalated():
    # A PDF's text layer puts every table cell on its own line, and BI Lampiran labels arrive
    # truncated. Treating a bare line break as a sentence start would pull all of them in.
    cands = _candidate_texts("Rincian neraca bank umum\nKewaj\n10.415,9\nAkti\n9.870,2")

    assert "Kewaj" not in cands
    assert "Akti" not in cands


def test_all_caps_abbreviation_opening_a_sentence_is_not_escalated():
    assert "BUMN" not in _candidate_texts("BUMN mencatat laba yang lebih tinggi pada April 2026.")


def test_misspelling_opening_a_sentence_is_reported_when_the_llm_confirms_it():
    # End to end: the fix has to reach the user's report, not merely the escalation queue.
    llm = _llm_returning([
        IndexedTypoVerdict(candidate_index=0, is_issue=True, category="ejaan", suggestion="Likuiditas", explanation="typo"),
    ])

    resp = check_typos("• Likuidiaftas perekonomian tumbuh positif pada April 2026.", llm=llm)

    assert resp.total_issues == 1
    assert resp.issues[0].word == "Likuidiaftas"
    assert resp.issues[0].category == "ejaan"
    assert resp.issues[0].suggestion == "Likuiditas"
