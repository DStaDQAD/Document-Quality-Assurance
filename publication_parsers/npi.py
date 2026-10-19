"""NPI: SEKI Tabel V.1 (Neraca Pembayaran Indonesia, quarterly, .xls).

Labels in column 2 nest by the label cell's indent ('I. Transaksi Berjalan' > 'A. Barang' >
'- Ekspor, fob'); years in row 4 over 'Q1'..'Q4' in row 5; an English mirror on the right.
The SEKI monthly reader cannot read it (no month row), hence the engine.

The internal release workbook ('NPI Tw. II-2026 R0.xlsx', sheet 'Id', English twin 'En')
stacks TABEL 1..8 on one sheet: roman sections in column B, every other label in column C
with its outline marker ('A.', '1.', 'a.', '1)', '-'), 'Q1' or 'Tw.I' under each year.
"""
from publication_parsers._common import SheetSpec, parse_with_specs
from table_model import TableData

SPECS = (
    (r"5\.1", SheetSpec(label_cols=(2,), hierarchy="indent")),
    (r"Id", SheetSpec(label_cols=(1, 2), hierarchy="outline",
                      sum_missing_leaves=("Penerimaan", "Pembayaran"))),
)
SHEET_PATTERNS = tuple(pattern for pattern, _ in SPECS)

# The report's fixed names for rows the internal workbook labels otherwise — BPM6 / BI usage in
# every NPI release, checked against NPI Tw II-2026 (laporan + tables): wisman 3,4 -> 4,0 juta
# = pelawat ke dalam negeri 3.436 -> 4.017 ribu; "Jumlah PMI 4,3 juta" = TKI 4.282 ribu;
# "investasi langsung asing 5,0" = the liability side 4.970; foreign net selling in the stock
# market 3,1 = private-sector portfolio equity liabilities -3.135; "neraca perdagangan migas"
# -5,1 = general merchandise oil & gas -5.087; NPI = the overall balance. Phrases only: a bare
# "PMI" is also the Prompt Manufacturing Index in the same report. "Neraca perdagangan
# nonmigas" is the memo balance (MEMO_BALANCES), not the general-merchandise row (12,1).
TERM_ALIASES = {
    "neraca perdagangan nonmigas": "Neraca perdagangan nonmigas (memorandum)",
    "wisatawan mancanegara": "Ke dalam negeri",
    "kunjungan wisman": "Ke dalam negeri",
    "wisman": "Ke dalam negeri",
    "wisatawan nasional": "Jumlah pelawat > Ke luar negeri",
    "kunjungan wisnas": "Jumlah pelawat > Ke luar negeri",
    "wisnas": "Jumlah pelawat > Ke luar negeri",
    "jumlah pmi": "Jumlah Tenaga Kerja Indonesia/TKI (ribuan orang)",
    "jumlah pekerja migran indonesia": "Jumlah Tenaga Kerja Indonesia/TKI (ribuan orang)",
    "investasi langsung asing": "Investasi Langsung > Kewajiban",
    "aliran masuk investasi langsung": "Investasi Langsung > Kewajiban",
    "aliran dana asing ke pasar saham domestik": "Kewajiban > Sektor swasta > Modal ekuitas",
    "aliran dana asing di pasar saham domestik": "Kewajiban > Sektor swasta > Modal ekuitas",
    "neraca perdagangan migas": "Barang Dagangan Umum > Migas",
    "neraca pendapatan investasi langsung": "Pendapatan investasi langsung",
    # Income paid on direct investment = the 'Pembayaran' the parser sums from equity + interest
    # (6,3 -> 6,5). The receipts side is left out: the report's 0,6 -> 0,7 fits equity income
    # alone (0,58 -> 0,74), not the sum (0,63 -> 0,77), so no fixed definition can be written.
    "pembayaran imbal hasil atas investasi langsung": "Pendapatan investasi langsung > Pembayaran",
    # Coupons are paid on debt securities: "pembayaran kupon/bunga menurun sejalan dengan pembayaran
    # kupon surat berharga internasional pemerintah" = portfolio interest paid (2.711 -> 2.216).
    # Not a bare "bunga", which is also direct-investment and loan interest.
    "pembayaran kupon/bunga": "Pendapatan investasi portofolio > Pendapatan utang (bunga) > Pembayaran",
    "pembayaran kupon": "Pendapatan investasi portofolio > Pendapatan utang (bunga) > Pembayaran",
    "neraca pembayaran indonesia": "Neraca Keseluruhan",
    "npi": "Neraca Keseluruhan",
    # Residents buying foreign securities = portfolio investment, asset side (-1.488 = 1,5).
    "pembelian neto surat berharga di luar negeri": "Transaksi Finansial > Investasi Portofolio > Aset",
    "surat berharga di luar negeri": "Transaksi Finansial > Investasi Portofolio > Aset",
}


# Balances the report computes from the memorandum rows, which the workbook never prints:
# "neraca perdagangan nonmigas surplus 11,5" (Tw II-2026) = total nonmigas exports 70.471 plus
# total nonmigas imports -58.970 (imports are stored negative).
MEMO_BALANCES = {
    "Neraca perdagangan nonmigas (memorandum)": (
        "Nominal > Total Ekspor (fob) > Nonmigas", "Nominal > Total Impor (fob) > Nonmigas"),
}


def _add_memo_balances(table: TableData) -> None:
    for name, (exports, imports) in MEMO_BALANCES.items():
        if exports not in table.row_labels or imports not in table.row_labels:
            continue
        added = False
        for key, value in list(table._data.items()):
            if key[0] != exports:
                continue
            other = table._data.get((imports,) + key[1:])
            if other is not None:
                table._data[(name,) + key[1:]] = round(value + other, 6)
                added = True
        if added:
            table.row_labels.append(name)


# Breakdowns the report discusses but the workbook never splits out, as {pattern: what it is}.
# Tw II-2026: "impor minyak mentah 3,8" was judged on all oil imports (11,6, crude plus refined
# products) and "kenaikan ekspor NG" on all gas exports (LNG plus NG) — both refuted, though the
# workbook simply has no such row. A row that does name the item ('Harga rata-rata ekspor minyak
# mentah', 'Produksi minyak mentah') still answers.
ABSENT_ITEMS = {
    r"\bminyak\s+mentah\b": "minyak mentah",
    r"\bproduk\s+kilang\b": "produk kilang",
    r"\bnatural\s+gas\b|\b(?-i:NG)\b": "natural gas (NG)",
    r"\bliqu[ei]fied\s+natural\s+gas\b|\b(?-i:LNG)\b": "LNG",
}


# Rows the parser names by their own word alone (it keeps the shortest name that is unique), but
# that the report only ever names with their parent: "defisit jasa transportasi penumpang" found
# no row in Tw II-2026, while 'Transportasi > Barang' (named in full because 'Barang' repeats)
# was found. Child rows ('Penumpang > Ekspor') get the parent too.
WITH_PARENT = {"Penumpang": "Transportasi"}


def _add_parents(table: TableData) -> None:
    renamed = {}
    for label in table.row_labels:
        head = label.split(" > ")[0]
        if head in WITH_PARENT:
            renamed[label] = f"{WITH_PARENT[head]} > {label}"
    if not renamed or any(new in table.row_labels for new in renamed.values()):
        return
    table.row_labels = [renamed.get(label, label) for label in table.row_labels]
    table._data = {(renamed.get(key[0], key[0]),) + key[1:]: value
                   for key, value in table._data.items()}
    table.row_units = {renamed.get(label, label): unit for label, unit in table.row_units.items()}


def parse(data: bytes, sheet_name: str) -> TableData:
    table = parse_with_specs(data, sheet_name, SPECS)
    _add_parents(table)
    _add_memo_balances(table)
    table.term_aliases = dict(TERM_ALIASES)
    table.absent_items = dict(ABSENT_ITEMS)
    return table
