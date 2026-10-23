"""Pengaturan — the team's shared settings per publication (docs/specs/settings-tab.md).

The 13 publications stay the ones in code (check_history.PUBLICATIONS, publication_parsers). Here
the team can hide one from the upload card, add aliases on top of a parser's built-in ones
(whole metric name only: TableData.label_aliases), and keep notes that the Tinjauan AI reads.
Notes are seeded once per publication from its *.notes.yaml. Every change is logged.

Stored next to Riwayat, through check_history's engine (Neon on Render, SQLite locally). A check
reads its publication's settings once (for_check); any failure there leaves the check on the
built-ins — settings never stop a check.
"""

import logging
import re
import threading
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set, Tuple

import yaml
from sqlalchemy import (
    Boolean, Column, DateTime, Integer, MetaData, String, Table, Text, delete, func, insert, select,
    update,
)

import check_history
from publication_parsers import PARSERS, notes_path

logger = logging.getLogger("fact-checker")

PUBLICATIONS = check_history.PUBLICATIONS
NOTES_MAX_WORDS = 600
PHRASE_MAX_CHARS = 200
ROW_MAX_CHARS = 300
NOTE_MAX_CHARS = 2000
CHANGES_SHOWN = 50
KNOWN_ROWS_KEPT = 3000
# The YAML sections that become notes. 'phrases' stays out: the parsers' aliases hold those.
_SEEDED_SECTIONS = ("conventions", "periods", "units_signs")
# Wording that made the extractor drop claims in run notes_1022 (spec: a warning, not a block).
_RISKY = re.compile(r"tidak (ada di|memuat)|abaikan|jangan diperiksa|belum dipetakan|bisa diperiksa",
                    re.IGNORECASE)

_metadata = MetaData()
publication_state = Table(
    "settings_publication", _metadata,
    Column("publication", String(32), primary_key=True),
    Column("hidden", Boolean, nullable=False),
    Column("updated_by", String(check_history.CHECKER_NAME_MAX_CHARS), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)
aliases = Table(
    "settings_alias", _metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("publication", String(32), nullable=False, index=True),
    Column("phrase", String(PHRASE_MAX_CHARS), nullable=False),
    Column("row", String(ROW_MAX_CHARS), nullable=False),
    Column("created_by", String(check_history.CHECKER_NAME_MAX_CHARS), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
notes_table = Table(
    "settings_note", _metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("publication", String(32), nullable=False, index=True),
    Column("position", Integer, nullable=False),
    Column("text", Text, nullable=False),
    Column("seeded", Boolean, nullable=False),
    Column("created_by", String(check_history.CHECKER_NAME_MAX_CHARS), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_by", String(check_history.CHECKER_NAME_MAX_CHARS)),
    Column("updated_at", DateTime(timezone=True)),
)
notes_seeded = Table(
    "settings_note_seeded", _metadata,
    Column("publication", String(32), primary_key=True),
    Column("seeded_at", DateTime(timezone=True), nullable=False),
)
known_rows_table = Table(
    "settings_known_row", _metadata,
    Column("publication", String(32), primary_key=True),
    Column("row", String(ROW_MAX_CHARS), primary_key=True),
    Column("last_seen", DateTime(timezone=True), nullable=False),
)
change_log = Table(
    "settings_change", _metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("at", DateTime(timezone=True), nullable=False, index=True),
    Column("by", String(check_history.CHECKER_NAME_MAX_CHARS), nullable=False),
    Column("publication", String(32), nullable=False, index=True),
    Column("kind", String(16), nullable=False),      # visibility | alias | note | reset
    Column("action", String(16), nullable=False),    # tambah | ubah | hapus | urutkan | sembunyikan | tampilkan
    Column("before", Text),
    Column("after", Text),
)

_ready: Set[str] = set()
_ready_lock = threading.Lock()


class SettingsError(ValueError):
    """A value the settings cannot take; the message is for the user, in Indonesian."""


class UnknownPublication(KeyError):
    """Not one of the 13 publications."""


def _engine():
    """check_history's engine, with this module's tables created on its first use."""
    engine = check_history._get_engine()
    # Keyed by the database, not id(engine): a disposed engine's id can come back on a new engine
    # for another database (the test suite makes one per test), whose tables would then be missing.
    key = engine.url.render_as_string(hide_password=False)
    with _ready_lock:
        if key not in _ready:
            _metadata.create_all(engine)
            _ready.add(key)
    return engine


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _publication(key: str) -> str:
    if key not in PUBLICATIONS:
        raise UnknownPublication(key)
    return key


def _editor(by: Optional[str]) -> str:
    name = check_history.normalize_checker_name(by)
    if not name:
        raise SettingsError("Isi nama Anda dulu; setiap perubahan dicatat dengan nama pengubahnya.")
    return name


def _norm(text: str) -> str:
    return " ".join((text or "").split())


def _log(conn, by: str, publication: str, kind: str, action: str, before: str = None, after: str = None):
    conn.execute(insert(change_log).values(at=_now(), by=by, publication=publication, kind=kind,
                                           action=action, before=before, after=after))


# --- publications --------------------------------------------------------------------------

def hidden_publications() -> Set[str]:
    with _engine().connect() as conn:
        return {r.publication for r in conn.execute(select(publication_state).where(publication_state.c.hidden))}


def set_hidden(publication: str, hidden: bool, by: str) -> None:
    publication, by = _publication(publication), _editor(by)
    with _engine().begin() as conn:
        exists = conn.execute(select(publication_state.c.publication)
                              .where(publication_state.c.publication == publication)).first()
        values = dict(hidden=bool(hidden), updated_by=by, updated_at=_now())
        if exists:
            conn.execute(update(publication_state).where(publication_state.c.publication == publication)
                         .values(**values))
        else:
            conn.execute(insert(publication_state).values(publication=publication, **values))
        _log(conn, by, publication, "visibility", "sembunyikan" if hidden else "tampilkan")


# --- aliases -------------------------------------------------------------------------------

def builtin_aliases(publication: str) -> List[Tuple[str, str]]:
    """(phrase, row) from the publication's parser: every module-level dict named *ALIASES, and
    SKDU's per-sheet ones (*_BY_SHEET: (pattern, dict) pairs). Read-only in the page."""
    module = PARSERS[_publication(publication)]
    found: Dict[str, str] = {}
    for name in dir(module):
        value = getattr(module, name)
        if name.endswith("ALIASES") and isinstance(value, dict):
            found.update({str(k): str(v) for k, v in value.items()})
        elif name.endswith("ALIASES_BY_SHEET") and isinstance(value, tuple):
            for _pattern, mapping in value:
                found.update({str(k): str(v) for k, v in mapping.items()})
    return sorted(found.items())


def team_aliases(publication: str) -> Dict[str, str]:
    with _engine().connect() as conn:
        rows = conn.execute(select(aliases).where(aliases.c.publication == _publication(publication))
                            .order_by(aliases.c.id))
        return {r.phrase: r.row for r in rows}


def list_team_aliases(publication: str) -> List[dict]:
    with _engine().connect() as conn:
        rows = conn.execute(select(aliases).where(aliases.c.publication == _publication(publication))
                            .order_by(aliases.c.id))
        return [_alias_dict(r) for r in rows]


def _alias_dict(r) -> dict:
    return {"id": r.id, "phrase": r.phrase, "row": r.row, "by": r.created_by,
            "at": r.created_at.isoformat() if r.created_at else None}


def add_alias(publication: str, phrase: str, row: str, by: str) -> dict:
    """Whole metric name only: the phrase is lower-cased and its spacing collapsed, as
    TableData.label_aliases compares it. Warns, never refuses, on an override of a built-in
    alias or a row no check of this publication has shown yet."""
    publication, by = _publication(publication), _editor(by)
    phrase, row = _norm(phrase).lower(), _norm(row)
    if not phrase or not row:
        raise SettingsError("Frasa di laporan dan baris tabel wajib diisi.")
    if len(phrase) > PHRASE_MAX_CHARS or len(row) > ROW_MAX_CHARS:
        raise SettingsError("Frasa atau nama baris terlalu panjang.")
    warnings = []
    builtin = dict(builtin_aliases(publication))
    if phrase in builtin and builtin[phrase] != row:
        warnings.append(f"Menggantikan padanan bawaan '{phrase}' → '{builtin[phrase]}'.")
    seen = known_rows(publication)
    if seen and row not in seen:
        warnings.append(f"Baris '{row}' belum pernah terlihat di tabel publikasi ini; periksa ejaannya.")
    with _engine().begin() as conn:
        conn.execute(delete(aliases).where((aliases.c.publication == publication) & (aliases.c.phrase == phrase)))
        new_id = conn.execute(insert(aliases).values(publication=publication, phrase=phrase, row=row,
                                                     created_by=by, created_at=_now())).inserted_primary_key[0]
        _log(conn, by, publication, "alias", "tambah", after=f"{phrase} → {row}")
        r = conn.execute(select(aliases).where(aliases.c.id == new_id)).first()
    return {**_alias_dict(r), "warnings": warnings}


def delete_alias(publication: str, alias_id: int, by: str) -> None:
    publication, by = _publication(publication), _editor(by)
    with _engine().begin() as conn:
        r = conn.execute(select(aliases).where((aliases.c.id == alias_id) & (aliases.c.publication == publication))).first()
        if r is None:
            raise SettingsError("Padanan itu tidak ditemukan; mungkin sudah dihapus orang lain.")
        conn.execute(delete(aliases).where(aliases.c.id == alias_id))
        _log(conn, by, publication, "alias", "hapus", before=f"{r.phrase} → {r.row}")


def record_known_rows(publication: str, rows: List[str]) -> None:
    """The row names a check parsed for this publication, for the alias form's suggestions."""
    if publication not in PUBLICATIONS:
        return
    rows = list(dict.fromkeys(_norm(r) for r in rows if _norm(r) and len(_norm(r)) <= ROW_MAX_CHARS))
    if not rows:
        return
    now = _now()
    with _engine().begin() as conn:
        present = {r.row for r in conn.execute(select(known_rows_table.c.row)
                                               .where(known_rows_table.c.publication == publication))}
        fresh = [r for r in rows if r not in present]
        if fresh and len(present) + len(fresh) <= KNOWN_ROWS_KEPT:
            conn.execute(insert(known_rows_table), [{"publication": publication, "row": r, "last_seen": now}
                                                    for r in fresh])
        conn.execute(update(known_rows_table).where((known_rows_table.c.publication == publication)
                                                    & known_rows_table.c.row.in_(rows)).values(last_seen=now))


def known_rows(publication: str) -> List[str]:
    with _engine().connect() as conn:
        return sorted(r.row for r in conn.execute(select(known_rows_table.c.row)
                                                  .where(known_rows_table.c.publication == publication)))


# --- notes ---------------------------------------------------------------------------------

def _seed_entries(publication: str) -> List[str]:
    path = notes_path(publication)
    if path is None or not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [str(e).strip() for section in _SEEDED_SECTIONS for e in (data.get(section) or []) if str(e).strip()]


def _seed(conn, publication: str) -> None:
    conn.execute(insert(notes_seeded).values(publication=publication, seeded_at=_now()))
    entries = _seed_entries(publication)
    if entries:
        conn.execute(insert(notes_table), [
            {"publication": publication, "position": i, "text": text, "seeded": True,
             "created_by": "catatan awal", "created_at": _now()}
            for i, text in enumerate(entries)])


def notes(publication: str) -> List[dict]:
    """The publication's notes in order; seeded from its YAML the first time they are read."""
    publication = _publication(publication)
    with _engine().begin() as conn:
        if conn.execute(select(notes_seeded).where(notes_seeded.c.publication == publication)).first() is None:
            _seed(conn, publication)
        rows = conn.execute(select(notes_table).where(notes_table.c.publication == publication)
                            .order_by(notes_table.c.position, notes_table.c.id))
        return [_note_dict(r) for r in rows]


def _note_dict(r) -> dict:
    return {"id": r.id, "text": r.text, "seeded": bool(r.seeded), "by": r.updated_by or r.created_by,
            "at": (r.updated_at or r.created_at).isoformat() if (r.updated_at or r.created_at) else None}


def _words(texts: List[str]) -> int:
    return sum(len(t.split()) for t in texts)


def _check_note(publication: str, text: str, replacing: Optional[int] = None) -> Tuple[str, List[str]]:
    text = (text or "").strip()
    if not text:
        raise SettingsError("Catatan tidak boleh kosong.")
    if len(text) > NOTE_MAX_CHARS:
        raise SettingsError("Satu catatan terlalu panjang; pecah menjadi beberapa catatan.")
    others = [n["text"] for n in notes(publication) if n["id"] != replacing]
    if _words(others + [text]) > NOTES_MAX_WORDS:
        raise SettingsError(f"Catatan publikasi ini melebihi {NOTES_MAX_WORDS} kata; ringkas atau hapus yang lama.")
    risky = _RISKY.search(text)
    warnings = ([f"Kata '{risky.group(0)}' pernah membuat klaim terlewat; pastikan catatan ini membantu "
                 "membaca kalimat, bukan menyuruh melewatinya."] if risky else [])
    return text, warnings


def add_note(publication: str, text: str, by: str) -> dict:
    publication, by = _publication(publication), _editor(by)
    text, warnings = _check_note(publication, text)
    with _engine().begin() as conn:
        last = conn.execute(select(func.max(notes_table.c.position))
                            .where(notes_table.c.publication == publication)).scalar()
        new_id = conn.execute(insert(notes_table).values(
            publication=publication, position=(last or 0) + 1 if last is not None else 0, text=text,
            seeded=False, created_by=by, created_at=_now())).inserted_primary_key[0]
        _log(conn, by, publication, "note", "tambah", after=text)
        r = conn.execute(select(notes_table).where(notes_table.c.id == new_id)).first()
    return {**_note_dict(r), "warnings": warnings}


def update_note(publication: str, note_id: int, text: str, by: str) -> dict:
    publication, by = _publication(publication), _editor(by)
    text, warnings = _check_note(publication, text, replacing=note_id)
    with _engine().begin() as conn:
        r = conn.execute(select(notes_table).where((notes_table.c.id == note_id)
                                                   & (notes_table.c.publication == publication))).first()
        if r is None:
            raise SettingsError("Catatan itu tidak ditemukan; mungkin sudah dihapus orang lain.")
        conn.execute(update(notes_table).where(notes_table.c.id == note_id)
                     .values(text=text, updated_by=by, updated_at=_now()))
        _log(conn, by, publication, "note", "ubah", before=r.text, after=text)
        r = conn.execute(select(notes_table).where(notes_table.c.id == note_id)).first()
    return {**_note_dict(r), "warnings": warnings}


def delete_note(publication: str, note_id: int, by: str) -> None:
    publication, by = _publication(publication), _editor(by)
    with _engine().begin() as conn:
        r = conn.execute(select(notes_table).where((notes_table.c.id == note_id)
                                                   & (notes_table.c.publication == publication))).first()
        if r is None:
            raise SettingsError("Catatan itu tidak ditemukan; mungkin sudah dihapus orang lain.")
        conn.execute(delete(notes_table).where(notes_table.c.id == note_id))
        _log(conn, by, publication, "note", "hapus", before=r.text)


def reorder_notes(publication: str, ids: List[int], by: str) -> None:
    publication, by = _publication(publication), _editor(by)
    current = [n["id"] for n in notes(publication)]
    if sorted(ids) != sorted(current):
        raise SettingsError("Urutan catatan sudah berubah; muat ulang halaman lalu coba lagi.")
    with _engine().begin() as conn:
        for position, note_id in enumerate(ids):
            conn.execute(update(notes_table).where(notes_table.c.id == note_id).values(position=position))
        _log(conn, by, publication, "note", "urutkan")


# --- reset, log, a check's view ------------------------------------------------------------

def reset(publication: str, by: str) -> None:
    """Back to the start: no team aliases, the seeded notes, visible."""
    publication, by = _publication(publication), _editor(by)
    with _engine().begin() as conn:
        conn.execute(delete(aliases).where(aliases.c.publication == publication))
        conn.execute(delete(notes_table).where(notes_table.c.publication == publication))
        conn.execute(delete(notes_seeded).where(notes_seeded.c.publication == publication))
        conn.execute(delete(publication_state).where(publication_state.c.publication == publication))
        _seed(conn, publication)
        _log(conn, by, publication, "reset", "ubah")


def changes(publication: Optional[str] = None, limit: int = CHANGES_SHOWN) -> List[dict]:
    query = select(change_log).order_by(change_log.c.id.desc()).limit(limit)
    if publication:
        query = query.where(change_log.c.publication == _publication(publication))
    with _engine().connect() as conn:
        return [{"id": r.id, "at": r.at.isoformat(), "by": r.by, "publication": r.publication,
                 "kind": r.kind, "action": r.action, "before": r.before, "after": r.after}
                for r in conn.execute(query)]


def for_check(publication: Optional[str]) -> Tuple[Dict[str, str], str]:
    """(team aliases, notes as '- line' text) for a check of this publication. Never raises: a
    settings failure is logged and the check runs on the built-ins."""
    if publication not in PUBLICATIONS:
        return {}, ""
    try:
        return team_aliases(publication), "\n".join(f"- {n['text']}" for n in notes(publication))
    except Exception:
        logger.warning("Pengaturan '%s' tidak terbaca; pemeriksaan memakai bawaan saja", publication,
                       exc_info=True)
        return {}, ""
