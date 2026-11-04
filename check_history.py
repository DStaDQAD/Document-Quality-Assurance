"""Shared, reopenable history of document checks.

Every successful paired check is stored once: a few summary columns for the history list, and
the whole response — gzipped — for reopening it exactly as it was shown. The host wipes its disk
on every deploy, so in production the rows live in an external Postgres (HISTORY_DATABASE_URL,
e.g. a free Neon project); left unset, they go to a local SQLite file, which is what development
and the test suite use.

This module knows nothing about the pipeline: main.py builds the CheckSummary and hands over the
response already dumped to JSON-safe types.
"""

import gzip
import json
import logging
import os
import threading
import uuid
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import (
    Column, DateTime, Float, Integer, LargeBinary, MetaData, String, Table, Text,
    and_, case, create_engine, false, func, inspect, not_, or_, select, text,
)
from sqlalchemy.engine import Engine

from schemas import CheckSummary, HistoryColumnFilter, HistoryQuery, TranslationCheckSummary

logger = logging.getLogger("fact-checker")

LOCAL_DB_PATH = Path(__file__).parent / "history.db"
# A History date filter means a day in Jakarta, where the team works.
WIB = timezone(timedelta(hours=7))
CHECKER_NAME_MAX_CHARS = 80

# The publication types the upload card offers (its "Jenis publikasi" radios use these values;
# the UI holds the labels). A check run under "Umum" has no publication.
PUBLICATIONS = (
    "uang-beredar", "uang-primer-m0", "npi", "pii", "cadangan-devisa", "sulni",
    "sk", "spe", "skdu", "pmi", "sbank", "shpr", "ppkom",
)

_metadata = MetaData()
checks = Table(
    "checks", _metadata,
    Column("id", String(36), primary_key=True),
    Column("created_at", DateTime(timezone=True), nullable=False, index=True),
    Column("filename", String(512), nullable=False),
    Column("file_kind", String(8), nullable=False),
    Column("checker_name", String(CHECKER_NAME_MAX_CHARS)),
    Column("publication", String(32)),
    Column("mode", String(16), nullable=False),
    Column("excel_files", Text, nullable=False),       # JSON list of names
    Column("reference_files", Text, nullable=False),   # JSON list of names
    Column("n_facts", Integer, nullable=False),
    Column("n_match", Integer, nullable=False),
    Column("n_mismatch", Integer, nullable=False),
    Column("n_unverified", Integer, nullable=False),
    Column("n_typos", Integer, nullable=False),
    Column("n_charts", Integer, nullable=False),
    Column("duration_s", Float, nullable=False),
    Column("tokens_in", Integer, nullable=False),
    Column("tokens_out", Integer, nullable=False),
    Column("result_gz", LargeBinary, nullable=False),  # gzipped PairedVerificationResponse JSON
)
# The list never needs the (large) result, so it never reads it.
_SUMMARY_COLUMNS = [c for c in checks.columns if c.name != "result_gz"]

# Cek Terjemahan (an English publication against its Indonesian version) keeps its own table:
# Riwayat lists the two kinds apart.
translation_checks = Table(
    "translation_checks", _metadata,
    Column("id", String(36), primary_key=True),
    Column("created_at", DateTime(timezone=True), nullable=False, index=True),
    Column("document", String(16), nullable=False),
    Column("id_filename", String(512), nullable=False),
    Column("en_filename", String(512), nullable=False),
    Column("checker_name", String(CHECKER_NAME_MAX_CHARS)),
    Column("publication", String(32)),
    Column("n_number", Integer, nullable=False),
    Column("n_meaning", Integer, nullable=False),
    Column("n_unchecked", Integer, nullable=False),
    Column("duration_s", Float, nullable=False),
    Column("result_gz", LargeBinary, nullable=False),  # gzipped TranslationCheckResponse JSON
)
_TRANSLATION_SUMMARY_COLUMNS = [c for c in translation_checks.columns if c.name != "result_gz"]

_engine: Optional[Engine] = None
_engine_lock = threading.Lock()


def database_url() -> str:
    """HISTORY_DATABASE_URL, or the local SQLite file when it is unset.

    Neon (like most hosts) hands out postgres:// or postgresql:// URLs, which SQLAlchemy maps to
    psycopg2 — a driver this app does not install. Those are pinned to psycopg (v3) here, so the
    connection string can be pasted exactly as the host shows it.
    """
    url = os.getenv("HISTORY_DATABASE_URL", "").strip()
    if not url:
        return f"sqlite:///{LOCAL_DB_PATH.as_posix()}"
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def _get_engine() -> Engine:
    """The engine, created (and its table with it) on first use.

    A failure here — the database unreachable — leaves nothing cached, so the next call tries
    again instead of the app being stuck without history until a restart.
    """
    global _engine
    with _engine_lock:
        if _engine is None:
            url = database_url()
            if url.startswith("sqlite"):
                # Saves run in a worker thread (asyncio.to_thread), not the thread that opened
                # the connection.
                connect_args: Dict[str, Any] = {"check_same_thread": False}
            else:
                # A sleeping Neon compute wakes in about a second; a dead one must not hold a
                # finished check back for long.
                connect_args = {"connect_timeout": 10}
            engine = create_engine(url, pool_pre_ping=True, connect_args=connect_args)
            _metadata.create_all(engine)
            _add_missing_columns(engine)
            _engine = engine
        return _engine


def _add_missing_columns(engine: Engine) -> None:
    """Bring a table made by an earlier release up to date; create_all never alters one.

    Only nullable columns are ever added after the first release, so old rows simply read None.
    """
    present = {c["name"] for c in inspect(engine).get_columns(checks.name)}
    with engine.begin() as conn:
        for column in checks.columns:
            if column.name not in present:
                ddl_type = column.type.compile(dialect=engine.dialect)
                conn.execute(text(f"ALTER TABLE {checks.name} ADD COLUMN {column.name} {ddl_type}"))


def reset_engine() -> None:
    """Drop the cached engine so the next call reads HISTORY_DATABASE_URL again (tests)."""
    global _engine
    with _engine_lock:
        if _engine is not None:
            _engine.dispose()
        _engine = None


def warn_if_ephemeral() -> None:
    """On Render without an external database, say once that history will not survive."""
    if os.getenv("RENDER") and not os.getenv("HISTORY_DATABASE_URL", "").strip():
        logger.warning(
            "HISTORY_DATABASE_URL is not set: check history is kept in a local SQLite file, "
            "which this host wipes on every deploy and restart."
        )


def normalize_checker_name(raw: Optional[str]) -> Optional[str]:
    """Collapse whitespace and cap the length; a blank name is no name."""
    name = " ".join((raw or "").split())[:CHECKER_NAME_MAX_CHARS]
    return name or None


def normalize_publication(raw: Optional[str]) -> Optional[str]:
    """A known publication key, or None for "Umum" and anything unrecognised."""
    key = (raw or "").strip().lower()
    return key if key in PUBLICATIONS else None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _gz(result: Dict[str, Any]) -> bytes:
    return gzip.compress(json.dumps(result, ensure_ascii=False).encode("utf-8"))


def _ungz(blob: bytes, check_id: str) -> Dict[str, Any]:
    result = json.loads(gzip.decompress(blob).decode("utf-8"))
    result["history_id"] = check_id
    return result


def _utc(created: datetime) -> datetime:
    # SQLite keeps no offset; every row was written in UTC (see _now).
    return created.replace(tzinfo=timezone.utc) if created.tzinfo is None else created


def save_check(summary: CheckSummary, result: Dict[str, Any]) -> str:
    """Store one finished check and return its new id."""
    check_id = str(uuid.uuid4())
    row = summary.model_dump(exclude={"id", "created_at"})
    row["checker_name"] = normalize_checker_name(row["checker_name"])
    row["publication"] = normalize_publication(row["publication"])
    row["excel_files"] = json.dumps(row["excel_files"], ensure_ascii=False)
    row["reference_files"] = json.dumps(row["reference_files"], ensure_ascii=False)
    with _get_engine().begin() as conn:
        conn.execute(checks.insert().values(id=check_id, created_at=_now(), result_gz=_gz(result), **row))
    return check_id


def _row_to_summary(row) -> CheckSummary:
    data = {k: v for k, v in row._mapping.items() if k != "result_gz"}
    data["created_at"] = _utc(data["created_at"])
    data["excel_files"] = json.loads(data["excel_files"])
    data["reference_files"] = json.loads(data["reference_files"])
    return CheckSummary(**data)


def _wib_day_start(day: date) -> datetime:
    # In UTC: SQLite keeps no offset and compares the stored UTC text as written.
    return datetime.combine(day, time.min, tzinfo=WIB).astimezone(timezone.utc)


def _filters(table: Table, text_columns: List[Column], q: str, publication: Optional[str],
             date_from: Optional[date], date_to: Optional[date]) -> list:
    """The WHERE conditions the History filters share. `q` matches any of `text_columns` or the
    checker's name, case-insensitively, as plain text: report names are full of underscores,
    which LIKE would otherwise read as a wildcard. The dates are whole WIB days, both included."""
    conditions = []
    needle = q.strip().lower()
    if needle:
        conditions.append(or_(
            *(func.lower(c).contains(needle, autoescape=True) for c in text_columns),
            func.lower(func.coalesce(table.c.checker_name, "")).contains(needle, autoescape=True),
        ))
    if publication:
        conditions.append(table.c.publication == publication)
    if date_from:
        conditions.append(table.c.created_at >= _wib_day_start(date_from))
    if date_to:
        conditions.append(table.c.created_at < _wib_day_start(date_to + timedelta(days=1)))
    return conditions


def _page(columns: list, table: Table, conditions: list, limit: int, offset: int,
          order_by: Optional[list] = None) -> Tuple[list, bool, int]:
    """The rows of one page (newest first unless `order_by` says otherwise), whether more
    follow, and how many match in all."""
    query = (select(*columns).where(*conditions)
             .order_by(*(order_by or [table.c.created_at.desc(), table.c.id.desc()])))
    with _get_engine().connect() as conn:
        rows = conn.execute(query.limit(limit + 1).offset(offset)).all()
        total = conn.execute(select(func.count()).select_from(table).where(*conditions)).scalar_one()
    return rows[:limit], len(rows) > limit, total


def list_checks(limit: int = 50, offset: int = 0, q: str = "", *, publication: Optional[str] = None,
                date_from: Optional[date] = None, date_to: Optional[date] = None,
                mismatch_only: bool = False) -> Tuple[List[CheckSummary], bool, int]:
    """A page of summaries, newest first, whether more follow, and how many match in all.

    `q` matches the file name or the checker's name; `publication` is a key of PUBLICATIONS;
    `mismatch_only` keeps the checks with at least one Tidak Sesuai.
    """
    conditions = _filters(checks, [checks.c.filename], q, publication, date_from, date_to)
    if mismatch_only:
        conditions.append(checks.c.n_mismatch > 0)
    rows, has_more, total = _page(_SUMMARY_COLUMNS, checks, conditions, limit, offset)
    return [_row_to_summary(r) for r in rows], has_more, total


def get_check(check_id: str) -> Optional[Tuple[CheckSummary, Dict[str, Any]]]:
    """One stored check — its summary and its full result, with history_id set — or None."""
    with _get_engine().connect() as conn:
        row = conn.execute(select(checks).where(checks.c.id == check_id)).first()
    if row is None:
        return None
    return _row_to_summary(row), _ungz(row.result_gz, check_id)


def save_translation_check(summary: TranslationCheckSummary, result: Dict[str, Any]) -> str:
    """Store one finished Cek Terjemahan and return its new id."""
    check_id = str(uuid.uuid4())
    row = summary.model_dump(exclude={"id", "created_at"})
    row["checker_name"] = normalize_checker_name(row["checker_name"])
    row["publication"] = normalize_publication(row["publication"])
    with _get_engine().begin() as conn:
        conn.execute(translation_checks.insert().values(id=check_id, created_at=_now(), result_gz=_gz(result), **row))
    return check_id


def _translation_summary(row) -> TranslationCheckSummary:
    data = {k: v for k, v in row._mapping.items() if k != "result_gz"}
    data["created_at"] = _utc(data["created_at"])
    return TranslationCheckSummary(**data)


def list_translation_checks(limit: int = 50, offset: int = 0, q: str = "", *,
                            publication: Optional[str] = None, date_from: Optional[date] = None,
                            date_to: Optional[date] = None) -> Tuple[List[TranslationCheckSummary], bool, int]:
    """A page of Cek Terjemahan summaries, as list_checks gives them; `q` matches either file
    name or the checker's name."""
    t = translation_checks
    conditions = _filters(t, [t.c.id_filename, t.c.en_filename], q, publication, date_from, date_to)
    rows, has_more, total = _page(_TRANSLATION_SUMMARY_COLUMNS, t, conditions, limit, offset)
    return [_translation_summary(r) for r in rows], has_more, total


# ── Column filters and sorting: the ▾ on each History column (POST /api/history/search and
# /api/history/facets, and their translation-history twins). Both lists share one vocabulary:
# Cek Terjemahan's "Dokumen" is its English file and its "Mode" column is the document kind.

HISTORY_KINDS = ("paired", "translation")


def _table_of(kind: str) -> Table:
    return checks if kind == "paired" else translation_checks


def _facet_columns(table: Table) -> Dict[str, Column]:
    if table is checks:
        return {"filename": checks.c.filename, "publication": checks.c.publication,
                "checker": checks.c.checker_name, "mode": checks.c.mode}
    t = translation_checks
    return {"filename": t.c.en_filename, "publication": t.c.publication,
            "checker": t.c.checker_name, "mode": t.c.document}


def _sort_key(table: Table, sort: str, publication_order: List[str]):
    if sort == "publication" and publication_order:
        rank = {key: i for i, key in enumerate(publication_order)}
        return case(rank, value=func.coalesce(table.c.publication, ""), else_=len(rank))
    if sort == "created_at":
        return table.c.created_at
    if sort == "duration":
        return table.c.duration_s
    if sort == "result":
        # Verifikasi: the Tidak Sesuai count; Cek Terjemahan: every finding.
        return checks.c.n_mismatch if table is checks else table.c.n_number + table.c.n_meaning
    return func.lower(func.coalesce(_facet_columns(table)[sort], ""))


def _day_condition(table: Table, days: List[str]):
    ranges = []
    for raw in days:
        try:
            day = date.fromisoformat(raw)
        except ValueError:
            continue
        ranges.append(and_(table.c.created_at >= _wib_day_start(day),
                           table.c.created_at < _wib_day_start(day + timedelta(days=1))))
    return or_(*ranges) if ranges else false()


def _column_condition(table: Table, column: str, flt: HistoryColumnFilter):
    if column == "day":
        match = _day_condition(table, flt.values)
    else:
        # A missing publication or checker is "", so it can be ticked like any other value, and
        # an excluded list never drops those rows by NULL comparison.
        match = func.coalesce(_facet_columns(table)[column], "").in_(flt.values)
    return not_(match) if flt.exclude else match


def _query_conditions(table: Table, query: HistoryQuery, skip_column: Optional[str] = None) -> list:
    text_columns = [checks.c.filename] if table is checks else [table.c.id_filename, table.c.en_filename]
    conditions = _filters(table, text_columns, query.q, None, query.date_from, query.date_to)
    if query.mismatch_only and table is checks:
        conditions.append(checks.c.n_mismatch > 0)
    for column, flt in query.columns.items():
        if column != skip_column:
            conditions.append(_column_condition(table, column, flt))
    return conditions


def search_history(kind: str, query: HistoryQuery) -> Tuple[list, bool, int]:
    """A page of summaries of one list ("paired" or "translation") narrowed by every filter of
    `query` and ordered by its column (ties newest first), whether more follow, and the total."""
    table = _table_of(kind)
    key = _sort_key(table, query.sort, query.publication_order)
    order = [key.desc() if query.descending else key.asc()]
    if query.sort != "created_at":
        order.append(table.c.created_at.desc())
    order.append(table.c.id.desc())
    if table is checks:
        rows, has_more, total = _page(_SUMMARY_COLUMNS, checks, _query_conditions(checks, query),
                                      query.limit, query.offset, order)
        return [_row_to_summary(r) for r in rows], has_more, total
    rows, has_more, total = _page(_TRANSLATION_SUMMARY_COLUMNS, table, _query_conditions(table, query),
                                  query.limit, query.offset, order)
    return [_translation_summary(r) for r in rows], has_more, total


def history_facet(kind: str, query: HistoryQuery, column: str) -> List[Tuple[str, int]]:
    """The values `column` takes among the checks every other filter keeps, with how many checks
    have each — what a column's ▾ lists, as Excel does. Days newest first, the rest A→Z."""
    table = _table_of(kind)
    conditions = _query_conditions(table, query, skip_column=column)
    with _get_engine().connect() as conn:
        if column == "day":
            stamps = conn.execute(select(table.c.created_at).where(*conditions)).scalars().all()
            counts = Counter(_utc(s).astimezone(WIB).date().isoformat() for s in stamps)
            return sorted(counts.items(), reverse=True)
        value = func.coalesce(_facet_columns(table)[column], "")
        rows = conn.execute(select(value, func.count()).select_from(table)
                            .where(*conditions).group_by(value)).all()
    return sorted(((v, n) for v, n in rows), key=lambda r: (r[0].lower(), r[0]))


def get_translation_check(check_id: str) -> Optional[Tuple[TranslationCheckSummary, Dict[str, Any]]]:
    """One stored Cek Terjemahan — its summary and its full result, with history_id set — or None."""
    with _get_engine().connect() as conn:
        row = conn.execute(select(translation_checks).where(translation_checks.c.id == check_id)).first()
    if row is None:
        return None
    return _translation_summary(row), _ungz(row.result_gz, check_id)
