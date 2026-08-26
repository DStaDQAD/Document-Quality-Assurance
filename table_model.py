"""Generic two-axis table container shared by every table parser.

A parsed table is modelled as (row_label, column_key) -> float where the column axis is one
of two kinds:

  temporal    : column key = (year, month) — the BI time-series case. The month slot holds
                a 3-letter month abbreviation ("Apr") OR a quarter token ("Q2") for
                quarterly tables. Internally stored as the 3-tuple key (row_label, year,
                month) so existing callers, tests and eval fixtures that populate `_data`
                directly keep working unchanged.
  categorical : column key = attribute name (e.g. 'Harga', 'Stok' in an item list).
                Stored as the 2-tuple key (row_label, col_label).

The fuzzy label-matching machinery (tiered containment + title-aware Total fallback) lives
here because it is axis-agnostic: the same tiers that resolve a claim's metric name against
row labels also resolve an attribute name against column labels.

`BITableData` in excel_parser_bi.py is an alias of this class (axis_type defaults to
"temporal"), kept so existing imports and type hints stay valid.
"""

import re
from dataclasses import dataclass, field
from typing import ClassVar, Dict, List, Optional, Tuple

# Separator between a duplicated label and its qualifying parent ("Parent > Child").
QUAL_SEP = " > "


def _sig_words(text: str) -> set:
    """Lowercased words of 3+ chars — the same notion of 'significant' used for title matching."""
    return {w.lower() for w in re.findall(r"\w+", text) if len(w) > 2}


def _canon(text: str) -> str:
    """Lowercase, whitespace collapsed, and spacing around punctuation removed.

    Containment matching compares a claim's wording against a sheet's, and the two spell the
    same category differently for purely cosmetic reasons: the narrative writes "kelompok
    pengeluaran Rp4,1-5 juta" where the sheet's row reads "Pengeluaran Rp4,1 - 5 juta". On the
    raw strings neither contains the other, so the breakdown row was missed and the claim fell
    back to the coarser national row of another sheet.

    Punctuation itself is KEPT — deleting it would let "tabungan lainnya rupiah" read as a
    substring of "Tabungan Lainnya (Rupiah dan Valas)", so a query for the Rupiah sub-row
    would bind to its parent's combined total instead.
    """
    return re.sub(r"\s*([^\w\s])\s*", r"\1", re.sub(r"\s+", " ", text.lower().strip()))


def _label_words(text: str) -> set:
    """`_sig_words` plus every word the text's glyph splits would have formed if rejoined.

    A label the PDF broke mid-word ('Kredit M ultiguna', 'Kredit Konsum si') yields word sets
    that share nothing with the claim's wording — 'ultiguna' is not 'multiguna' — so every guard
    built on word overlap silently stops recognising the row. Adding the 2- and 3-word
    concatenations restores the real words ('m'+'ultiguna', 'konsum'+'si') without the bleed a
    plain substring test brings: 'usaha' would match inside 'Perusahaan', but no run of whole
    words ever concatenates to it.
    """
    raw = [w.lower() for w in re.findall(r"\w+", text)]
    words = {w for w in raw if len(w) > 2}
    for span in (2, 3):
        for i in range(len(raw) - span + 1):
            joined = "".join(raw[i:i + span])
            if len(joined) > 2:
                words.add(joined)
    return words


def _label_is_covered_by(label: str, q_words: set, ignorable: frozenset) -> bool:
    """True when every meaningful word of `label` is accounted for by the claim's words.

    Walks the label left to right, consuming one word when the claim names it and two or three
    when only their concatenation does — which is how a glyph-split label is read back
    ('m'+'odal' -> modal, 'um'+'km' -> umkm) without a substring test's bleed. Tokens the claim
    never names make the label a DIFFERENT series, and that is the whole point of the check:
    "Uang Beredar Digital" must not bind to 'Uang Beredar Luas(M2)' just because it shares the
    first two words. Stop words and stray one/two-character tokens (footnote markers, '1)') are
    ignored — they distinguish nothing either way.
    """
    raw = [w.lower() for w in re.findall(r"\w+", label)]
    i = 0
    while i < len(raw):
        if raw[i] in q_words or raw[i] in ignorable:
            i += 1
            continue
        merged = False
        for span in (2, 3):
            if i + span <= len(raw) and "".join(raw[i:i + span]) in q_words:
                i += span
                merged = True
                break
        if merged:
            continue
        if len(raw[i]) < 3:
            i += 1        # footnote marker or the orphan half of a split we could not rejoin
            continue
        return False
    return True


def _tight(text: str) -> str:
    """Letters and digits only, lowercased — a form immune to where the spaces fell.

    BI's PDFs embed zero-width spaces inside words, so the text layer spells row labels
    'Kredit M ultiguna', 'Kredit Konsum si (KK)' and 'Sim panan Berjangka'. The spaces are real
    characters in the content stream, indistinguishable from word spaces without the glyph
    boxes, so the labels cannot be repaired — only compared in a form that ignores the split.
    Same normalisation `pdf_table_extraction._norm_label` already applies for the same reason.
    """
    return re.sub(r'[^a-z0-9]', '', text.lower())


def _tight_score(query: str, matched_label: str) -> float:
    """Containment score of two labels compared without spacing or punctuation.

    1.0 for the same label spelled differently, otherwise the share of the longer string the
    shorter one accounts for, and 0.0 when neither contains the other.
    """
    q, l = _tight(query), _tight(matched_label)
    if not q or not l:
        return 0.0
    if q == l:
        return 1.0
    if q in l or l in q:
        return min(len(q), len(l)) / max(len(q), len(l))
    return 0.0


def label_match_score(query: str, matched_label: str) -> float:
    """How well a resolved row label actually answers the metric name a claim asked for: the
    Dice overlap of their significant words, 1.0 for a perfect match and 0.0 for none.

    The fuzzy tiers are deliberately permissive — they will happily bind "IKK of the >Rp5
    juta expenditure group" to a plain "Indeks Keyakinan Konsumen (IKK)" row, since the
    query CONTAINS that label. That is correct as a last resort but wrong when another
    uploaded sheet carries the actual breakdown row, so paired_verifier ranks the sources
    that resolved a claim by this score instead of taking whichever came first.

    Penalising both directions is intentional: words of the query missing from the label
    mean the row is too coarse for the claim (a group breakdown answered by the national
    aggregate), and words of the label missing from the query mean it is too specific (a
    national claim answered by one city's row).
    """
    q, l = _sig_words(query), _sig_words(matched_label)
    if not q or not l:
        return _tight_score(query, matched_label)
    # Word overlap alone punishes a label the PDF split mid-word: 'Kredit M ultiguna' shares only
    # 'kredit' with the claim's "kredit multiguna" and scores 0,5, while a generic 'Kredit' row in
    # a DIFFERENT table scores 0,67 and wins the source ranking — the claim is then answered with
    # total credit growth (9,4%) instead of multiguna's (8,5%). Measured on the M2 report, that
    # one effect produced six false Refuteds. Comparing the labels as unspaced strings recognises
    # them as the same name, so the better score of the two is the honest one.
    return max(2 * len(q & l) / (len(q) + len(l)), _tight_score(query, matched_label))


@dataclass
class TableData:
    """Parsed table ready for direct lookup along either axis kind."""
    title: str
    unit: str
    row_labels: List[str]
    col_labels: List[str] = field(default_factory=list)  # categorical axis only
    axis_type: str = "temporal"  # "temporal" | "categorical"

    # Internal: (row_label, year, month) -> float for temporal tables,
    #           (row_label, col_label)   -> float for categorical tables.
    # First occurrence wins for dupes.
    _data: Dict[Tuple, float] = field(default_factory=dict, repr=False)

    # ------------------------------------------------------------------
    # Temporal lookups (unchanged behaviour from the original BITableData)
    # ------------------------------------------------------------------

    def lookup(self, row_label: str, year: int, month: str) -> Optional[float]:
        return self._data.get((row_label, year, month))

    # Words too generic to indicate semantic overlap between a query and the table title.
    # Includes report-speak verbs ("penghimpunan DPK", "penyaluran kredit") that describe an
    # action on the subject rather than naming a different subject.
    _TITLE_STOP_WORDS: ClassVar[frozenset] = frozenset({
        "dan", "di", "ke", "dari", "untuk", "yang", "pada", "kepada", "atau", "dalam",
        "dengan", "oleh", "atas", "total", "jumlah", "posisi", "indonesia", "bank",
        "penghimpunan", "penyaluran", "pertumbuhan", "perkembangan", "tercatat",
        # BI's classifier vocabulary: words that name the DIMENSION a table breaks its subject
        # down by, never the series itself. "kredit skala usaha mikro" is a claim about mikro,
        # and "Berdasarkan Jenis Penggunaan" says how Tabel 6 is organised. Counting them as
        # significant made every such claim fail the label guards — 'M ikro' accounts for one
        # of {kredit, skala, usaha, mikro} and needs half — so the row that IS the answer was
        # never reachable. 'kelompok' is deliberately absent: a survey report's "kelompok
        # pengeluaran Rp4,1-5 juta" really does name a distinct series.
        "skala", "usaha", "jenis", "penggunaan", "golongan", "berdasarkan",
        "the", "of", "a", "an", "and", "in", "for",
    })

    # Report terminology whose official table titles use a different wording — e.g. the M2
    # report says "DPK" while the corresponding table is titled "Posisi Simpanan Masyarakat".
    # A query word counts as covered by the title when the word itself OR any synonym is there.
    _SUBJECT_SYNONYMS: ClassVar[Dict[str, frozenset]] = {
        "dpk": frozenset({"simpanan", "dana", "pihak", "ketiga"}),
    }

    # Row labels that represent the table-wide aggregate; BI uses both spellings.
    _TOTAL_ROW_NAMES: ClassVar[frozenset] = frozenset({"total", "jumlah"})

    def _query_matches_table_subject(self, query: str) -> bool:
        """Return True when the query names this table's overall subject (per the title).

        Used to decide whether a generic 'Total'/'Jumlah' row is the right aggregate for
        the query (e.g. query='Cadangan Devisa' against a table titled 'Cadangan Devisa
        Indonesia', or query='Penghimpunan DPK' against 'Posisi Simpanan Masyarakat ...').

        EVERY significant query word must be covered by the title (directly or via
        _SUBJECT_SYNONYMS) — mere overlap is not enough, because a query with an extra
        uncovered word ('DPK korporasi') names a BREAKDOWN of the subject, and answering
        it with the table-wide total row would compare against the wrong series.
        """
        def sig_words(text: str) -> set:
            return {
                w.lower() for w in re.findall(r'\w+', text)
                if len(w) > 2 and w.lower() not in self._TITLE_STOP_WORDS
            }
        # Canonicalise multi-word report terms to the abbreviation the synonym map keys on.
        q_words = sig_words(query.lower().replace("dana pihak ketiga", "dpk"))
        if not q_words:
            return False
        title_words = sig_words(self.title)
        return all(
            w in title_words or (self._SUBJECT_SYNONYMS.get(w, frozenset()) & title_words)
            for w in q_words
        )

    def query_coverage(self, query: str, matched_label: str) -> float:
        """Share of a claim's significant words this table accounts for, title AND row together.

        Row labels alone cannot rank sources once a report's own tables are all in the pool:
        'Korporasi' is a row of BOTH "Tabel 4. Penghimpunan Dana Pihak Ketiga Berdasarkan
        Golongan Nasabah" and "Tabel 5. Perkembangan Kredit Berdasarkan Golongan Debitur", and
        it matches "DPK korporasi" equally well in each, so the claim was answered with credit
        growth (14,5%) instead of DPK growth (16,3%). The TITLE is what separates them, and the
        same reading fixes "kredit investasi UMKM" resolving to Lampiran 4's economy-wide
        Kredit Investasi row instead of Tabel 8's UMKM breakdown.

        Scope words are load-bearing here, so this ranks ABOVE label quality in
        paired_verifier._evaluate_fact: a source that ignores 'UMKM' answers a different
        question, however well its row name reads.
        """
        q_words = {
            w for w in _sig_words(query.lower().replace("dana pihak ketiga", "dpk"))
            if w not in self._TITLE_STOP_WORDS and not w.isdigit()
        }
        if not q_words:
            return 1.0
        known = _label_words(matched_label) | _label_words(self.title)
        covered = {
            w for w in q_words
            if w in known or (self._SUBJECT_SYNONYMS.get(w, frozenset()) & known)
        }
        # A term _SUBJECT_SYNONYMS knows about names what the report is TALKING ABOUT, not a
        # detail of it. A source that mentions it nowhere is answering a different question, so
        # it scores zero rather than partial credit and _evaluate_fact drops it: "DPK korporasi"
        # was being answered by Tabel 5's 'Korporasi' row — a CREDIT table — because the row
        # name alone matched perfectly. The DPK table's own Korporasi rows are dropped as
        # ambiguous (three rows share the name), so the honest answer there is "not enough data".
        if any(w in self._SUBJECT_SYNONYMS for w in q_words - covered):
            return 0.0
        return len(covered) / len(q_words)

    def available_periods(self, query: str) -> List[Tuple[int, str]]:
        """Return all (year, month) pairs that have data for the label closest matching query.

        Uses the same tier-1/2/3 label matching as lookup_fuzzy so that diagnostics
        are consistent with what the comparison step would attempt.
        """
        if self.axis_type != "temporal":
            return []  # categorical keys are 2-tuples — there are no periods to report
        matched = self._resolve_label(query)
        if matched is None:
            return []
        return sorted({(y, m) for (l, y, m) in self._data if l == matched})

    def _match_tiers_over(self, query: str, labels: List[str]):
        """Yield candidate labels (from `labels`) for query, best tier first.

        Tier order (each tier yields (label, sort_key) candidates; the caller takes the first
        tier that produces a usable match, picking min(sort_key)):

          1. Case-insensitive exact equality.
          2. Query contained in label — the label is the fuller official name of what the query
             names ("simpanan berjangka" → "Simpanan Berjangka (Rupiah dan Valas)"). SHORTEST
             label wins (closest to the query).
          3. Leaf of a parent-qualified label ('Simpanan Berjangka ... > Rupiah' → 'rupiah')
             contained in the query. Ranked by how many significant words of the FULL
             qualified label appear in the query (desc), so "tabungan lainnya rupiah" picks
             Tabungan Lainnya's Rupiah sub-row over Simpanan Berjangka's, then by leaf
             length. Runs BEFORE the bare label-in-query tier: a leaf match corroborated by
             parent words ("simpanan berjangka valuta asing" → SB's Valuta Asing) must beat
             a short generic label that merely appears in the query ('Simpanan').
          4. Label contained in query — a verbose query embeds an exact label name. LONGEST
             label wins (most specific). This direction is kept LAST and specificity-ranked
             because it is the dangerous one: a short generic row like 'Simpanan' (a nested
             sub-item of a different section) is contained in "simpanan berjangka" and, when
             ranked shortest-first in the same pool as tier 2, shadowed the correct row —
             observed on BI I.1, producing a false Refuted against the negative 'Simpanan'
             liability row. Candidates here are additionally filtered by
             _query_is_about_the_label: the words the query adds must not name a different
             quantity from the one the row holds.

        Containment (not shared-prefix) in the full-label tiers keeps distinct metrics that
        merely start alike apart: "Uang Beredar Digital" binds to nothing ("Uang Beredar
        Luas(M2)" neither contains it nor is contained by it).

        All comparisons run on _canon forms so cosmetic spacing/punctuation differences
        between narrative wording and sheet wording do not defeat containment.
        """
        q_canon = _canon(query)
        q_tight = _tight(query)
        q_words = _sig_words(query)
        tier_exact, tier_q_in_l, tier_l_in_q, tier_leaf = [], [], [], []
        tier_tight_q_in_l, tier_tight_words = [], []
        for label in labels:
            l_canon = _canon(label)
            l_tight = _tight(label)
            if l_canon == q_canon:
                tier_exact.append((label, 0))
            if q_canon and q_canon in l_canon:
                tier_q_in_l.append((label, len(label)))
            if l_canon and l_canon in q_canon and self._query_is_about_the_label(query, label):
                # Ranked by how much of what the TITLE does not already say the row accounts
                # for, then by specificity. "kredit properti KPR dan KPA" contains both the
                # 'Kredit Properti' row and the 'KPR dan KPA' row of Tabel 7, and plain
                # longest-wins picked the first — answering a 4,8% claim with the 17,5% total.
                # The title already says "Kredit Properti", so those words separate nothing;
                # what is left of the claim ('kpr', 'kpa') is what the row has to earn.
                residual = q_words - _label_words(self.title) - self._TITLE_STOP_WORDS
                earned = residual & _label_words(label)
                # A row that adds nothing the title did not already say is the table's own
                # subject, not the breakdown the claim asked about. "kredit properti real
                # estat" against Tabel 7 ('Kredit Properti' is both the title and a row) was
                # answered with the 17,5% total instead of real estate's 13,9%. Dropping such
                # a candidate lets the looser tiers below reach the row that does earn its
                # place — and where none does, the claim honestly comes back unresolved.
                if residual and not earned and _label_words(label) <= _label_words(self.title):
                    continue
                tier_l_in_q.append((label, (-len(earned), -len(label))))
            if QUAL_SEP in label:
                leaf = _canon(label.rsplit(QUAL_SEP, 1)[1])
                if leaf and leaf in q_canon:
                    overlap = len(_sig_words(label) & q_words)
                    tier_leaf.append((label, (-overlap, -len(leaf))))
            # The last two tiers ignore where the spaces fell (see _tight / _label_words), so a
            # label the PDF broke mid-word can still be recognised. They run LAST: a label that
            # matches on its real words always wins over one that only matches once the stray
            # spaces are taken out.
            if q_tight and q_tight in l_tight:
                tier_tight_q_in_l.append((label, len(label)))
            elif QUAL_SEP not in label and self._query_is_about_the_label(query, label):
                # Word coverage rather than containment, because the two spellings rarely nest:
                # the claim says "Uang Primer (M0) adjusted" while Lampiran 6's row reads 'Uang
                # Prim er Adjusted 1)' — neither contains the other, so a containment test
                # settled for the plain 'Uang Prim er' row above it and checked M0 adjusted
                # against unadjusted uang primer (Rp1.798,9 vs Rp2.232,2 triliun). Ranking by
                # how much of the CLAIM a row accounts for picks the adjusted row instead.
                # Qualified labels are excluded above: 'IKLK > Usia >60 th' shares every
                # significant word with a claim about 'IKLK > Usia >41 tahun' — only the
                # digits differ, and those are not significant words — so word coverage
                # would bind the claim to the wrong age group. Tier 3 resolves qualified
                # labels properly, by their leaf.
                covered = _label_words(label) & q_words
                if covered and _label_is_covered_by(label, q_words, self._TITLE_STOP_WORDS):
                    tier_tight_words.append(
                        (label, (-len(covered), -_tight_score(query, label), len(label)))
                    )
        return [
            tier_exact, tier_q_in_l, tier_leaf, tier_l_in_q,
            tier_tight_q_in_l, tier_tight_words,
        ]

    def _match_tiers(self, query: str):
        return self._match_tiers_over(query, self.row_labels)

    # Least share of a query's significant words a bare label-in-query match must account for.
    # At exactly half, "pertumbuhan kredit" still binds to 'Kredit'; at a third, "suku bunga
    # simpanan berjangka tenor 1 bulan" no longer binds to 'Simpanan Berjangka'.
    _LABEL_IN_QUERY_MIN_COVERAGE: ClassVar[float] = 0.5

    # A claim often carries the period it is about into the metric name ("pertumbuhan giro
    # valas pada Januari"). Such words say WHEN, never WHAT, so they neither corroborate nor
    # contradict a row label. Bare years are dropped alongside them (see _query_is_about_the_label).
    _PERIOD_WORDS: ClassVar[frozenset] = frozenset({
        "januari", "februari", "maret", "april", "mei", "juni", "juli", "agustus",
        "september", "oktober", "november", "desember",
        "jan", "feb", "mar", "apr", "jun", "jul", "agu", "ags", "sep", "okt", "nov", "des",
        "january", "february", "march", "june", "july", "august", "october", "december",
        "triwulan", "kuartal", "semester", "tahun", "bulan", "yoy",
    })

    def _query_is_about_the_label(self, query: str, label: str) -> bool:
        """False when the words a query adds beyond the label name a DIFFERENT quantity.

        Guards the label-in-query tier, the permissive one: any short row label that happens
        to appear inside a longer claim binds to it. On the M2 report that produced ten
        confident false Refuteds in one run — "suku bunga simpanan berjangka tenor 1 bulan
        4,20%" was answered by the 'Simpanan Berjangka' row of a %-yoy DPK table (3,7), and
        "DPK nasabah lainnya" by a determinants row merely called 'Lainnya'. The report
        carries no interest-rate table at all, so the honest answer was 'no data'.

        The extra words are acceptable when the TABLE's own title accounts for them — they
        are then context ("pertumbuhan kredit" against a table titled "Pertumbuhan …"), not a
        new subject. Otherwise the label must still cover most of the query on its own.
        """
        q_words = {
            w for w in _sig_words(query) - self._TITLE_STOP_WORDS - self._PERIOD_WORDS
            if not w.isdigit()
        }
        if not q_words:
            return True
        covered = q_words & _label_words(label)
        leftover = q_words - covered - _label_words(self.title)
        if not leftover:
            return True
        return len(covered) >= self._LABEL_IN_QUERY_MIN_COVERAGE * len(q_words)

    @staticmethod
    def _qualifier_kept(query: str, label: str) -> bool:
        """False when an explicitly qualified query resolved to a label that drops its
        qualifier entirely.

        A query carrying QUAL_SEP means the extractor deliberately named a sub-dimension —
        'Indeks Ketersediaan Lapangan Kerja (IKLK) > tingkat pendidikan lainnya', 'IEKU >
        Pengeluaran Rp4,1-5 juta'. The containment tiers will still bind such a query to the
        bare parent row ('… (IKLK)'), because the label IS contained in the query, and the
        claim then gets checked against the national aggregate instead of the group it is
        about — a confidently wrong verdict. Require MOST of the query leaf's significant
        words to survive in the match; a single shared word is not enough, or 'IKLK >
        tingkat pendidikan lainnya' would settle for a row merely called 'Lainnya'. When
        nothing qualifies, no row in this table answers the claim and it is better left
        unresolved than answered by the wrong series.
        """
        if QUAL_SEP not in query:
            return True
        leaf_words = _sig_words(query.rsplit(QUAL_SEP, 1)[1])
        if not leaf_words:
            return True
        return 2 * len(leaf_words & _label_words(label)) >= len(leaf_words)

    def _resolve_label(self, query: str) -> Optional[str]:
        """Return the best matching row label for query, or None if nothing matches."""
        if query in self.row_labels:
            return query
        for tier in self._match_tiers(query):
            kept = [t for t in tier if self._qualifier_kept(query, t[0])]
            if kept:
                return min(kept, key=lambda t: t[1])[0]
        if self.title and self._query_matches_table_subject(query):
            for label in self.row_labels:
                if label.strip().lower() in self._TOTAL_ROW_NAMES:
                    return label
        return None

    def lookup_fuzzy(
        self, query: str, year: int, month: str
    ) -> Tuple[Optional[str], Optional[float]]:
        """Return (matched_label, value) using exact → tiered containment → Total fallback.

        See _match_tiers_over for the tier order and why the two containment directions must
        not share one pool. Within a tier, only labels that actually have data for
        (year, month) are considered, so a better-named but data-less row never blocks a
        usable one. The final fallback handles tables where the overall metric (e.g.
        'Cadangan Devisa') is not a row label but IS the table's subject (from self.title),
        and the aggregate is stored in a row simply called 'Total'.
        """
        # Exact
        v = self._data.get((query, year, month))
        if v is not None:
            return query, v
        for tier in self._match_tiers(query):
            with_data = [
                (label, key) for label, key in tier
                if self._data.get((label, year, month)) is not None
                and self._qualifier_kept(query, label)
            ]
            if with_data:
                best = min(with_data, key=lambda t: t[1])[0]
                return best, self._data[(best, year, month)]
        # Title-aware total-row fallback: query describes this table's subject → return the
        # aggregate row (BI labels it 'Total' or 'Jumlah' depending on the table).
        if self.title and self._query_matches_table_subject(query):
            for label in self.row_labels:
                if label.strip().lower() in self._TOTAL_ROW_NAMES:
                    v = self._data.get((label, year, month))
                    if v is not None:
                        return label, v
        return None, None

    # ------------------------------------------------------------------
    # Categorical lookups
    # ------------------------------------------------------------------

    def lookup_cell(self, row_label: str, col_label: str) -> Optional[float]:
        return self._data.get((row_label, col_label))

    def lookup_cell_fuzzy(
        self, row_query: str, col_query: str
    ) -> Tuple[Optional[str], Optional[str], Optional[float]]:
        """Return (matched_row, matched_col, value) fuzzy-matching BOTH axes.

        Row candidates are tried tier by tier (same ordering as lookup_fuzzy); for each row
        candidate the column query is resolved with the same tier machinery, restricted to
        columns that actually hold data for that row — so a plausible row name never blocks
        the lookup just because the best-guess column is empty for it.
        """
        v = self._data.get((row_query, col_query))
        if v is not None:
            return row_query, col_query, v
        for row_tier in self._match_tiers_over(row_query, self.row_labels):
            row_tier = [t for t in row_tier if self._qualifier_kept(row_query, t[0])]
            for row_label, _ in sorted(row_tier, key=lambda t: t[1]):
                for col_tier in self._match_tiers_over(col_query, self.col_labels):
                    with_data = [
                        (col, key) for col, key in col_tier
                        if (row_label, col) in self._data
                    ]
                    if with_data:
                        best_col = min(with_data, key=lambda t: t[1])[0]
                        return row_label, best_col, self._data[(row_label, best_col)]
        return None, None, None
