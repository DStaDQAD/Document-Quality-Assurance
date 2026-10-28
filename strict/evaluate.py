"""A strict claim judged on the cells it names. Nothing is matched by words and no guard second-
guesses the row: the cells are read by exact key, the arithmetic is the old engine's (units,
rounding as printed, signed comparison, the stability band), and every result lists its cells.
"""
from decimal import ROUND_HALF_UP, Decimal
from typing import Dict, List, Optional, Tuple

from cell_formula import FormulaError, evaluate
from paired_verifier import (
    MATCH_TOLERANCE,
    _is_growth_series,
    _is_percent_unit,
    _is_plain_index,
    _numeric_verdict,
    _parse_scale_unit,
    _period_ordinal,
    _printed_decimals,
    _stable_band,
    _unit_factor,
    _value_verdict,
)
from schemas import FactVerificationResult, PeriodResult
from strict.claims import Cell, Claim
from strict.tables import ANNUAL, TableView, period_header

_TRENDS = {"is_increasing", "is_decreasing", "is_stable"}
_SEP = " > "
# A breakdown's total is not one of its members (SKDU 'TOTAL > Saldo Bersih' beside each LU's).
_AGGREGATES = {"total", "jumlah"}


class _Undecided(Exception):
    """The claim cannot be judged: Tidak Cukup Data with this reason code and explanation."""

    def __init__(self, code: str, why: str):
        super().__init__(why)
        self.code, self.why = code, why


Read = Tuple[Cell, TableView, float]


def _read(claim: Claim, views: Dict[str, TableView]) -> List[Read]:
    if not claim.cells:
        raise _Undecided("klaim_tidak_lengkap", "Klaim tidak menyebut sel tabel mana pun.")
    out = []
    for cell in claim.cells:
        written = f"{cell.code} · {cell.row_text} · {cell.period_text}"
        view = views.get((cell.code or "").strip().upper())
        if view is None or cell.row is None:
            raise _Undecided("tidak_ditemukan", f"Sel yang disebut AI tidak ada di tabel: '{written}'.")
        if cell.key is None:
            raise _Undecided("periode", f"Periode yang disebut AI tidak dikenali: '{written}'.")
        if cell.key[1] == ANNUAL:
            value = (view.table.annual or {}).get((cell.row, cell.key[0]))
        else:
            value = view.table._data.get((cell.row, *cell.key))
        if value is None:
            raise _Undecided("periode", f"Tabel tidak memuat sel '{cell.code} · {cell.row} · "
                                        f"{period_header(*cell.key)}'.")
        out.append((cell, view, value))
    return out


def _missing_members(read: List[Read]) -> List[str]:
    """Rows of a breakdown the claim says it covers whole but does not cite. The breakdown is the
    one level where the cited rows differ ('TAHUNAN (YOY) > <kota> > TOTAL': the city); its members
    are the table's rows that match the cited ones on every other level. Nothing is checked when
    the rows differ on no level or on several, or when they are flat (no level to hold fixed)."""
    missing = []
    for view in {id(v): v for _, v, _ in read}.values():
        cited = list(dict.fromkeys(c.row for c, v, _ in read if v is view))
        parts = [row.split(_SEP) for row in cited]
        depth = len(parts[0])
        if len(cited) < 2 or depth < 2 or any(len(p) != depth for p in parts):
            continue
        varying = [i for i in range(depth) if len({p[i] for p in parts}) > 1]
        if len(varying) != 1:
            continue
        fixed = {i: parts[0][i] for i in range(depth) if i != varying[0]}
        for row in view.table.row_labels:
            levels = row.split(_SEP)
            if (row not in cited and len(levels) == depth
                    and all(levels[i] == text for i, text in fixed.items())
                    and levels[varying[0]].strip().lower() not in _AGGREGATES):
                missing.append(row)
    return list(dict.fromkeys(missing))


def _unit(view: TableView, row: str) -> str:
    """A cell's unit: its row's own (NPI 'Transaksi Berjalan (% PDB)') or the table's."""
    return (view.table.row_units or {}).get(row) or view.table.unit


def _ordered(read: List[Read]) -> List[Read]:
    """Cells of one row, oldest first: the sign of a change must not hang on the order the model
    happened to list them in. Cells of several rows keep the model's order (numerator, denominator)."""
    if len({cell.row for cell, _, _ in read}) != 1:
        return read
    return sorted(read, key=lambda r: (r[0].key[0], _period_ordinal(r[0].key[1])))


def _common(read: List[Read], claim: Claim, sizes: bool) -> List[float]:
    """Every cell's value in the first cell's unit (a ratio of ULN in Juta USD to PDB in Miliar
    USD); with `sizes`, each cell as its size. Units with no common scale cannot be combined."""
    unit = _unit(read[0][1], read[0][0].row)
    base = _parse_scale_unit(unit)
    values = []
    for cell, view, value in read:
        own = _unit(view, cell.row)
        if own != unit:
            other = _parse_scale_unit(own)
            if base is None or other is None or base[1] != other[1]:
                raise _Undecided("jenis", f"Satuan sel tidak bisa digabung: '{unit}' dan '{own}'.")
            value = value * other[0] / base[0]
        values.append(abs(value) if sizes else value)
    return values


# Where the size of a negative balance is taken. Per cell when the claim is about that one balance
# over time ("impor meningkat", "defisit melebar 3,0"); of the result when the cells are combined
# first ("kewajiban neto 252,9" = Kewajiban − Aset, "transaksi modal dan finansial defisit 8,0" =
# 68,8 + −8.031,3), where a size per cell changes the sum itself.
_SIZE_OF_RESULT = {"sum", "average", "ratio", "formula"}


def _factor(claim: Claim, table_unit: str) -> float:
    """What a number in the claim's unit is multiplied by to be in the table's unit. A level claim
    on a growth table, or a percent claim on an index read as a level, compares different kinds
    of number (15,7 / 1e12; 0,40 against 114,01): that is `jenis`, as in the old engine."""
    if _is_growth_series(table_unit) and claim.unit and _parse_scale_unit(claim.unit) is not None:
        raise _Undecided("jenis", f"Tabel bersatuan '{table_unit}' (pertumbuhan), klaim nilai dalam '{claim.unit}'.")
    if _is_percent_unit(claim.unit) and _is_plain_index(table_unit):
        raise _Undecided("jenis", f"Tabel berisi level indeks ('{table_unit}'); klaim dalam persen.")
    factor = _unit_factor(claim.unit, table_unit)
    if factor is not None:
        return factor
    if _parse_scale_unit(table_unit) is None:
        scale = _parse_scale_unit(claim.unit) if claim.unit else None
        return scale[0] if scale else 1.0
    raise _Undecided("jenis", f"Satuan klaim '{claim.unit}' tidak bisa dibandingkan dengan '{table_unit}'.")


def _claimed(claim: Claim) -> float:
    if claim.claimed_value is None:
        raise _Undecided("klaim_tidak_lengkap", "Klaim tidak memuat angka untuk dibandingkan.")
    # The size of a negative balance has no sign, whatever sign the model wrote on it.
    return abs(claim.claimed_value) if claim.size_of_negative else claim.claimed_value


def _derived(claim: Claim, computed: float) -> Tuple[float, float, str, str]:
    """A computed number judged by rounding the final result to the decimals the sentence prints
    (half-up, as Excel shows it); printed with none, it must be exact ("23 juta" against 22,78 is
    Tidak Sesuai: the user's rule). No allowance for levels the table already rounded: the user
    would rather see those flagged. Only a sentence that does not show how it printed the number
    falls back to MATCH_TOLERANCE."""
    claimed = _claimed(claim)
    delta, verdict = _numeric_verdict(claimed, round(computed, 4))
    decimals = _printed_decimals(claimed, claim.context_quote)
    if decimals is None:
        return round(computed, 4), delta, verdict, f"Δ = {delta} (toleransi {MATCH_TOLERANCE})"
    if decimals == 0:
        same = round(computed, 6) == claimed
        return round(computed, 4), delta, "Entailed" if same else "Refuted", (
            f"angka tanpa desimal harus sama persis → {'sama dengan' if same else 'berbeda dari'} klaim")
    step = Decimal(1).scaleb(-decimals)
    shown = Decimal(repr(round(computed, 10))).quantize(step, rounding=ROUND_HALF_UP)
    same = shown == Decimal(repr(claimed)).quantize(step, rounding=ROUND_HALF_UP)
    return round(computed, 4), delta, "Entailed" if same else "Refuted", (
        f"hasil dibulatkan ke {decimals} desimal seperti di laporan = {f'{shown:f}'.replace('.', ',')} → "
        f"{'sama dengan' if same else 'berbeda dari'} klaim")


def _per_row(read: List[Read], values: List[float]) -> Dict[str, List[float]]:
    rows: Dict[str, List[float]] = {}
    for (cell, _, _), value in zip(read, values):
        rows.setdefault(cell.row, []).append(value)
    return rows


def _number(x: float) -> str:
    text = str(round(x, 2))
    text = text[:-2] if text.endswith(".0") else text
    return text.replace("-", "−").replace(".", ",")


def _ranking(claim: Claim, read: List[Read], values: List[float]) -> Tuple[None, None, str, str]:
    """'tertinggi pada A, B dan C' / 'penurunan terbesar di A dan B': the named members (listed
    first, `ranked` of them) against every other member. A member is one row: one cell is its level,
    two cells its change (later minus earlier, whatever order they were listed in). The comparison
    is built here, never written by the model: the lowest of the named is at least the highest of
    the rest ('highest'), or the other way round ('lowest')."""
    measure: Dict[Tuple[str, str], float] = {}
    if claim.rank_pairs:
        # A net position per member: two rows at one period, the first minus the second
        # (Kewajiban − Aset), in the order the model listed them.
        if len(read) % 2 or any(read[i][0].key != read[i + 1][0].key for i in range(0, len(read), 2)):
            raise _Undecided("klaim_tidak_lengkap", "Peringkat selisih butuh dua sel pada periode yang sama "
                                                    "untuk setiap anggota.")
        for i in range(0, len(read), 2):
            measure[(read[i][0].code, f"{read[i][0].row} − {read[i + 1][0].row}")] = values[i] - values[i + 1]
    else:
        members: Dict[Tuple[str, str], List[Tuple[Cell, float]]] = {}
        for (cell, _, _), value in zip(read, values):
            members.setdefault((cell.code, cell.row), []).append((cell, value))
        sizes = {len(cells) for cells in members.values()}
        if len(sizes) != 1 or sizes.pop() not in (1, 2):
            raise _Undecided("klaim_tidak_lengkap", "Peringkat butuh satu sel (level) atau dua sel (perubahan) "
                                                    "untuk setiap anggota, sama untuk semuanya.")
        for key, cells in members.items():
            cells = sorted(cells, key=lambda cv: (cv[0].key[0], _period_ordinal(cv[0].key[1])))
            measure[key] = cells[0][1] if len(cells) == 1 else cells[1][1] - cells[0][1]
    ordered = list(measure)
    named, rest = ordered[:claim.ranked or 0], ordered[claim.ranked or 0:]
    if not named or not rest or claim.rank not in ("highest", "lowest"):
        raise _Undecided("klaim_tidak_lengkap", "Peringkat butuh anggota yang disebut, anggota lainnya, "
                                                "dan arahnya (tertinggi atau terendah).")
    if claim.rank == "highest":
        edge, other = min(named, key=measure.get), max(rest, key=measure.get)
        holds = measure[edge] >= measure[other]
    else:
        edge, other = max(named, key=measure.get), min(rest, key=measure.get)
        holds = measure[edge] <= measure[other]
    word = "tertinggi" if claim.rank == "highest" else "terendah"
    listed = "; ".join(f"{key[1]} {_number(measure[key])}" for key in named)
    tail = (f"disebut ({word}): {listed} | {word} di antara lainnya: {other[1]} {_number(measure[other])} "
            f"→ {'sesuai' if holds else 'tidak sesuai'} dengan klaim")
    return None, None, "Entailed" if holds else "Refuted", tail


def _judge(claim: Claim, read: List[Read]) -> Tuple[Optional[float], Optional[float], str, str]:
    """(computed value, delta, verdict, explanation) for the claim's operation."""
    op, unit = claim.operation, _unit(read[0][1], read[0][0].row)
    if op == "value" or (op == "yoy_growth" and len(read) == 1):
        if len(read) != 1:
            raise _Undecided("klaim_tidak_lengkap", "Klaim nilai harus menyebut tepat satu sel.")
        cell_value = abs(read[0][2]) if claim.size_of_negative else read[0][2]
        computed = cell_value / _factor(claim, unit)
        delta, verdict, tail = _value_verdict(_claimed(claim), computed, claim.context_quote)
        return round(computed, 4), delta, verdict, tail
    if op in ("diff", "yoy_growth"):
        read = _ordered(read)
    elif op in _TRENDS:
        first_seen = {row: i for i, row in reversed(list(enumerate(c.row for c, _, _ in read)))}
        read = sorted(read, key=lambda r: (first_seen[r[0].row], r[0].key[0], _period_ordinal(r[0].key[1])))
    of_result = claim.size_of_negative and op in _SIZE_OF_RESULT
    values = _common(read, claim, sizes=claim.size_of_negative and not of_result)

    def size(x: float) -> float:
        return abs(x) if of_result else x

    if op == "yoy_growth":
        if len(values) != 2 or values[0] == 0:
            raise _Undecided("klaim_tidak_lengkap", "Pertumbuhan yoy butuh dua sel: tahun lalu, lalu sekarang.")
        return _derived(claim, (values[1] / values[0] - 1) * 100)
    if op == "diff":
        if len(values) != 2:
            raise _Undecided("klaim_tidak_lengkap", "Selisih butuh dua sel: sebelum, lalu sesudah.")
        return _derived(claim, (values[1] - values[0]) / _factor(claim, unit))
    if op in ("sum", "average"):
        total = sum(values) / (len(values) if op == "average" else 1)
        return _derived(claim, size(total) / _factor(claim, unit))
    if op == "ratio":
        if len(values) < 2 or values[-1] == 0:
            raise _Undecided("klaim_tidak_lengkap", "Rasio butuh pembilang dan penyebut.")
        return _derived(claim, size(sum(values[:-1]) / values[-1] * 100))
    if op in _TRENDS:
        rows = _per_row(read, values)
        if any(len(series) < 2 for series in rows.values()):
            raise _Undecided("klaim_tidak_lengkap", "Tren butuh paling sedikit dua periode untuk setiap baris.")
        net = any("saldo bersih" in r.lower() for r in rows) or "sbt" in unit.lower()
        steps = [(a, b) for series in rows.values() for a, b in zip(series, series[1:])]
        if op == "is_increasing":
            holds = all(b > a for a, b in steps)
        elif op == "is_decreasing":
            holds = all(b < a for a, b in steps)
        else:
            holds = all(abs(b - a) <= _stable_band(a, b, net) for a, b in steps)
        return None, None, "Entailed" if holds else "Refuted", "sesuai dengan klaim" if holds else "tidak sesuai dengan klaim"
    if op in ("above_threshold", "below_threshold"):
        bound = _claimed(claim)
        holds = all((v > bound) if op == "above_threshold" else (v < bound) for v in values)
        side = "di atas" if op == "above_threshold" else "di bawah"
        return None, None, "Entailed" if holds else "Refuted", f"setiap sel {side} {bound}: {'ya' if holds else 'tidak'}"
    if op == "ranking":
        return _ranking(claim, read, values)
    if op == "formula":
        try:
            result = evaluate(claim.formula or "", values)
        except (FormulaError, ZeroDivisionError) as exc:
            raise _Undecided("klaim_tidak_lengkap", f"Rumus AI tidak bisa dihitung: {claim.formula!r} ({exc}).")
        if isinstance(result, bool):
            return None, None, "Entailed" if result else "Refuted", f"rumus {claim.formula} = {'benar' if result else 'salah'}"
        result = size(result)
        if claim.formula_unit == "tabel":
            result = result / _factor(claim, unit)
        computed, delta, verdict, tail = _derived(claim, result)
        return computed, delta, verdict, f"rumus {claim.formula} = {computed}; {tail}"
    raise _Undecided("klaim_tidak_lengkap", f"Operasi tidak dikenal: {op!r}.")


def evaluate_claim(claim: Claim, views: List[TableView]) -> FactVerificationResult:
    """The claim's verdict from the cells it names, with those cells in `periods` and the reasoning."""
    base = dict(operation=claim.operation, metric_label=claim.subject or "-", claimed_value=claim.claimed_value,
                claimed_unit=claim.unit, context_quote=claim.context_quote, page_number=claim.page_number,
                resolved_via="strict",
                # A claim about every member: the Tinjauan AI must not re-judge it on members it picks
                # (ai_review.review_candidates leaves group-scoped claims alone).
                group_scope="all" if claim.all_members else None)
    by_code = {v.code: v for v in views}
    try:
        if claim.not_in_tables:
            raise _Undecided("di_luar_tabel", "Menurut AI, data ini tidak ada di tabel yang diunggah: "
                                              f"{claim.reason or 'tanpa alasan'}.")
        read = _read(claim, by_code)
        if claim.all_members:
            missing = _missing_members(read)
            if missing:
                raise _Undecided("klaim_tidak_lengkap", "Kalimat membahas seluruh anggota, tetapi sel ini tidak "
                                                         "disebut: " + ", ".join(missing) + ".")
        computed, delta, verdict, tail = _judge(claim, read)
    except _Undecided as undecided:
        return FactVerificationResult(**base, verdict="Inconclusive", reason_code=undecided.code,
                                      reasoning=undecided.why)
    cells = "; ".join(f"{c.code} · {c.row} · {period_header(*c.key)} = {round(v, 4)}" for c, _, v in read)
    return FactVerificationResult(
        **base, matched_excel_source=read[0][1].source.label,
        periods=[PeriodResult(metric_label=c.row, year=c.key[0], month=c.key[1], excel_value=v) for c, _, v in read],
        computed_value=computed, delta=delta, verdict=verdict, reasoning=f"Sel: {cells} | {tail}",
    )
