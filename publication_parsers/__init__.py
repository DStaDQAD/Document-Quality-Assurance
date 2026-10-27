"""Per-publication Excel parsers — one module per BI publication.

Each module declares SHEET_PATTERNS (regexes matched against the whole, stripped sheet name,
case-insensitive) and parse(data, sheet_name) -> TableData, raising PublicationParseError when
a covered sheet does not have the layout it expects. paired_verifier tries the parser of the
publication the checker picked before its generic cascade (see _parse_table_with_fallback).
Design: docs/superpowers/specs/2026-10-07-publication-parsers-design.md
"""
import os
import re
from pathlib import Path
from types import ModuleType
from typing import Dict, List, Optional, Sequence

import yaml

from publication_parsers import cadangan_devisa, npi, pii, pmi, ppkom, sbank, shpr, sk, skdu, spe, sulni, uang_beredar, uang_primer
from publication_parsers._common import PublicationParseError, load_grid, sheet_matches
from table_model import TableData

PARSERS: Dict[str, ModuleType] = {
    "uang-beredar": uang_beredar,
    "uang-primer-m0": uang_primer,
    "cadangan-devisa": cadangan_devisa,
    "sulni": sulni,
    "npi": npi,
    "pii": pii,
    "sk": sk,
    "skdu": skdu,
    "spe": spe,
    "sbank": sbank,
    "shpr": shpr,
    "pmi": pmi,
    "ppkom": ppkom,
}

__all__ = [
    "PARSERS", "PublicationParseError", "covers", "load_grid", "notes_enabled", "notes_for", "notes_sections",
    "parse_for_publication", "skipped_sections",
]

# Publication notes (docs/specs/publication-notes.md): <module>.notes.yaml next to each parser, in
# Indonesian. Sections in the order the extractor reads them, with their heading in the prompt.
NOTES_SECTIONS = (
    ("conventions", "Konvensi"),
    ("phrases", "Frasa yang biasa dipakai → baris tabel"),
    ("periods", "Periode"),
    ("units_signs", "Satuan dan tanda"),
)
# They go into every chunk's prompt: ~600 words is ~800 tokens a call.
NOTES_MAX_WORDS = 600
# A quoted phrase in a YAML phrase line: '"IPR", "penjualan eceran" → INDEKS TOTAL.'
_QUOTED = re.compile(r'"([^"]+)"')


def covers(publication: str, sheet_name: str) -> bool:
    """True when `publication` has a parser and it claims this sheet."""
    module = PARSERS.get(publication or "")
    return module is not None and any(
        sheet_matches(pattern, sheet_name) for pattern in module.SHEET_PATTERNS
    )


def parse_for_publication(publication: str, data: bytes, sheet_name: str) -> Optional[TableData]:
    """The publication's own reading of the sheet; None when it has no parser for this sheet."""
    if not covers(publication, sheet_name):
        return None
    return PARSERS[publication].parse(data, sheet_name)


def skipped_sections(publication: str) -> tuple:
    """Headings (regexes on the whole heading line) of report sections the extractor is not shown:
    they hold no claim about the data, only figures that look like one (SBank's METODOLOGI:
    "Nilai SBT > 0 berarti lebih ketat")."""
    module = PARSERS.get(publication or "")
    return tuple(getattr(module, "SKIPPED_SECTIONS", ()))


def notes_enabled() -> bool:
    """PUBLICATION_NOTES=1/on/true/yes shows the chosen publication's notes to the extractor."""
    return os.getenv("PUBLICATION_NOTES", "").strip().lower() in ("1", "on", "true", "yes")


def notes_path(publication: str) -> Optional[Path]:
    """<module>.notes.yaml next to the publication's parser; None for an unknown publication."""
    module = PARSERS.get(publication or "")
    if module is None:
        return None
    return Path(module.__file__).with_name(module.__name__.rsplit(".", 1)[-1] + ".notes.yaml")


def _notes_file(publication: str) -> Optional[dict]:
    """The publication's notes file, checked for unknown sections; None when it has none."""
    path = notes_path(publication)
    if path is None or not path.exists():
        return None
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    known = {key for key, _ in NOTES_SECTIONS}
    unknown = sorted(set(data) - {"publication", "title"} - known)
    if unknown:
        raise ValueError(
            f"Catatan publikasi {path.name}: bagian tidak dikenal {unknown}; "
            f"yang boleh: publication, title, {', '.join(sorted(known))}."
        )
    return data


def notes_sections(publication: str) -> Dict[str, List[str]]:
    """The notes file's entries per section, in NOTES_SECTIONS order; [] for a missing section."""
    data = _notes_file(publication) or {}
    return {key: [str(entry) for entry in data.get(key) or []] for key, _ in NOTES_SECTIONS}


def notes_for(publication: str, team_phrases: Optional[Dict[str, str]] = None,
              team_conventions: Sequence[str] = ()) -> str:
    """The publication's notes as a prompt block, ending in a blank line; "" when it has none.

    Guidance only: they tell the extractor what the publication's usual wording means, and nothing
    reads them to refuse a claim. The parser's TERM_ALIASES are listed under the phrases, so an
    alias is written once. The team's phrases and conventions (Pengaturan › Catatan) join their
    sections; a team phrase replaces the parser's line for the same phrase.
    """
    data = _notes_file(publication)
    team_phrases = {" ".join(p.lower().split()): row for p, row in (team_phrases or {}).items()}
    if data is None and not (team_phrases or team_conventions):
        return ""
    data = data or {}
    lines = [f"PUBLIKASI: {data.get('title') or publication} — catatan analis tentang arti kata-kata "
             "yang biasa dipakai publikasi ini. Pakai hanya sebagai petunjuk membaca kalimat, tidak pernah "
             "sebagai alasan melewatkan klaim. Tetap ekstrak setiap klaim seperti biasa: juga kalimat tren "
             "tanpa angka (naik/turun/stabil), angka yang mungkin tidak ada di tabel, dan klaim yang tidak "
             "disebut di sini."]
    module = PARSERS.get(publication)
    for key, heading in NOTES_SECTIONS:
        entries = [str(entry) for entry in data.get(key) or []]
        if key == "conventions":
            entries += [str(text) for text in team_conventions]
        if key == "phrases":
            built_in = {phrase: row for phrase, row in getattr(module, "TERM_ALIASES", {}).items()
                        if phrase not in team_phrases}
            # A YAML line may name the team's phrase among others: the team's row says it wins.
            in_yaml = {q.lower() for entry in entries for q in _QUOTED.findall(entry)}
            entries += [f'"{phrase}" → baris \'{row}\'' for phrase, row in built_in.items()]
            entries += [f'"{phrase}" → baris \'{row}\'' + (" (padanan tim, berlaku di atas catatan bawaan untuk frasa ini)"
                                                           if phrase in in_yaml else "")
                        for phrase, row in team_phrases.items()]
        if entries:
            lines.append(f"{heading}:")
            lines += [f"- {entry}" for entry in entries]
    return "\n".join(lines) + "\n\n"
