"""PII: SEKI Tabel V.39 (Posisi Investasi Internasional, quarterly, .xls).

Same layout as NPI: labels in column 2 nested by indent ('Aset' > 'Investasi Langsung' >
'Modal Ekuitas'), years over 'Q1'..'Q4', annual columns for the early years (skipped).

The report speaks of two figures the sheet does not print, both derived here from its own rows:
each component's NET position ("investasi portofolio mencatat kewajiban neto sebesar 229,6") and
the external debt ("posisi total ULN Indonesia (posisi KFLN dalam instrumen utang) … 453,4").
"""
from publication_parsers._common import SheetSpec, parse_with_specs
from table_model import QUAL_SEP, TableData

SPECS = ((r"5\.39", SheetSpec(label_cols=(2,), hierarchy="indent")),)
SHEET_PATTERNS = tuple(pattern for pattern, _ in SPECS)

# The functional categories the report gives a net position for, each on both sides of the sheet.
NET_COMPONENTS = ("Investasi Langsung", "Investasi Portofolio", "Derivatif Finansial",
                  "Investasi Lainnya")

# The report's footnote: "Utang mencakup instrumen Hak Tarik Khusus, Simpanan, Surat Utang,
# Pinjaman (termasuk utang afiliasi), dan Utang Lainnya" — every liability but equity and
# derivatives (Tw II-2026: 47,15 + 209,62 + 196,60 = 453,37 -> "453,4").
ULN_ROW = "Utang Luar Negeri (ULN)"
ULN_PARTS = (
    f"Kewajiban{QUAL_SEP}Investasi Langsung{QUAL_SEP}Instrumen Utang",
    f"Kewajiban{QUAL_SEP}Investasi Portofolio{QUAL_SEP}Surat Utang",
    f"Kewajiban{QUAL_SEP}Investasi Lainnya",
)
# "posisi KFLN dalam instrumen utang" is the report's own gloss on ULN; its 'instrumen utang'
# otherwise reached the 'Ke luar negeri > Instrumen utang' row of the memorandum (51,3).
TERM_ALIASES = {
    "utang luar negeri (uln)": ULN_ROW,
    "utang luar negeri": ULN_ROW,
    "posisi kfln dalam instrumen utang": ULN_ROW,
    "kfln dalam instrumen utang": ULN_ROW,
}


# Rows that are already a net position, and the gross rows that stand for them. The memorandum
# nets direct investment by direction ('Ke luar negeri' − 'Di Indonesia (PMA)'); reserve assets
# have no liability side. Tw II-2026: "investasi langsung mencatat kewajiban neto sebesar 149,1"
# came back once as 'Di Indonesia (PMA)' (294,1), and "cadangan devisa mencatat aset neto" as
# 'Cadangan Devisa' itself.
DIRECTIONAL_NET = "Investasi langsung berdasarkan arah investasi"
NET_ALIASES = {
    "ke luar negeri": DIRECTIONAL_NET,
    "di indonesia (pma)": DIRECTIONAL_NET,
    DIRECTIONAL_NET.lower(): DIRECTIONAL_NET,
    "cadangan devisa": "Cadangan Devisa",
}


def parse(data: bytes, sheet_name: str) -> TableData:
    table = parse_with_specs(data, sheet_name, SPECS)
    _add_net_rows(table)
    table.net_rows.update({key: row for key, row in NET_ALIASES.items() if row in table.row_labels})
    if _add_sum_row(table, ULN_ROW, ULN_PARTS):
        table.term_aliases = dict(TERM_ALIASES)
    return table


def _add_net_rows(table: TableData) -> None:
    """'<component> (neto)' = Aset − Kewajiban, signed like 'Posisi Investasi Internasional,
    bersih' (negative = net liabilities).

    Kept out of row_labels: no lookup reaches them, so a gross claim ("posisi AFLN investasi
    portofolio … 43,5") can never land on one. paired_verifier._net_row_instead moves a claim to
    one only when its number is a net position (_is_a_net_figure) and it resolved to a side.
    """
    for component in NET_COMPONENTS:
        asset, liability = f"Aset{QUAL_SEP}{component}", f"Kewajiban{QUAL_SEP}{component}"
        if asset not in table.row_labels or liability not in table.row_labels:
            continue
        net = f"{component} (neto)"
        for key, value in list(table._data.items()):
            if len(key) != 3 or key[0] != asset:
                continue
            other = table._data.get((liability, key[1], key[2]))
            if other is not None:
                table._data[(net, key[1], key[2])] = value - other
        table.net_rows[component.lower()] = net


def _add_sum_row(table: TableData, row: str, parts) -> bool:
    if not all(part in table.row_labels for part in parts):
        return False
    periods = {key[1:] for key in table._data if len(key) == 3 and key[0] == parts[0]}
    for period in periods:
        values = [table._data.get((part, *period)) for part in parts]
        if all(v is not None for v in values):
            table._data[(row, *period)] = sum(values)
    table.row_labels.append(row)
    return True
