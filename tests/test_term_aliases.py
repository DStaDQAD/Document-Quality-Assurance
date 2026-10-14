"""Official term aliases: a publication's own name for a row its tables never print.

The press release says "ULN publik"; the table's row is 'Pemerintah dan Bank Sentral'. That is
BI's fixed definition, not an interpretation, so the publication parser carries it and the
lookup reads the claim in the table's own words. Tables here are synthetic.
"""

from excel_parser_bi import BITableData
from paired_verifier import _ExcelSource, _evaluate_fact
from structured_extractor import ExtractedFact, PeriodPoint


def _table(aliases=None):
    table = BITableData(title="Posisi Utang Luar Negeri (ULN) Indonesia", unit="Juta USD",
                        row_labels=["TOTAL", "Pemerintah dan Bank Sentral", "Swasta"])
    for label, (jun, jul) in {"TOTAL": (900.0, 910.0), "Pemerintah dan Bank Sentral": (500.0, 520.0),
                              "Swasta": (400.0, 390.0)}.items():
        table._data[(label, 2026, "Jun")] = jun
        table._data[(label, 2026, "Jul")] = jul
    if aliases:
        table.term_aliases = aliases
    return table


ALIASES = {"uln publik": "Pemerintah dan Bank Sentral"}


def _trend(operation, label="ULN publik"):
    return ExtractedFact(operation, [PeriodPoint(label, 2026, "Jul")], None, None,
                         "Perkembangan ini dipengaruhi oleh peningkatan ULN publik.")


def test_a_table_without_the_alias_does_not_know_the_term():
    assert _table().lookup_fuzzy("ULN publik", 2026, "Jul") == (None, None)


def test_the_alias_finds_the_row_the_publication_means():
    assert _table(ALIASES).lookup_fuzzy("ULN publik", 2026, "Jul") == ("Pemerintah dan Bank Sentral", 520.0)


def test_the_alias_matches_regardless_of_case_and_spacing():
    assert _table(ALIASES).lookup_fuzzy("ULN  Publik", 2026, "Jul")[0] == "Pemerintah dan Bank Sentral"


def test_a_true_claim_under_the_alias_is_entailed_and_a_flipped_one_refuted():
    src = _ExcelSource(table=_table(ALIASES), filename="t.xlsx", sheet="T")
    assert _evaluate_fact(_trend("is_increasing"), [src]).verdict == "Entailed"
    assert _evaluate_fact(_trend("is_decreasing"), [src]).verdict == "Refuted"


def test_the_sulni_parser_carries_the_official_definition(monkeypatch):
    import publication_parsers.sulni as sulni
    monkeypatch.setattr(sulni, "parse_with_specs", lambda data, sheet, specs: _table())
    table = sulni.parse(b"", "TabI.1")
    assert table.lookup_fuzzy("ULN publik", 2026, "Jul")[0] == "Pemerintah dan Bank Sentral"
    assert table.lookup_fuzzy("ULN sektor publik", 2026, "Jul")[0] == "Pemerintah dan Bank Sentral"


def test_the_sk_parser_carries_its_report_wording(monkeypatch):
    """SK writes "proporsi pembayaran cicilan/utang" and "pendapatan yang ditabung" for its
    'Cicilan pinjaman' and 'Tabungan' rows; a claim labelled that way found no row."""
    import publication_parsers.sk as sk
    table = BITableData(title="Komposisi penggunaan pendapatan", unit="%", row_labels=[])
    for label, value in (("Total > Cicilan pinjaman", 9.7), ("Total > Tabungan", 15.4),
                         ("Kelompok A > Cicilan pinjaman", 8.0)):
        table.row_labels.append(label)
        table._data[(label, 2026, "Sep")] = value
    monkeypatch.setattr(sk, "parse_with_generic_reader", lambda data, sheet, unit=None: table)
    parsed = sk.parse(b"", "Tabel 5")
    assert parsed.lookup_fuzzy("pembayaran cicilan", 2026, "Sep")[0] == "Total > Cicilan pinjaman"
    assert parsed.lookup_fuzzy("Proporsi pembayaran cicilan/utang", 2026, "Sep")[0] == "Total > Cicilan pinjaman"
    assert parsed.lookup_fuzzy("Proporsi pendapatan konsumen yang disimpan", 2026, "Sep")[0] == "Total > Tabungan"
