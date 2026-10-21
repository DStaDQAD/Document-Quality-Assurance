"""PPKom (Survei Properti Komersial): segment-by-city claims, Denpasar's headless 'Jual' row, and
chart captions run into a sentence. Synthetic tables shaped like PPKom Tw I-2026."""
from unittest import mock

from paired_verifier import _ExcelSource, _evaluate_fact
from publication_parsers import ppkom
from structured_extractor import ExtractedFact, PeriodPoint
from table_model import TableData


def _sheet(name, rows):
    """`rows` as the generic reader returns them (city rows still under their number)."""
    table = TableData(title=f"{name}. Perkembangan Indeks", unit="", row_labels=list(rows))
    for label, (q4, q1) in rows.items():
        table._data[(label, 2025, "Q4")] = q4
        table._data[(label, 2026, "Q1")] = q1
    with mock.patch.object(ppkom, "parse_with_generic_reader", return_value=table):
        return ppkom.parse(b"", name)


PRICE = {"Sewa": (102.2, 100.6), "Jual": (104.8, 104.9), "1 > JAKARTA": (102.91, 102.90),
         "1 > Convention Hall": (157.43, 156.19), "1 > Hotel": (98.78, 95.19)}
SUPPLY = {"Sewa": (116.3, 117.5), "Jual": (108.572, 108.571), "1 > JAKARTA": (100.0, 100.4),
          "1 > Convention Hall": (100.0, 100.0), "1 > Hotel": (115.2, 127.6)}


def test_a_segment_in_a_city_is_that_citys_segment_row():
    table = _sheet("Tabel 1", PRICE)
    query = "Indeks Harga Properti Komersial kategori sewa > convention hall > Jakarta"
    assert table.lookup_fuzzy(query, 2026, "Q1")[0] == "JAKARTA > Convention Hall"


def test_another_measures_claim_finds_no_row():
    # "pengembangan hotel di Jakarta" (supply) was answered by the price tables.
    table = _sheet("Tabel 1", PRICE)
    query = "Indeks Pasokan Properti Komersial kategori sewa > hotel > Jakarta"
    assert table.lookup_fuzzy(query, 2026, "Q1") == (None, None)


def test_the_index_name_alone_is_the_national_row():
    table = _sheet("Tabel 7", SUPPLY)
    assert table.lookup_fuzzy("Indeks Pasokan Properti Komersial kategori jual", 2026, "Q1")[0] == "Jual"


def test_a_headless_category_row_does_not_adopt_the_segments():
    grid = {"10 > DENPASAR": (101.0, 102.0), "10 > Sewa": (101.0, 102.0),
            "10 > Jual > Hotel": (127.6, 118.3), "10 > Jual > Ritel Sewa": (70.0, 66.2)}
    table = _sheet("Tabel 2", grid)
    assert "DENPASAR > Hotel" in table.row_labels and "DENPASAR > Ritel Sewa" in table.row_labels
    assert table._data[("DENPASAR > Hotel", 2026, "Q1")] == 118.3


def test_a_chart_caption_in_the_sentence_does_not_pick_the_growth_table():
    # Tw I-2026: "Hal ini disebabkan oleh penurunan permintaan pada segmen convention hall di
    # Jakarta Grafik 1 Pertumbuhan Tahunan Indeks … (%, yoy)" — the index fell 85,1 -> 70,2.
    level = TableData(title="Tabel 13. Perkembangan Indeks Permintaan Properti Komersial", unit="Indeks",
                      row_labels=["JAKARTA > Convention Hall"])
    level._data = {("JAKARTA > Convention Hall", 2025, "Q4"): 85.06,
                   ("JAKARTA > Convention Hall", 2026, "Q1"): 70.21}
    growth = TableData(title="Tabel 17. Pertumbuhan Tahunan Indeks Permintaan Properti Komersial",
                       unit="%, yoy", row_labels=["JAKARTA > Convention Hall"])
    growth._data = {("JAKARTA > Convention Hall", 2025, "Q4"): -26.02,
                    ("JAKARTA > Convention Hall", 2026, "Q1"): -2.68}
    quote = ("Hal ini disebabkan oleh penurunan permintaan pada segmen convention hall di Jakarta "
             "Grafik 1 Pertumbuhan Tahunan Indeks Permintaan Properti Komersial Kategori Jual (%, yoy)")
    point = lambda y, q: PeriodPoint("Jakarta > Convention Hall", y, q)  # noqa: E731
    fact = ExtractedFact("is_decreasing", [point(2025, "Q4"), point(2026, "Q1")], None, None, quote)
    # Workbook order; the caption's 'pertumbuhan tahunan' used to lift Tabel 17 over Tabel 13.
    sources = [_ExcelSource(table=level, filename="p.xlsx", sheet="Tabel 13"),
               _ExcelSource(table=growth, filename="p.xlsx", sheet="Tabel 17")]
    result = _evaluate_fact(fact, sources)
    assert result.verdict == "Entailed" and result.matched_excel_source.endswith("Tabel 13")
