"""Uang Beredar (M2): SEKI Tabel I.1 and I.1A (reklasifikasi), read by the proven SEKI reader.

The historical sheets of TABEL1_1.xls ('Th 1985-1992' ...) are not covered.
"""
from publication_parsers._common import parse_with_bi_reader
from table_model import TableData

SHEET_PATTERNS = (r"I\.1A?",)

# The report's sections about data I.1 / I.1A do not hold. They have their own tables in the
# report (DPK Tabel 3–4, kredit Tabel 5–8, M0 Tabel 9, suku bunga Grafik 6), but their words
# overlap M2's rows: "simpanan berjangka tumbuh 4,7%" under the DPK heading is DPK's (Tabel 3),
# not M2's 'Simpanan Berjangka (Rupiah dan Valas)' (4,55%) — a false Tidak Sesuai on UB Agustus
# 2026, as was M0's "uang kartal yang diedarkan" matched to M2's currency outside banks.
UNCOVERED_SECTIONS = {
    r"\bdana pihak ketiga\b|\bdpk\b": "Dana Pihak Ketiga (DPK)",
    r"\bkredit\b": "kredit perbankan",
    r"\buang primer\b|\bm0\b": "Uang Primer (M0)",
    r"\bsuku bunga\b": "suku bunga",
}


def parse(data: bytes, sheet_name: str) -> TableData:
    table = parse_with_bi_reader(data, sheet_name)
    table.uncovered_sections = dict(UNCOVERED_SECTIONS)
    return table
