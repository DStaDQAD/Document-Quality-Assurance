"""A strict claim judged on the cells it names. Nothing is matched by words and no guard second-
guesses the row: the cells are read by exact key, the arithmetic is the old engine's (units,
rounding as printed, signed comparison, the stability band), and every result lists its cells.
"""
from typing import Dict, List, Optional, Tuple

from cell_formula import FormulaError, evaluate
from paired_verifier import (
    MATCH_TOLERANCE,
    _growth_tolerance,
    _numeric_verdict,
    _parse_scale_unit,
    _stable_band,
    _unit_factor,
    _value_verdict,
)
from schemas import FactVerificationResult, PeriodResult
from strict.claims import Cell, Claim
from strict.tables import TableView, period_header

_TRENDS = {"is_increasing", "is_decreasing", "is_stable"}
_SEP = " > "


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
        value = view.table._data.get((cell.row, *cell.key))
        if value is None:
            raise _Undecided("periode", f"Tabel tidak memuat sel '{cell.code} · {cell.row} · "
                                        f"{period_header(*cell.key)}'.")
        out.append((cell, view, value))
    return out


def _missing_members(read: List[Read]) -> List[str]:
    """Rows of a breakdown the claim says it covers whole but does not cite: same table, same
    parent, same depth. Rows without a parent name no breakdown and are not checked."""
    missing = []
    for view in {id(v): v for _, v, _ in read}.values():
        cited = {c.row for c, v, _ in read if v is view}
        for parent, depth in {(r.rsplit(_SEP, 1)[0], r.count(_SEP)) for r in cited if _SEP in r}:
            missing += [r for r in view.table.row_labels
                        if r not in cited and _SEP in r and r.rsplit(_SEP, 1)[0] == parent
                        and r.count(_SEP) == depth]
    return list(dict.fromkeys(missing))


def _common(read: List[Read]) -> List[float]:
    """Every cell's value in the first cell's table unit (a ratio of ULN in Juta USD to PDB in
    Miliar USD). Units with no common scale cannot be combined."""
    unit = read[0][1].table.unit
    base = _parse_scale_unit(unit)
    values = []
    for _, view, value in read:
        if view.table.unit == unit:
            values.append(value)
            continue
        other = _parse_scale_unit(view.table.unit)
        if base is None or other is None or base[1] != other[1]:
            raise _Undecided("jenis", f"Satuan sel tidak bisa digabung: '{unit}' dan '{view.table.unit}'.")
        values.append(value * other[0] / base[0])
    return values


def _factor(claim: Claim, table_unit: str) -> float:
    """What a number in the claim's unit is multiplied by to be in the table's unit."""
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
    return claim.claimed_value


def _derived(claim: Claim, computed: float) -> Tuple[float, float, str, str]:
    delta, verdict = _numeric_verdict(_claimed(claim), round(computed, 4))
    return round(computed, 4), delta, verdict, f"Δ = {delta} (toleransi {MATCH_TOLERANCE})"


def _per_row(read: List[Read], values: List[float]) -> Dict[str, List[float]]:
    rows: Dict[str, List[float]] = {}
    for (cell, _, _), value in zip(read, values):
        rows.setdefault(cell.row, []).append(value)
    return rows


def _judge(claim: Claim, read: List[Read]) -> Tuple[Optional[float], Optional[float], str, str]:
    """(computed value, delta, verdict, explanation) for the claim's operation."""
    op, unit = claim.operation, read[0][1].table.unit
    if op == "value" or (op == "yoy_growth" and len(read) == 1):
        if len(read) != 1:
            raise _Undecided("klaim_tidak_lengkap", "Klaim nilai harus menyebut tepat satu sel.")
        computed = read[0][2] / _factor(claim, unit)
        delta, verdict, tail = _value_verdict(_claimed(claim), computed, claim.context_quote)
        return round(computed, 4), delta, verdict, tail
    values = _common(read)
    if op == "yoy_growth":
        if len(values) != 2 or values[0] == 0:
            raise _Undecided("klaim_tidak_lengkap", "Pertumbuhan yoy butuh dua sel: tahun lalu, lalu sekarang.")
        growth = (values[1] / values[0] - 1) * 100
        tolerance = _growth_tolerance(values[1], values[0], _claimed(claim), claim.context_quote)
        delta, verdict = _numeric_verdict(_claimed(claim), round(growth, 4), tolerance)
        return round(growth, 4), delta, verdict, f"Δ = {delta} (toleransi {round(tolerance, 4)})"
    if op == "diff":
        if len(values) != 2:
            raise _Undecided("klaim_tidak_lengkap", "Selisih butuh dua sel: sebelum, lalu sesudah.")
        return _derived(claim, (values[1] - values[0]) / _factor(claim, unit))
    if op in ("sum", "average"):
        total = sum(values) / (len(values) if op == "average" else 1)
        return _derived(claim, total / _factor(claim, unit))
    if op == "ratio":
        if len(values) < 2 or values[-1] == 0:
            raise _Undecided("klaim_tidak_lengkap", "Rasio butuh pembilang dan penyebut.")
        return _derived(claim, sum(values[:-1]) / values[-1] * 100)
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
    if op == "formula":
        try:
            result = evaluate(claim.formula or "", values)
        except (FormulaError, ZeroDivisionError) as exc:
            raise _Undecided("klaim_tidak_lengkap", f"Rumus AI tidak bisa dihitung: {claim.formula!r} ({exc}).")
        if isinstance(result, bool):
            return None, None, "Entailed" if result else "Refuted", f"rumus {claim.formula} = {'benar' if result else 'salah'}"
        if claim.formula_unit == "tabel":
            result = result / _factor(claim, unit)
        computed, delta, verdict, tail = _derived(claim, result)
        return computed, delta, verdict, f"rumus {claim.formula} = {computed}; {tail}"
    raise _Undecided("klaim_tidak_lengkap", f"Operasi tidak dikenal: {op!r}.")


def evaluate_claim(claim: Claim, views: List[TableView]) -> FactVerificationResult:
    """The claim's verdict from the cells it names, with those cells in `periods` and the reasoning."""
    base = dict(operation=claim.operation, metric_label=claim.subject or "-", claimed_value=claim.claimed_value,
                claimed_unit=claim.unit, context_quote=claim.context_quote, page_number=claim.page_number,
                resolved_via="strict")
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
