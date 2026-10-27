"""A strict claim: the extractor's answer (StrictClaim) and the finished claim the engine judges (Claim).

The extractor names cells by table code, row and period as it was shown them; the finished claim
keeps what it wrote next to what that resolved to, so a wrong reference can be shown as written.
"""
from dataclasses import asdict, dataclass
from typing import List, Literal, Optional, Tuple

from pydantic import BaseModel, Field

Operation = Literal[
    "value", "yoy_growth", "diff", "ratio", "sum", "average",
    "is_increasing", "is_decreasing", "is_stable",
    "above_threshold", "below_threshold", "ranking", "formula",
]


class CellRef(BaseModel):
    table: str = Field(description="Kode tabel persis, mis. 'S1'")
    row: str = Field(description="Nama baris persis seperti di daftar TABLES")
    period: str = Field(description="Periode persis seperti tabel menulisnya, mis. 'Q4 2025' atau 'Jun 2026'")


class StrictClaim(BaseModel):
    # Field order matters: an answer is generated top to bottom, so the model writes what the
    # sentence is about, and whether the tables hold it at all, before it names any cell.
    anchor_quote: str = Field(description="3-8 kata berurutan disalin persis dari teks, memuat angka klaim")
    subject: str = Field(description="Apa yang diukur, disalin dari kalimat")
    not_in_tables: bool = Field(False, description="True bila tidak ada tabel yang memuat data ini")
    reason: Optional[str] = Field(None, description="Alasan singkat bila not_in_tables")
    operation: Operation = Field(description="Operasi klaim; 'formula' bila menu tidak bisa mengatakannya")
    cells: List[CellRef] = Field(default_factory=list, description="Sel yang dibutuhkan klaim, urut sesuai operasinya")
    all_members: bool = Field(False, description="True bila kalimat membahas SETIAP anggota rincian (seluruh/semua/12 dari 18)")
    size_of_negative: bool = Field(
        False, description="True bila kalimat menyebut BESARNYA saldo yang di tabel tercatat negatif (defisit, impor, "
                           "pembayaran, kewajiban neto): angkanya tanpa minus, dibandingkan dengan besar sel")
    ranked: Optional[int] = Field(
        None, description="Hanya untuk operation='ranking': berapa anggota yang disebut kalimat (selnya di depan)")
    rank: Optional[Literal["highest", "lowest"]] = Field(
        None, description="Hanya untuk operation='ranking': yang disebut 'highest' (tertinggi/terbesar naik) atau "
                          "'lowest' (terendah/penurunan terbesar)")
    formula: Optional[str] = Field(None, description="Hanya untuk operation='formula': rumus atas c1..cn")
    formula_unit: Optional[Literal["tabel", "persen", "jumlah"]] = Field(
        None, description="Satuan hasil rumus: 'tabel', 'persen', atau 'jumlah' (hitungan)")
    claimed_value_raw: Optional[str] = Field(
        None, description="Angka di kalimat disalin persis; minus untuk turun/kontraksi; ambang untuk above/below")
    unit: Optional[str] = Field(None, description="Satuan angka klaim, mis. 'miliar USD', 'persen'")


class StrictClaims(BaseModel):
    """Always a JSON object with the 'claims' key, never a bare array."""
    claims: List[StrictClaim] = Field(default_factory=list)


@dataclass
class Cell:
    """A cited cell: as the extractor wrote it, and what it resolved to (None when it did not)."""
    code: str
    row_text: str
    period_text: str
    row: Optional[str] = None
    key: Optional[Tuple[int, str]] = None


@dataclass
class Claim:
    operation: str
    subject: str
    cells: List[Cell]
    context_quote: str
    page_number: Optional[int] = None
    claimed_value: Optional[float] = None
    unit: Optional[str] = None
    formula: Optional[str] = None
    formula_unit: Optional[str] = None
    ranked: Optional[int] = None
    rank: Optional[str] = None
    all_members: bool = False
    size_of_negative: bool = False
    not_in_tables: bool = False
    reason: Optional[str] = None


def claim_to_dict(claim: Claim) -> dict:
    return asdict(claim)


def claim_from_dict(data: dict) -> Claim:
    cells = [Cell(code=c["code"], row_text=c["row_text"], period_text=c["period_text"], row=c.get("row"),
                  key=tuple(c["key"]) if c.get("key") else None)
             for c in data.get("cells") or []]
    rest = {k: v for k, v in data.items() if k != "cells"}
    return Claim(cells=cells, **rest)
