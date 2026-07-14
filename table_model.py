"""Generic two-axis table container shared by every table parser.

A parsed table is modelled as (row_label, column_key) -> float where the column axis is one
of two kinds:

  temporal    : column key = (year, month) — the BI time-series case. Internally stored as
                the 3-tuple key (row_label, year, month) so existing callers, tests and eval
                fixtures that populate `_data` directly keep working unchanged.
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
        "dan", "di", "ke", "dari", "untuk", "yang", "pada", "atau", "dalam",
        "dengan", "oleh", "atas", "total", "jumlah", "posisi", "indonesia", "bank",
        "penghimpunan", "penyaluran", "pertumbuhan", "perkembangan", "tercatat",
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
             liability row.

        Containment (not shared-prefix) in the full-label tiers keeps distinct metrics that
        merely start alike apart: "Uang Beredar Digital" binds to nothing ("Uang Beredar
        Luas(M2)" neither contains it nor is contained by it).
        """
        q_lower = query.lower().strip()
        q_words = _sig_words(q_lower)
        tier_exact = [(label, 0) for label in labels if label.lower().strip() == q_lower]
        tier_q_in_l = [(label, len(label)) for label in labels if q_lower in label.lower()]
        tier_l_in_q = [(label, -len(label)) for label in labels if label.lower().strip() in q_lower]
        tier_leaf = []
        for label in labels:
            if QUAL_SEP not in label:
                continue
            leaf = label.rsplit(QUAL_SEP, 1)[1].lower().strip()
            if leaf and leaf in q_lower:
                overlap = len(_sig_words(label) & q_words)
                tier_leaf.append((label, (-overlap, -len(leaf))))
        return [tier_exact, tier_q_in_l, tier_leaf, tier_l_in_q]

    def _match_tiers(self, query: str):
        return self._match_tiers_over(query, self.row_labels)

    def _resolve_label(self, query: str) -> Optional[str]:
        """Return the best matching row label for query, or None if nothing matches."""
        if query in self.row_labels:
            return query
        for tier in self._match_tiers(query):
            if tier:
                return min(tier, key=lambda t: t[1])[0]
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
