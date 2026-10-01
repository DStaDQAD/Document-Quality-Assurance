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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import (
    Column, DateTime, Float, Integer, LargeBinary, MetaData, String, Table, Text,
    create_engine, func, or_, select,
)
from sqlalchemy.engine import Engine

from schemas import CheckSummary

logger = logging.getLogger("fact-checker")

LOCAL_DB_PATH = Path(__file__).parent / "history.db"
CHECKER_NAME_MAX_CHARS = 80

_metadata = MetaData()
checks = Table(
    "checks", _metadata,
    Column("id", String(36), primary_key=True),
    Column("created_at", DateTime(timezone=True), nullable=False, index=True),
    Column("filename", String(512), nullable=False),
    Column("file_kind", String(8), nullable=False),
    Column("checker_name", String(CHECKER_NAME_MAX_CHARS)),
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
            _engine = engine
        return _engine


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


def _now() -> datetime:
    return datetime.now(timezone.utc)


def save_check(summary: CheckSummary, result: Dict[str, Any]) -> str:
    """Store one finished check and return its new id."""
    check_id = str(uuid.uuid4())
    row = summary.model_dump(exclude={"id", "created_at"})
    row["checker_name"] = normalize_checker_name(row["checker_name"])
    row["excel_files"] = json.dumps(row["excel_files"], ensure_ascii=False)
    row["reference_files"] = json.dumps(row["reference_files"], ensure_ascii=False)
    blob = gzip.compress(json.dumps(result, ensure_ascii=False).encode("utf-8"))
    with _get_engine().begin() as conn:
        conn.execute(checks.insert().values(id=check_id, created_at=_now(), result_gz=blob, **row))
    return check_id


def _row_to_summary(row) -> CheckSummary:
    data = {k: v for k, v in row._mapping.items() if k != "result_gz"}
    created = data["created_at"]
    if created.tzinfo is None:
        # SQLite keeps no offset; every row was written in UTC (see _now).
        data["created_at"] = created.replace(tzinfo=timezone.utc)
    data["excel_files"] = json.loads(data["excel_files"])
    data["reference_files"] = json.loads(data["reference_files"])
    return CheckSummary(**data)


def list_checks(limit: int = 50, offset: int = 0, q: str = "") -> Tuple[List[CheckSummary], bool]:
    """A page of summaries, newest first, and whether more follow.

    `q` matches the file name or the checker's name, case-insensitively, as plain text: report
    names are full of underscores, which LIKE would otherwise read as a wildcard.
    """
    query = select(*_SUMMARY_COLUMNS).order_by(checks.c.created_at.desc(), checks.c.id.desc())
    needle = q.strip().lower()
    if needle:
        query = query.where(or_(
            func.lower(checks.c.filename).contains(needle, autoescape=True),
            func.lower(func.coalesce(checks.c.checker_name, "")).contains(needle, autoescape=True),
        ))
    with _get_engine().connect() as conn:
        rows = conn.execute(query.limit(limit + 1).offset(offset)).all()
    return [_row_to_summary(r) for r in rows[:limit]], len(rows) > limit


def get_check(check_id: str) -> Optional[Tuple[CheckSummary, Dict[str, Any]]]:
    """One stored check — its summary and its full result, with history_id set — or None."""
    with _get_engine().connect() as conn:
        row = conn.execute(select(checks).where(checks.c.id == check_id)).first()
    if row is None:
        return None
    result = json.loads(gzip.decompress(row.result_gz).decode("utf-8"))
    result["history_id"] = check_id
    return _row_to_summary(row), result
