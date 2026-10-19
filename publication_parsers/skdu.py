"""SKDU: 'T1 Kegiatan Usaha' .. 'T10 Margin Usaha' (plus 'T7b Investasi Semesteran').

The generic reader already reads every sheet of this workbook correctly (audit 2026-10-07), so
this parser wraps it: what it adds is coverage, a shape check, the Indonesian half of each
sheet's bilingual unit cell, the report's own words for some rows and tables, and the
golden-number tests.
"""
import re

from publication_parsers._common import parse_with_generic_reader
from table_model import TableData

SHEET_PATTERNS = (r"T\d+b? .+",)

# The report's own names for a sheet's rows, by sheet; set only when the sheet has the row.
# SKDU Tw II-2026: "tekanan harga pada level produsen terindikasi meningkat … dan diprakirakan
# menurun pada triwulan III" is the selling-price balance of Tabel 5 (15,82 -> 22,38 -> 15,94) —
# the respondents are the producers. Kept to that sheet: on every sheet 'TOTAL' would answer it
# with that sheet's own total. SKDU Tw III-2026: "akses kredit yang tetap mudah" is Tabel 3's
# balance of easy over hard answers (1,84); the reader leaves that row without its 'Akses
# kredit' parent, so the only row naming access was 'Normal', one answer's share.
TERM_ALIASES_BY_SHEET = (
    (r"T5\b", {
        "tekanan harga pada level produsen": "TOTAL",
        "tekanan harga di tingkat produsen": "TOTAL",
        "tekanan harga di level produsen": "TOTAL",
        "tekanan harga produsen": "TOTAL",
    }),
    (r"T3\b", {
        "akses kredit": "Saldo Bersih (% Mudah - % Sulit)",
        "akses kredit perbankan": "Saldo Bersih (% Mudah - % Sulit)",
    }),
)

# "Kinerja sejumlah LU tercatat meningkat, antara lain LU Industri Pengolahan, …": the report's
# word for Tabel 1 'Kegiatan Usaha'. Tabel 1, 4, 5, 6 and 7 list the same sectors, so a list of
# sectors is read on Tabel 1 only when the sentence names it (paired_verifier._components_of_one_total).
SUBJECT_WORDS_BY_SHEET = ((r"T1\b", frozenset({"kinerja"})),)

# Tabel 3's section headings, which head its rows as a level: layout, not a group, so "likuiditas
# membaik" is '… > - Likuiditas > Saldo Bersih (% Baik - % Buruk)' itself.
LAYOUT_LEVELS_BY_SHEET = ((r"T3\b", frozenset({
    "kondisi keuangan selama 3 bulan terakhir :", "akses kredit selama 3 bulan terakhir :",
    "masalah dalam memperoleh kredit :",
})),)

# A balance sheet whose rows carry no 'Saldo Bersih' level: the extractor's "Pertanian,
# Kehutanan, dan Perikanan > Saldo Bersih" (copied from Tabel 8's row names) is the sector's row.
MEASURE_LEVELS = frozenset({"saldo bersih", "saldo bersih tertimbang", "sbt"})


def parse(data: bytes, sheet_name: str) -> TableData:
    table = parse_with_generic_reader(data, sheet_name)
    for pattern, aliases in TERM_ALIASES_BY_SHEET:
        if re.match(pattern, sheet_name) and set(aliases.values()) <= set(table.row_labels):
            table.term_aliases = dict(aliases)
    for pattern, words in SUBJECT_WORDS_BY_SHEET:
        if re.match(pattern, sheet_name):
            table.subject_words = words
    for pattern, levels in LAYOUT_LEVELS_BY_SHEET:
        if re.match(pattern, sheet_name):
            table.layout_levels = levels
    if re.search(r"saldo\s+bersih", table.unit or "", re.IGNORECASE) and not any(
            "saldo bersih" in label.lower() for label in table.row_labels):
        table.measure_levels = MEASURE_LEVELS
    return table
