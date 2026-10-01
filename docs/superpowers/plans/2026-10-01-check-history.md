# Riwayat Pengecekan (Check History) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Store every successful paired check in a shared history that anyone signed in can list, search, and reopen, with the result rendered exactly as it was.

**Architecture:** A new self-contained module `check_history.py` (SQLAlchemy Core, one `checks` table: summary columns + the gzipped response JSON) talks to Neon Postgres via `HISTORY_DATABASE_URL`, or to a local SQLite `history.db` when unset. `_run_paired_pipeline` in `main.py` saves each finished check through it (a failure only logs a warning and returns `history_id: null`), two new `GET /api/history` endpoints read it back, and `static/index.html` gets a "Riwayat" panel that re-renders a stored result with the existing `renderPairedResponse`.

**Tech Stack:** Python 3.13 (`.venv`), FastAPI, SQLAlchemy 2.0 Core (already installed via langchain), `psycopg[binary]` (new), pytest, vanilla JS in `static/index.html`, headless Chrome over CDP from Node 22 for the UI check.

**Spec:** `docs/superpowers/specs/2026-10-01-check-history-design.md`

## Global Constraints

- Cost $0: storage is Neon free tier; nothing else external.
- History must never fail a check: any save error → `logger.warning`, response still returned, `history_id: null`.
- Only successful checks are saved; failed (exception) and cancelled runs are not.
- Original uploaded files (PDF/Word/Excel) are never stored — only the response and summary.
- `checker_name`: optional, whitespace-collapsed, max **80** characters, blank → `NULL`.
- `GET /api/history`: newest first, `limit` default 50, clamped to **1..100**; `q` searches `filename` and `checker_name`, case-insensitive.
- Times stored in UTC, shown in the UI as WIB (`Asia/Jakarta`).
- Tests never touch Neon: an autouse fixture points `HISTORY_DATABASE_URL` at a per-test SQLite file.
- `history.db` is gitignored and dockerignored; separate from `DATABASE_PATH` / `statistik_makro.db`.
- UI copy is Indonesian, matching the existing shell.
- Commit messages: one imperative sentence in the repo's style (see `git log`), **no** `Co-Authored-By` trailer.
- Always run Python through `.venv/Scripts/python` (the global `python` lacks deps).
- Edit regex literals with the Edit tool, never through Bash heredocs/sed (backslashes get mangled).

## Review Focus

1. **SQLite hands back naive datetimes** — without normalising, the UI would parse `created_at` as local time and show WIB 7 hours off on the dev box; every summary must carry a UTC offset. (Test: Task 1 `test_created_at_comes_back_as_utc`.)
2. **Search text containing `_` or `%`** — report names like `M2_Juli.pdf` are common; a naive `LIKE` would treat `_` as a wildcard and match `M2-Juli.pdf` too. (Test: Task 1 `test_search_treats_like_wildcards_literally`.)
3. **Neon connection string pasted as-is (`postgres://…` / `postgresql://…`)** — SQLAlchemy would pick psycopg2, which is not installed, and every save would fail silently. (Test: Task 1 `test_database_url_pins_postgres_to_psycopg`.)
4. **Opening several history entries in one page** — `attachPairedHandlers` adds a click listener to the element it is given; reusing one element would stack them and a chart thumbnail would zoom and un-zoom on one click. (Check: Task 5 UI script, "thumbnail still zooms after a second open".)
5. **Opening a shared `#riwayat/<id>` link while signed out** — the fetch would 401 behind the login overlay; the entry must open once the user signs in. (Covered by calling `openFromHash()` from the auth gate's `unlock()`; Task 5 UI script checks the link opens on a fresh load.)

---

### Task 1: `check_history` storage module

**Files:**
- Create: `check_history.py`
- Modify: `schemas.py` (append two models at the end of the file)
- Modify: `requirements.txt`, `.gitignore`, `.env.example`, `tests/conftest.py`
- Test: `tests/test_check_history.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `schemas.CheckSummary` (pydantic): `id: Optional[str]`, `created_at: Optional[datetime]`, `filename: str`, `file_kind: Literal["pdf","docx"]`, `checker_name: Optional[str]`, `mode: str`, `excel_files: List[str]`, `reference_files: List[str]`, `n_facts, n_match, n_mismatch, n_unverified, n_typos, n_charts: int`, `duration_s: float`, `tokens_in, tokens_out: int`.
  - `schemas.CheckHistoryListResponse`: `items: List[CheckSummary]`, `has_more: bool`.
  - `check_history.save_check(summary: CheckSummary, result: dict) -> str` (returns the new id).
  - `check_history.list_checks(limit: int = 50, offset: int = 0, q: str = "") -> Tuple[List[CheckSummary], bool]` (items, has_more).
  - `check_history.get_check(check_id: str) -> Optional[Tuple[CheckSummary, dict]]` (the dict has `history_id` set).
  - `check_history.warn_if_ephemeral() -> None`, `check_history.reset_engine() -> None`, `check_history.database_url() -> str`, `check_history.normalize_checker_name(raw) -> Optional[str]`.

- [ ] **Step 1: Install the Postgres driver and pin it**

Run: `.venv/Scripts/python -m pip install "psycopg[binary]>=3.2"`
Expected: `Successfully installed psycopg-3.x psycopg-binary-3.x`

Add to `requirements.txt` directly after the `PyYAML` line:

```
psycopg[binary]>=3.2  # check history in Postgres (Neon); SQLAlchemy comes via langchain-community
```

Add to `.gitignore` directly after the `statistik_makro.db` line:

```
history.db
```

(`.dockerignore` already excludes `*.db`.)

Append to `.env.example`:

```

# Check history (Riwayat). Every successful check is stored so it can be reopened later. The
# host's disk is wiped on every deploy, so in production point this at an external Postgres,
# e.g. a free Neon project (region Singapore): paste its connection string as-is, it already
# carries sslmode=require. Left blank, history goes to a local SQLite file (history.db) - fine
# for development, but on Render it would vanish on the next deploy or restart.
HISTORY_DATABASE_URL=
```

- [ ] **Step 2: Add the isolation fixture to `tests/conftest.py`**

Append:

```python
@pytest.fixture(autouse=True)
def _isolated_history_db(tmp_path, monkeypatch):
    """Every test gets its own empty SQLite check history, so no test ever writes to the real
    history (a local history.db, or Neon when .env sets HISTORY_DATABASE_URL)."""
    import check_history
    monkeypatch.setenv("HISTORY_DATABASE_URL", f"sqlite:///{(tmp_path / 'history.db').as_posix()}")
    check_history.reset_engine()
    yield
    check_history.reset_engine()
```

- [ ] **Step 3: Write the failing tests**

Create `tests/test_check_history.py`:

```python
import gzip
import json
import logging
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

import check_history
from schemas import CheckSummary

PNG_1PX = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
           "YAAAAAYAAjCB0C8AAAAASUVORK5CYII=")


def _summary(**overrides):
    base = dict(filename="laporan.pdf", file_kind="pdf", mode="excel")
    base.update(overrides)
    return CheckSummary(**base)


def _save(filename="laporan.pdf", result=None, **overrides):
    return check_history.save_check(_summary(filename=filename, **overrides),
                                     result or {"pdf_filename": filename})


@pytest.fixture
def clock(monkeypatch):
    """Each save one minute after the previous one, so 'newest first' never depends on how
    finely the OS clock ticks."""
    start = datetime(2026, 9, 28, 7, 0, tzinfo=timezone.utc)
    ticks = iter(start + timedelta(minutes=i) for i in range(1000))
    monkeypatch.setattr(check_history, "_now", lambda: next(ticks))


def test_saved_result_comes_back_identical_with_its_id():
    result = {
        "pdf_filename": "laporan.pdf",
        "total_facts": 2,
        "chart_checks": [{"caption": "Grafik 1", "thumbnail": PNG_1PX}],
        "note": "Rp1.234,5 triliun — naik",
    }
    check_id = check_history.save_check(_summary(n_facts=2, checker_name="Ivan"), result)

    summary, loaded = check_history.get_check(check_id)

    assert loaded == {**result, "history_id": check_id}
    assert summary.id == check_id
    assert summary.n_facts == 2
    assert summary.checker_name == "Ivan"


def test_result_is_stored_gzipped():
    result = {"pdf_filename": "laporan.pdf", "results": ["x"] * 50}
    _save(result=result)

    with check_history._get_engine().connect() as conn:
        blob = conn.execute(select(check_history.checks.c.result_gz)).scalar_one()

    assert json.loads(gzip.decompress(blob)) == result


def test_list_is_newest_first_and_carries_no_result(clock):
    first = _save("a.pdf")
    second = _save("b.pdf")

    items, has_more = check_history.list_checks()

    assert [i.id for i in items] == [second, first]
    assert has_more is False
    assert "result_gz" not in items[0].model_dump()


def test_created_at_comes_back_as_utc(clock):
    _save()

    items, _ = check_history.list_checks()

    assert items[0].created_at == datetime(2026, 9, 28, 7, 0, tzinfo=timezone.utc)
    assert items[0].created_at.utcoffset() == timedelta(0)


def test_lists_round_trip(clock):
    _save(excel_files=["TABEL1_1.xls", "TABEL2_1.xls"], reference_files=["M2_Juli.pdf"])

    items, _ = check_history.list_checks()

    assert items[0].excel_files == ["TABEL1_1.xls", "TABEL2_1.xls"]
    assert items[0].reference_files == ["M2_Juli.pdf"]


def test_search_matches_filename_and_checker_case_insensitively(clock):
    _save("SK-Agustus-2026.pdf", checker_name="Ivan")
    _save("M2-Juli.pdf", checker_name="Budi")
    _save("lain.pdf")

    assert [i.filename for i in check_history.list_checks(q="agustus")[0]] == ["SK-Agustus-2026.pdf"]
    assert [i.filename for i in check_history.list_checks(q="BUDI")[0]] == ["M2-Juli.pdf"]


def test_search_treats_like_wildcards_literally(clock):
    _save("M2_Juli.pdf")
    _save("M2-Juli.pdf")

    assert [i.filename for i in check_history.list_checks(q="m2_")[0]] == ["M2_Juli.pdf"]
    assert check_history.list_checks(q="%")[0] == []


def test_limit_offset_and_has_more(clock):
    ids = [_save(f"r{i}.pdf") for i in range(5)]

    page1, more1 = check_history.list_checks(limit=2)
    page3, more3 = check_history.list_checks(limit=2, offset=4)

    assert [p.id for p in page1] == [ids[4], ids[3]]
    assert more1 is True
    assert [p.id for p in page3] == [ids[0]]
    assert more3 is False


def test_unknown_id_is_none():
    assert check_history.get_check("does-not-exist") is None


def test_checker_name_is_collapsed_capped_and_blank_becomes_none(clock):
    _save("a.pdf", checker_name="  Ivan   Jehuda ")
    _save("b.pdf", checker_name="x" * 200)
    _save("c.pdf", checker_name="   ")

    by_file = {i.filename: i.checker_name for i in check_history.list_checks()[0]}

    assert by_file["a.pdf"] == "Ivan Jehuda"
    assert by_file["b.pdf"] == "x" * 80
    assert by_file["c.pdf"] is None


@pytest.mark.parametrize("raw, expected", [
    ("postgres://u:p@h/db?sslmode=require", "postgresql+psycopg://u:p@h/db?sslmode=require"),
    ("postgresql://u:p@h/db", "postgresql+psycopg://u:p@h/db"),
    ("postgresql+psycopg://u:p@h/db", "postgresql+psycopg://u:p@h/db"),
    ("sqlite:///x.db", "sqlite:///x.db"),
])
def test_database_url_pins_postgres_to_psycopg(monkeypatch, raw, expected):
    monkeypatch.setenv("HISTORY_DATABASE_URL", f"  {raw} ")
    assert check_history.database_url() == expected


def test_database_url_defaults_to_local_sqlite(monkeypatch):
    monkeypatch.delenv("HISTORY_DATABASE_URL", raising=False)
    url = check_history.database_url()
    assert url.startswith("sqlite:///")
    assert url.endswith("/history.db")


def test_warns_on_render_without_an_external_database(monkeypatch, caplog):
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.delenv("HISTORY_DATABASE_URL", raising=False)
    with caplog.at_level(logging.WARNING, logger="fact-checker"):
        check_history.warn_if_ephemeral()
    assert "HISTORY_DATABASE_URL" in caplog.text


def test_no_warning_when_configured_or_off_render(monkeypatch, caplog):
    monkeypatch.setenv("RENDER", "true")
    with caplog.at_level(logging.WARNING, logger="fact-checker"):
        check_history.warn_if_ephemeral()          # conftest set HISTORY_DATABASE_URL
        monkeypatch.delenv("RENDER")
        monkeypatch.delenv("HISTORY_DATABASE_URL")
        check_history.warn_if_ephemeral()          # local development
    assert caplog.text == ""
```

- [ ] **Step 4: Run the tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_check_history.py -q`
Expected: collection error — `ModuleNotFoundError: No module named 'check_history'` (or `ImportError: cannot import name 'CheckSummary'`).

- [ ] **Step 5: Add the schema models**

Append to `schemas.py` (add `from datetime import datetime` to its imports if it is not there):

```python
class CheckSummary(BaseModel):
    """One stored check as the history list shows it — everything but the full result."""
    id: Optional[str] = None
    created_at: Optional[datetime] = None   # UTC; the UI shows it in WIB
    filename: str
    file_kind: Literal["pdf", "docx"]
    checker_name: Optional[str] = None
    mode: str
    excel_files: List[str] = Field(default_factory=list)
    reference_files: List[str] = Field(default_factory=list)
    n_facts: int = 0
    n_match: int = 0
    n_mismatch: int = 0
    n_unverified: int = 0
    n_typos: int = 0
    n_charts: int = 0
    duration_s: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0


class CheckHistoryListResponse(BaseModel):
    items: List[CheckSummary]
    has_more: bool
```

- [ ] **Step 6: Write `check_history.py`**

```python
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
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_check_history.py -q`
Expected: all pass (17 tests).

- [ ] **Step 8: Run the full suite (the new autouse fixture touches every test)**

Run: `.venv/Scripts/python -m pytest -q`
Expected: everything passes, same count as before plus 17.

- [ ] **Step 9: Commit**

```bash
git add check_history.py schemas.py requirements.txt .gitignore .env.example tests/conftest.py tests/test_check_history.py
git commit -m "Store finished checks in a shared history table, Postgres or local SQLite"
```

---

### Task 2: Save every finished check from the pipeline

**Files:**
- Modify: `perf_log.py` (extract `usage_totals`)
- Modify: `schemas.py` (`PairedVerificationResponse.history_id`)
- Modify: `main.py` (imports ~line 77-107; `_run_paired_pipeline` signature ~line 532 and its final `return` ~line 773; both paired endpoints ~line 824 and ~874; module-level warning after `logger = ...` ~line 110)
- Test: `tests/test_perf_log.py`, create `tests/test_main_check_history.py`

**Interfaces:**
- Consumes: `check_history.save_check`, `check_history.list_checks`, `check_history.get_check`, `check_history.warn_if_ephemeral`, `schemas.CheckSummary` (Task 1).
- Produces:
  - `perf_log.usage_totals(usage_metadata) -> Tuple[int, int]`.
  - `PairedVerificationResponse.history_id: Optional[str] = None`.
  - Query param `checker_name: str = ""` on `POST /api/verify-paired` and `POST /api/verify-paired-stream`.
  - `main._run_paired_pipeline(..., checker_name: str = "")`.

- [ ] **Step 1: Write the failing `usage_totals` test**

Append to `tests/test_perf_log.py` (and add `usage_totals` to its `from perf_log import ...` line):

```python
def test_usage_totals_sums_every_model_and_is_zero_when_none_reported():
    usage = {
        "gemini-2.5-flash": {"input_tokens": 1000, "output_tokens": 200},
        "openai/gpt-oss-120b": {"input_tokens": 50, "output_tokens": None},
    }
    assert usage_totals(usage) == (1050, 200)
    assert usage_totals(None) == (0, 0)
    assert usage_totals({}) == (0, 0)
```

- [ ] **Step 2: Write the failing pipeline tests**

Create `tests/test_main_check_history.py`:

```python
"""The paired endpoints store every finished check in the history, and never fail over it."""

import json
from unittest.mock import patch

from fastapi.testclient import TestClient

import check_history
import main
from schemas import PairedVerificationResponse

client = TestClient(main.app)

_PDF = ("pdf_file", ("report.pdf", b"%PDF-1.4 fake", "application/pdf"))
_XLS = ("excel_file", ("TABEL1_1.xls", b"xls-bytes", "application/vnd.ms-excel"))


def _fact_response(**overrides):
    base = dict(
        pdf_filename="report.pdf",
        excel_filenames=["TABEL1_1.xls"],
        excel_sheets=["I.1"],
        excel_units=["triliun Rp"],
        total_facts=3,
        entailed_count=2,
        refuted_count=1,
        inconclusive_count=0,
        results=[],
    )
    base.update(overrides)
    return PairedVerificationResponse(**base)


def _mock_pipeline(mock_get_vision_llm, mock_extract, mock_verify):
    mock_get_vision_llm.side_effect = RuntimeError("no key configured")
    mock_extract.return_value = "[== Halaman 1 ==]\nInflasi tumbuh 9,7% (yoy)."
    mock_verify.return_value = _fact_response()


@patch("main.verify_paired")
@patch("main.extract_narrative_text")
@patch("main.get_vision_llm")
def test_verify_paired_saves_the_check_and_returns_its_id(mock_vision, mock_extract, mock_verify):
    _mock_pipeline(mock_vision, mock_extract, mock_verify)

    response = client.post(
        "/api/verify-paired",
        params={"run_typo_check": "false", "checker_name": "  Ivan  "},
        files=[_PDF, _XLS],
    )

    assert response.status_code == 200
    history_id = response.json()["history_id"]
    assert history_id
    items, _ = check_history.list_checks()
    assert len(items) == 1
    saved = items[0]
    assert saved.id == history_id
    assert saved.filename == "report.pdf"
    assert saved.file_kind == "pdf"
    assert saved.checker_name == "Ivan"
    assert saved.mode == "excel"
    assert saved.excel_files == ["TABEL1_1.xls"]
    assert (saved.n_facts, saved.n_match, saved.n_mismatch, saved.n_unverified) == (3, 2, 1, 0)
    assert saved.duration_s >= 0
    _, result = check_history.get_check(history_id)
    assert result["total_facts"] == 3
    assert result["history_id"] == history_id


@patch("main.verify_paired")
@patch("main.extract_narrative_text")
@patch("main.get_vision_llm")
def test_stream_saves_the_check_too(mock_vision, mock_extract, mock_verify):
    _mock_pipeline(mock_vision, mock_extract, mock_verify)

    response = client.post(
        "/api/verify-paired-stream", params={"run_typo_check": "false"}, files=[_PDF, _XLS]
    )

    lines = [json.loads(line) for line in response.text.splitlines() if line.strip()]
    result = next(line["data"] for line in lines if line["type"] == "result")
    items, _ = check_history.list_checks()
    assert [i.id for i in items] == [result["history_id"]]
    assert items[0].checker_name is None


@patch("main.verify_paired")
@patch("main.extract_narrative_text")
@patch("main.get_vision_llm")
def test_one_excel_file_used_for_two_sheets_is_listed_once(mock_vision, mock_extract, mock_verify):
    _mock_pipeline(mock_vision, mock_extract, mock_verify)

    client.post(
        "/api/verify-paired",
        params={"run_typo_check": "false", "sheet_names": "I.1,II.1"},
        files=[_PDF, _XLS, _XLS],
    )

    assert check_history.list_checks()[0][0].excel_files == ["TABEL1_1.xls"]


@patch("main.check_history.save_check", side_effect=RuntimeError("database down"))
@patch("main.verify_paired")
@patch("main.extract_narrative_text")
@patch("main.get_vision_llm")
def test_a_failed_save_still_returns_the_result(mock_vision, mock_extract, mock_verify, _save, caplog):
    _mock_pipeline(mock_vision, mock_extract, mock_verify)

    response = client.post(
        "/api/verify-paired", params={"run_typo_check": "false"}, files=[_PDF, _XLS]
    )

    assert response.status_code == 200
    assert response.json()["total_facts"] == 3
    assert response.json()["history_id"] is None
    assert "History save failed" in caplog.text


@patch("main.verify_paired")
@patch("main.extract_narrative_text")
@patch("main.get_vision_llm")
def test_a_failed_check_is_not_saved(mock_vision, mock_extract, mock_verify):
    _mock_pipeline(mock_vision, mock_extract, mock_verify)
    mock_verify.side_effect = Exception("boom")

    response = client.post(
        "/api/verify-paired", params={"run_typo_check": "false"}, files=[_PDF, _XLS]
    )

    assert response.status_code == 400
    assert check_history.list_checks() == ([], False)
```

- [ ] **Step 3: Run them to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_perf_log.py tests/test_main_check_history.py -q`
Expected: FAIL — `ImportError: cannot import name 'usage_totals'`; and in the endpoint tests `KeyError: 'history_id'` / `AttributeError: <module 'main'> does not have the attribute 'check_history'`.

- [ ] **Step 4: Extract `usage_totals` in `perf_log.py`**

Change the typing import to `from typing import Dict, Mapping, Optional, Tuple` and replace `format_usage` with:

```python
def usage_totals(usage_metadata: Optional[Mapping[str, Mapping]]) -> Tuple[int, int]:
    """Input and output tokens summed across every model a run touched; (0, 0) when none were
    reported (providers that don't report usage, e.g. Ollama, and every mocked test run).

    The handler keys its totals by model name and a run may touch two models (a vision/primary
    plus a text fallback), so the counts are summed across them — the per-model split is not
    what we are tuning against.
    """
    if not usage_metadata:
        return 0, 0
    total_in = sum(int(u.get("input_tokens", 0) or 0) for u in usage_metadata.values())
    total_out = sum(int(u.get("output_tokens", 0) or 0) for u in usage_metadata.values())
    return total_in, total_out


def format_usage(usage_metadata: Optional[Mapping[str, Mapping]]) -> str:
    """Render a UsageMetadataCallbackHandler's per-model totals as ``in=… out=…``.

    Token counts are what make a prompt-size change provable, so they ride the same log
    line as the durations. Returns "" when nothing was recorded, so the log line simply
    omits the section.
    """
    total_in, total_out = usage_totals(usage_metadata)
    if not total_in and not total_out:
        return ""
    return f"in={total_in:,} out={total_out:,}".replace(",", ".")
```

- [ ] **Step 5: Add `history_id` to the response schema**

In `schemas.py`, in `PairedVerificationResponse`, after the `chart_pages_unread` field:

```python
    # The id this check was stored under in the history (Riwayat), or None when storing it
    # failed — the check itself still succeeded. See check_history.
    history_id: Optional[str] = None
```

- [ ] **Step 6: Wire the save into `main.py`**

Imports: add `import check_history` next to the other first-party imports (alphabetically, before `from db import ...`), change the perf import to `from perf_log import StageTimer, log_perf, usage_totals`, and add `CheckSummary,` to the `from schemas import (...)` block.

Directly after `logger = logging.getLogger("fact-checker")` (~line 110):

```python
check_history.warn_if_ephemeral()
```

Add the parameter to `_run_paired_pipeline` (after `check_charts: bool = False,`):

```python
    checker_name: str = "",
```

and append to its docstring:

```
    `checker_name` is stored with the check in the history (see _record_in_history).
```

Replace the function's final statement — `return fact_result.model_copy(update={ ... })` — with:

```python
    response = fact_result.model_copy(update={
        "typo_check": typo_result,
        "number_format_notice": notice,
        "coverage_gaps": coverage_gaps,
        "unread_tables": (word.unread_tables
                          if word is not None and mode in ("internal", "both") else []),
    })
    return await _record_in_history(
        response,
        pdf_filename=pdf_filename,
        file_kind="docx" if word is not None else "pdf",
        mode=mode,
        checker_name=checker_name,
        excel_sources=excel_sources,
        reference_pdfs=reference_pdfs or [],
        timer=timer,
        usage_metadata=usage_handler.usage_metadata,
    )
```

Add this function directly above `_run_paired_pipeline`:

```python
async def _record_in_history(
    response: PairedVerificationResponse,
    *,
    pdf_filename: str,
    file_kind: str,
    mode: str,
    checker_name: str,
    excel_sources: List[Tuple[bytes, str, str]],
    reference_pdfs: List[Tuple[bytes, str]],
    timer: StageTimer,
    usage_metadata: Optional[Dict[str, Any]],
) -> PairedVerificationResponse:
    """Store a finished check in the shared history and return the response with its id.

    The check already cost minutes and tokens, so the history never fails it: any error is
    logged and the response goes out with history_id None, which the UI reports as "not saved".
    """
    tokens_in, tokens_out = usage_totals(usage_metadata)
    summary = CheckSummary(
        filename=pdf_filename,
        file_kind=file_kind,
        checker_name=checker_name,
        mode=mode,
        # One workbook is uploaded once per selected sheet; list each file once.
        excel_files=list(dict.fromkeys(name for _, _, name in excel_sources)),
        reference_files=[name for _, name in reference_pdfs],
        n_facts=response.total_facts,
        n_match=response.entailed_count,
        n_mismatch=response.refuted_count,
        n_unverified=response.inconclusive_count,
        n_typos=response.typo_check.total_issues if response.typo_check else 0,
        n_charts=len(response.chart_checks),
        duration_s=timer.total(),
        tokens_in=tokens_in,
        tokens_out=tokens_out,
    )
    try:
        check_id = await asyncio.to_thread(
            check_history.save_check, summary, response.model_dump(mode="json")
        )
    except Exception:
        logger.warning("History save failed for %s", pdf_filename, exc_info=True)
        return response
    return response.model_copy(update={"history_id": check_id})
```

In **both** `verify_paired_endpoint` and `verify_paired_stream_endpoint`, add after `check_charts: bool = False,`:

```python
    checker_name: str = "",
```

add to each docstring (after the `check_charts:` paragraph):

```
    checker_name: optional; stored with the check in the history (GET /api/history).
```

and pass it on in each `_run_paired_pipeline(...)` call: `checker_name=checker_name,`.

- [ ] **Step 7: Run the tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_perf_log.py tests/test_main_check_history.py -q`
Expected: all pass.

- [ ] **Step 8: Run the full suite**

Run: `.venv/Scripts/python -m pytest -q`
Expected: all pass. (Existing paired tests now also save into their per-test SQLite; that is expected.)

- [ ] **Step 9: Commit**

```bash
git add perf_log.py schemas.py main.py tests/test_perf_log.py tests/test_main_check_history.py
git commit -m "Save every finished check to the history, and never fail a check over it"
```

---

### Task 3: Read endpoints `GET /api/history` and `GET /api/history/{id}`

**Files:**
- Modify: `main.py` (new endpoints directly after `verify_paired_stream_endpoint`, before `list_tables_endpoint`; add `CheckHistoryListResponse,` to the `from schemas import (...)` block)
- Modify: `README.md` (API table)
- Test: `tests/test_main_check_history.py`

**Interfaces:**
- Consumes: `check_history.list_checks`, `check_history.get_check`, `schemas.CheckHistoryListResponse` (Task 1).
- Produces:
  - `GET /api/history?limit=&offset=&q=` → `{"items": [CheckSummary...], "has_more": bool}`; 503 when the database is unreachable.
  - `GET /api/history/{check_id}` → `{"summary": CheckSummary, "result": {...PairedVerificationResponse JSON, history_id set}}`; 404 unknown id; 503 unreachable.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_main_check_history.py` (add `from schemas import CheckSummary` to the imports, and `import pytest`):

```python
def _seed(filename="laporan.pdf", **overrides):
    summary = CheckSummary(filename=filename, file_kind="pdf", mode="internal", **overrides)
    return check_history.save_check(summary, {"pdf_filename": filename, "total_facts": 1})


def test_history_list_returns_summaries_without_results():
    check_id = _seed("SK-Agustus-2026.pdf", checker_name="Ivan", n_match=4)

    body = client.get("/api/history").json()

    assert body["has_more"] is False
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["id"] == check_id
    assert item["filename"] == "SK-Agustus-2026.pdf"
    assert item["checker_name"] == "Ivan"
    assert item["n_match"] == 4
    assert item["created_at"].endswith(("Z", "+00:00"))
    assert "result" not in item and "result_gz" not in item


def test_history_list_searches_and_pages():
    for i in range(3):
        _seed(f"M2_Juli_{i}.pdf")
    _seed("lain.pdf")

    page = client.get("/api/history", params={"q": "m2_juli", "limit": 2}).json()
    rest = client.get("/api/history", params={"q": "m2_juli", "limit": 2, "offset": 2}).json()

    assert len(page["items"]) == 2 and page["has_more"] is True
    assert len(rest["items"]) == 1 and rest["has_more"] is False


@pytest.mark.parametrize("asked, used", [(0, 1), (-5, 1), (1000, 100), (20, 20)])
def test_history_list_clamps_limit(asked, used):
    with patch("main.check_history.list_checks", return_value=([], False)) as mock_list:
        client.get("/api/history", params={"limit": asked, "offset": -3})
    assert mock_list.call_args.args[:2] == (used, 0)


def test_history_detail_returns_summary_and_result():
    check_id = _seed("SK-Agustus-2026.pdf", checker_name="Ivan")

    body = client.get(f"/api/history/{check_id}").json()

    assert body["summary"]["id"] == check_id
    assert body["summary"]["checker_name"] == "Ivan"
    assert body["result"] == {
        "pdf_filename": "SK-Agustus-2026.pdf", "total_facts": 1, "history_id": check_id,
    }


def test_history_detail_unknown_id_is_404():
    response = client.get("/api/history/not-a-real-id")
    assert response.status_code == 404
    assert "riwayat" in response.json()["detail"].lower()


def test_history_endpoints_answer_503_when_the_database_is_down():
    with patch("main.check_history.list_checks", side_effect=RuntimeError("down")):
        assert client.get("/api/history").status_code == 503
    with patch("main.check_history.get_check", side_effect=RuntimeError("down")):
        assert client.get("/api/history/abc").status_code == 503


def test_history_endpoints_need_a_session_when_login_is_on(monkeypatch):
    monkeypatch.setenv("APP_USERNAME", "user@example.com")
    monkeypatch.setenv("APP_PASSWORD", "secret")
    fresh = TestClient(main.app)
    assert fresh.get("/api/history").status_code == 401
    assert fresh.get("/api/history/abc").status_code == 401
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_main_check_history.py -q -k history_`
Expected: FAIL — 404/405 from the missing routes (and the 401 test passes already: the middleware gates every `/api/*`; that is fine).

- [ ] **Step 3: Add the endpoints**

In `main.py`, directly after `verify_paired_stream_endpoint`:

```python
_HISTORY_UNAVAILABLE = "Riwayat tidak dapat dibuka saat ini: database riwayat tidak terhubung."


@app.get("/api/history", response_model=CheckHistoryListResponse)
async def list_history_endpoint(limit: int = 50, offset: int = 0, q: str = "") -> CheckHistoryListResponse:
    """The shared check history, newest first, without the stored results.

    q: matches the document's file name or the checker's name, case-insensitively.
    limit: page size, 1-100. has_more says whether another page follows.
    """
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    try:
        items, has_more = await asyncio.to_thread(check_history.list_checks, limit, offset, q)
    except Exception as exc:
        logger.warning("History list failed", exc_info=True)
        raise HTTPException(status_code=503, detail=_HISTORY_UNAVAILABLE) from exc
    return CheckHistoryListResponse(items=items, has_more=has_more)


@app.get("/api/history/{check_id}")
async def get_history_endpoint(check_id: str) -> JSONResponse:
    """One stored check: its summary, and its full result exactly as /api/verify-paired returned
    it (history_id set). The result is sent as stored, not re-validated against today's schema,
    so an old entry still opens after the response model has grown."""
    try:
        entry = await asyncio.to_thread(check_history.get_check, check_id)
    except Exception as exc:
        logger.warning("History read failed for %s", check_id, exc_info=True)
        raise HTTPException(status_code=503, detail=_HISTORY_UNAVAILABLE) from exc
    if entry is None:
        raise HTTPException(status_code=404, detail="Pengecekan ini tidak ada di riwayat.")
    summary, result = entry
    return JSONResponse({"summary": summary.model_dump(mode="json"), "result": result})
```

(`JSONResponse` is already imported from `fastapi.responses`; check the import block at ~line 70 and add it if not.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_main_check_history.py -q`
Expected: all pass.

- [ ] **Step 5: Document the endpoints in `README.md`**

In the `## API` table, after the `/api/verify-paired-stream` row, add:

```
| `/api/history` | GET | The shared history of finished checks, newest first: summary rows only (`?limit=` 1-100, `?offset=`, `?q=` searches file name and checker name). Every successful `/api/verify-paired(-stream)` run is stored, with an optional `checker_name` query param; its id comes back as `history_id` (null when storing failed - the check itself still succeeded). |
| `/api/history/{id}` | GET | One stored check: `{"summary": ..., "result": ...}`, where `result` is the original `/api/verify-paired` payload. Stored in Postgres when `HISTORY_DATABASE_URL` is set (e.g. a free Neon project), otherwise in a local `history.db`. |
```

- [ ] **Step 6: Run the full suite**

Run: `.venv/Scripts/python -m pytest -q`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add main.py README.md tests/test_main_check_history.py
git commit -m "List and reopen stored checks through /api/history"
```

---

### Task 4: UI — checker name on the form, and a "saved" note on each result

**Files:**
- Modify: `static/index.html` (CSS after the `.cancel-btn .material-symbols-outlined` rule ~line 210; HTML in `.card-footer` of `#panel-paired` ~line 985; JS before `// ══ Paired verification with streamed progress` ~line 2065, `runPairedVerification` ~line 2153, the `[data-action="paired"]` click handler ~line 2196)

**Interfaces:**
- Consumes: query param `checker_name` and response field `history_id` (Task 2).
- Produces: JS `checkerNameEl`, `renderHistoryNote(result)`; `runPairedVerification(resultEl, formData, sheetNames, mode, withTypo, crossPdf, withCharts, checkerName, signal)` (new `checkerName` argument before `signal`).

No automated test pins `index.html`; verification is `node --check` on the inline script plus the browser check in Task 5.

- [ ] **Step 1: Add the CSS**

After `.cancel-btn .material-symbols-outlined { font-size: 16px; }`:

```css
/* Optional name stored with the check in the shared history (Riwayat). margin-right:auto
   pushes it to the left of the footer, away from the action buttons. */
.checker-field {
  margin-right: auto; display: inline-flex; align-items: center; gap: 6px;
  border: 1px solid var(--border); border-radius: var(--r); background: var(--white);
  padding: 0 10px; color: var(--text-400);
}
.checker-field:focus-within { border-color: var(--blue-400); }
.checker-field .material-symbols-outlined { font-size: 16px; }
.checker-field input {
  border: none; outline: none; background: transparent; font-family: inherit;
  font-size: 13px; padding: 7px 0; width: 200px; color: var(--text-900);
}
/* Above each finished result: whether it made it into the history. */
.history-note {
  display: flex; align-items: center; gap: 6px; margin-bottom: 10px;
  font-size: 12px; color: var(--text-400);
}
.history-note .material-symbols-outlined { font-size: 16px; }
.history-note.is-saved { color: #15803D; }
.history-note.is-unsaved { color: #B45309; }
```

- [ ] **Step 2: Add the input to the form footer**

In `#panel-paired`'s `<div class="card-footer">`, as its first child (before the `cancel-btn` button):

```html
              <label class="checker-field" title="Nama ini ikut tersimpan di riwayat pengecekan">
                <span class="material-symbols-outlined">person</span>
                <input type="text" id="checker-name" maxlength="80" autocomplete="name"
                       placeholder="Nama pemeriksa (opsional)">
              </label>
```

- [ ] **Step 3: Remember the name and render the note**

Directly before the `// ══ Paired verification with streamed progress ══...` comment:

```js
  // ══ Check history: checker name + "saved" note ══════════════════════
  // The checker's name is remembered per browser so it is typed once. Storage can be
  // unavailable (private window, blocked site data); that must never break the form.
  const CHECKER_NAME_KEY = "dqa.checkerName";
  const checkerNameEl = document.getElementById("checker-name");
  try { checkerNameEl.value = localStorage.getItem(CHECKER_NAME_KEY) || ""; } catch (_) {}
  checkerNameEl.addEventListener("change", () => {
    try { localStorage.setItem(CHECKER_NAME_KEY, checkerNameEl.value.trim()); } catch (_) {}
  });

  function renderHistoryNote(result) {
    if (result.history_id) {
      return `<div class="history-note is-saved">
        <span class="material-symbols-outlined">history</span>Tersimpan di riwayat</div>`;
    }
    return `<div class="history-note is-unsaved">
      <span class="material-symbols-outlined">warning</span>Hasil ini tidak tersimpan di riwayat</div>`;
  }
```

- [ ] **Step 4: Send the name and show the note**

In `runPairedVerification`, change the signature to:

```js
  async function runPairedVerification(resultEl, formData, sheetNames, mode, withTypo, crossPdf, withCharts, checkerName, signal) {
```

change the URL to:

```js
        `/api/verify-paired-stream?sheet_names=${encodeURIComponent(sheetNames)}` +
          `&mode=${encodeURIComponent(mode)}&run_typo_check=${withTypo}&check_charts=${withCharts}` +
          `&checker_name=${encodeURIComponent(checkerName)}`,
```

and change `resultEl.innerHTML = renderPairedResponse(result);` to:

```js
    resultEl.innerHTML = renderHistoryNote(result) + renderPairedResponse(result);
```

In the `[data-action="paired"]` click handler, next to the other snapshotted settings (after `const withCharts = ...`):

```js
    const checkerName = checkerNameEl.value.trim();
```

and in the loop change the call to:

```js
        await runPairedVerification(body, buildFormData(pdfFiles[idx]), sheetNames, mode, withTypo, crossPdf, withCharts, checkerName, controller.signal);
```

- [ ] **Step 5: Syntax-check the inline script**

Extract the page's inline `<script>` (the longest one) to `<scratchpad>/inline_check.js` and
syntax-check it (bash, from the repo root; `<scratchpad>` = this session's scratchpad directory):

```bash
.venv/Scripts/python -c "import re, pathlib, sys; s = pathlib.Path('static/index.html').read_text(encoding='utf-8'); pathlib.Path(sys.argv[1]).write_text(max(re.findall('<script>(.*?)</script>', s, re.S), key=len), encoding='utf-8')" "<scratchpad>/inline_check.js" && node --check "<scratchpad>/inline_check.js" && echo SYNTAX-OK
```

Expected: `SYNTAX-OK`

- [ ] **Step 6: Commit**

```bash
git add static/index.html
git commit -m "Ask for an optional checker name and say whether each result was saved"
```

---

### Task 5: UI — the Riwayat panel, reopening an entry, shareable links

**Files:**
- Modify: `static/index.html` (sidebar nav ~line 897; new panel after `#panel-paired` ~line 1007; CSS after the Task 4 rules; JS: `PANEL_TITLES` + nav handler ~line 1066, new history section after the Task 4 JS, auth gate `unlock()` ~line 2305, paired click handler)
- Create (scratch, not committed): `<scratchpad>/history_ui_launcher.py`, `<scratchpad>/check_history_ui.mjs`

**Interfaces:**
- Consumes: `GET /api/history`, `GET /api/history/{id}` (Task 3); `renderPairedResponse`, `attachPairedHandlers`, `escapeHtml`, `checkerNameEl` (existing / Task 4).
- Produces: JS `showTab(tab)`, `loadHistory(reset)`, `openHistoryEntry(id)`, `openFromHash()`, `renderHistoryBanner(summary)`, `formatWib(iso)`, `formatDuration(seconds)`.

- [ ] **Step 1: Add the nav item**

In `<nav class="sidebar-nav">`, directly after the Verifikasi `</a>`:

```html
    <a class="nav-item inactive-nav" data-tab="history" title="Riwayat">
      <span class="material-symbols-outlined">history</span>
      <span class="nav-text">Riwayat</span>
    </a>
```

- [ ] **Step 2: Add the panel**

Directly after the closing `</div>` of `#panel-paired` (before `<!-- Panel: Lihat Data -->`):

```html
    <!-- Panel: Riwayat. Every successful check, shared by everyone signed in (GET /api/history).
         Opening a row renders the stored result on the Verifikasi panel. -->
    <div class="panel" id="panel-history">
      <div class="card">
        <div class="card-header history-toolbar">
          <span class="material-symbols-outlined">search</span>
          <input type="search" id="history-search" placeholder="Cari nama dokumen atau pemeriksa">
        </div>
        <div id="history-list"></div>
        <div class="card-footer" id="history-more-row" hidden>
          <button type="button" class="history-more-btn" id="history-more">Muat lagi</button>
        </div>
      </div>
    </div>
```

- [ ] **Step 3: Add the CSS**

After the Task 4 `.history-note.is-unsaved` rule:

```css
/* ── Riwayat (check history) ── */
.history-toolbar { display: flex; align-items: center; gap: 8px; padding: 12px 18px; }
.history-toolbar .material-symbols-outlined { font-size: 18px; color: var(--text-400); }
.history-toolbar input {
  flex: 1; border: none; outline: none; background: transparent;
  font-family: inherit; font-size: 13px; color: var(--text-900);
}
.history-table-wrap { overflow-x: auto; }
.history-row { cursor: pointer; }
.history-row:focus { outline: 2px solid var(--blue-400); outline-offset: -2px; }
.history-file { max-width: 320px; overflow: hidden; text-overflow: ellipsis; }
.history-counts .is-match { color: #15803D; }
.history-counts .is-mismatch { color: #DC2626; }
.history-counts .is-unverified { color: #B45309; }
.history-empty { padding: 18px 22px; }
/* display:flex on .card-footer would otherwise beat the hidden attribute. */
.card-footer[hidden] { display: none; }
.history-more-btn {
  background: var(--white); color: var(--text-500);
  border: 1px solid var(--border); border-radius: var(--r);
  padding: 7px 14px; font-family: inherit; font-size: 13px; font-weight: 600; cursor: pointer;
}
.history-more-btn:hover { color: var(--blue-400); border-color: var(--blue-400); }
.history-banner {
  display: flex; align-items: center; gap: 8px; padding: 9px 14px; margin-bottom: 12px;
  border: 1px solid var(--border); border-radius: var(--r); background: var(--blue-50);
  font-size: 13px; color: var(--text-900);
}
.history-banner .material-symbols-outlined { font-size: 18px; color: var(--blue-400); }
.history-back {
  margin-left: auto; border: none; background: none; cursor: pointer;
  color: var(--blue-400); font-family: inherit; font-size: 13px; font-weight: 600;
}
```

- [ ] **Step 4: Turn the nav handler into `showTab`**

Replace the `PANEL_TITLES` line and the whole `document.querySelectorAll(".nav-item[data-tab]").forEach(...)` block with:

```js
  const PANEL_TITLES = { paired: "Verifikasi", history: "Riwayat", data: "Lihat Data" };

  // Also called from code (opening a history entry, its back button), not only from the nav.
  function showTab(tab) {
    document.querySelectorAll(".nav-item[data-tab]").forEach((n) => {
      const active = n.dataset.tab === tab;
      n.classList.toggle("active-nav", active);
      n.classList.toggle("inactive-nav", !active);
    });
    document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
    document.getElementById(`panel-${tab}`).classList.add("active");
    document.getElementById("header-title").textContent = PANEL_TITLES[tab] || tab;
    if (tab === "data" && !tableListLoaded) {
      tableListLoaded = true;
      loadTableList();
    }
    // Reloaded on every visit, so a check finished a moment ago is already listed.
    if (tab === "history") loadHistory(true);
  }

  document.querySelectorAll(".nav-item[data-tab]").forEach((item) => {
    item.addEventListener("click", () => showTab(item.dataset.tab));
  });
```

- [ ] **Step 5: Add the history JS**

Directly after the Task 4 `renderHistoryNote` function (use the Edit tool — this block holds a regex literal):

```js
  // ══ Riwayat panel ═══════════════════════════════════════════════════
  const HISTORY_PAGE = 50;
  const MODE_LABELS = { excel: "Excel", internal: "Intra-file", both: "Excel + intra-file", none: "Antar-file" };
  let historyItems = [];   // the summaries listed, by position; a row's data-idx points here
  let historyQuery = "";
  let historyRequest = 0;  // a newer search makes an older answer still in flight stale
  let historySearchTimer = null;

  function formatWib(iso) {
    return new Date(iso).toLocaleString("id-ID", {
      timeZone: "Asia/Jakarta", day: "numeric", month: "short", year: "numeric",
      hour: "2-digit", minute: "2-digit",
    }) + " WIB";
  }

  function formatDuration(seconds) {
    const s = Math.round(seconds);
    return s < 60 ? `${s} dtk` : `${Math.floor(s / 60)} mnt ${s % 60} dtk`;
  }

  async function fetchJson(url) {
    const res = await fetch(url);
    if (!res.ok) {
      let detail = `Server membalas ${res.status}.`;
      try { detail = (await res.json()).detail || detail; } catch (_) {}
      throw new Error(detail);
    }
    return res.json();
  }

  function renderHistoryRow(item, idx) {
    const who = item.checker_name ? escapeHtml(item.checker_name) : `<span class="muted-text">—</span>`;
    return `<tr class="history-row" data-idx="${idx}" tabindex="0">
      <td>${escapeHtml(formatWib(item.created_at))}</td>
      <td class="history-file" title="${escapeHtml(item.filename)}">${escapeHtml(item.filename)}</td>
      <td>${who}</td>
      <td>${escapeHtml(MODE_LABELS[item.mode] || item.mode)}</td>
      <td class="history-counts">
        <span class="is-match">✓ ${item.n_match}</span>
        <span class="is-mismatch">✗ ${item.n_mismatch}</span>
        <span class="is-unverified">? ${item.n_unverified}</span>
      </td>
      <td>${escapeHtml(formatDuration(item.duration_s))}</td>
    </tr>`;
  }

  async function loadHistory(reset) {
    const listEl = document.getElementById("history-list");
    const moreRow = document.getElementById("history-more-row");
    const request = ++historyRequest;
    if (reset) {
      historyItems = [];
      listEl.innerHTML = `<p class="muted-text history-empty">Memuat riwayat…</p>`;
    }
    const params = new URLSearchParams({ limit: HISTORY_PAGE, offset: historyItems.length, q: historyQuery });
    let page;
    try {
      page = await fetchJson(`/api/history?${params}`);
    } catch (err) {
      if (request !== historyRequest) return;
      const banner = `<div class="error-banner">${escapeHtml(err.message)}</div>`;
      // A failed "Muat lagi" keeps the rows already listed.
      if (reset) listEl.innerHTML = banner; else listEl.insertAdjacentHTML("beforeend", banner);
      moreRow.hidden = true;
      return;
    }
    if (request !== historyRequest) return;
    historyItems = historyItems.concat(page.items);
    moreRow.hidden = !page.has_more;
    if (!historyItems.length) {
      listEl.innerHTML = `<p class="muted-text history-empty">${historyQuery
        ? "Tidak ada pengecekan yang cocok." : "Belum ada pengecekan yang tersimpan."}</p>`;
      return;
    }
    listEl.innerHTML = `<div class="history-table-wrap"><table class="data-table history-table">
      <thead><tr><th>Waktu</th><th>Dokumen</th><th>Pemeriksa</th><th>Mode</th><th>Hasil</th><th>Durasi</th></tr></thead>
      <tbody>${historyItems.map(renderHistoryRow).join("")}</tbody>
    </table></div>`;
  }

  function renderHistoryBanner(summary) {
    const who = summary.checker_name ? ` · oleh ${escapeHtml(summary.checker_name)}` : "";
    return `<div class="history-banner">
      <span class="material-symbols-outlined">history</span>
      <span>Dari riwayat · ${escapeHtml(formatWib(summary.created_at))}${who}</span>
      <button type="button" class="history-back">← Kembali ke riwayat</button>
    </div>`;
  }

  function clearHistoryHash() {
    window.history.replaceState(null, "", window.location.pathname + window.location.search);
  }

  async function openHistoryEntry(id) {
    showTab("paired");
    const resultEl = document.getElementById("paired-result");
    resultEl.innerHTML = `<p class="muted-text">Memuat hasil dari riwayat…</p>`;
    let entry;
    try {
      entry = await fetchJson(`/api/history/${encodeURIComponent(id)}`);
    } catch (err) {
      resultEl.innerHTML = `<div class="error-banner">${escapeHtml(err.message)}</div>`;
      return;
    }
    // replaceState, not location.hash: it does not fire hashchange, so this cannot loop.
    window.history.replaceState(null, "", `#riwayat/${id}`);
    // A fresh element per entry: attachPairedHandlers adds listeners to the element it is
    // given, and reusing #paired-result across several opened entries would stack them (a
    // chart thumbnail would then zoom and un-zoom on a single click).
    const body = document.createElement("div");
    body.innerHTML = renderHistoryBanner(entry.summary) + renderPairedResponse(entry.result);
    resultEl.replaceChildren(body);
    document.getElementById("paired-container").classList.add("has-results");
    document.getElementById("panel-paired").classList.add("has-results");
    attachPairedHandlers(body);
    body.querySelector(".history-back").addEventListener("click", () => {
      clearHistoryHash();
      showTab("history");
    });
  }

  // A shared link (…/#riwayat/<id>) opens its entry: on load once signed in (see unlock() in
  // the login gate) and when the hash changes in an open tab.
  function openFromHash() {
    const m = /^#riwayat\/([0-9a-f-]{36})$/i.exec(window.location.hash);
    if (m) openHistoryEntry(m[1]);
  }
  window.addEventListener("hashchange", openFromHash);

  const historyListEl = document.getElementById("history-list");
  historyListEl.addEventListener("click", (e) => {
    const row = e.target.closest(".history-row");
    if (row) openHistoryEntry(historyItems[Number(row.dataset.idx)].id);
  });
  historyListEl.addEventListener("keydown", (e) => {
    const row = e.target.closest(".history-row");
    if (row && e.key === "Enter") openHistoryEntry(historyItems[Number(row.dataset.idx)].id);
  });
  document.getElementById("history-more").addEventListener("click", () => loadHistory(false));
  document.getElementById("history-search").addEventListener("input", (e) => {
    clearTimeout(historySearchTimer);
    historySearchTimer = setTimeout(() => {
      historyQuery = e.target.value.trim();
      loadHistory(true);
    }, 300);
  });
```

- [ ] **Step 6: Hook up the login gate and a new run**

In `initAuthGate`'s `unlock()`, add as its last line:

```js
      openFromHash();
```

In the `[data-action="paired"]` click handler, directly before `withDisabledButton(e.currentTarget, async () => {`:

```js
    // A new run replaces whatever entry from the history was open, so its link no longer applies.
    clearHistoryHash();
```

- [ ] **Step 7: Syntax-check the inline script**

Run the same command as Task 4 Step 5.
Expected: `SYNTAX-OK`

- [ ] **Step 8: Write the UI check launcher (scratchpad, not committed)**

Create `<scratchpad>/history_ui_launcher.py` (`<scratchpad>` = this session's scratchpad directory):

```python
"""Serve the app without the login gate on :8765, with two checks already in the history."""
import logging
import os
import sys
from pathlib import Path

REPO = Path(r"C:\Users\Ivan Jehuda Angi\Coding\fact-checker")
os.environ["APP_USERNAME"] = ""  # in Python, not PowerShell: see the ui-browser-check notes
os.environ["HISTORY_DATABASE_URL"] = "sqlite:///" + Path(sys.argv[1]).as_posix()
sys.path.insert(0, str(REPO))
os.chdir(REPO)
logging.basicConfig(level=logging.INFO)

import uvicorn  # noqa: E402

import check_history  # noqa: E402
import main  # noqa: E402
from schemas import ChartCheck, CheckSummary, PairedVerificationResponse  # noqa: E402

PNG_1PX = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
           "YAAAAAYAAjCB0C8AAAAASUVORK5CYII=")
chart = ChartCheck(page_number=3, caption="Grafik 1", title="Uang Beredar", verdict="Entailed",
                   label_count=4, entailed_count=4, refuted_count=0, inconclusive_count=0,
                   thumbnail=PNG_1PX)
for name, who in [("SK-Agustus-2026.pdf", "Ivan"), ("M2_Juli.pdf", None)]:
    result = PairedVerificationResponse(
        pdf_filename=name, excel_filenames=[], excel_sheets=[], excel_units=[], mode="internal",
        total_facts=3, entailed_count=2, refuted_count=1, inconclusive_count=0, results=[],
        chart_checks=[chart], chart_label_count=4,
    )
    check_history.save_check(
        CheckSummary(filename=name, file_kind="pdf", checker_name=who, mode="internal",
                     n_facts=3, n_match=2, n_mismatch=1, n_charts=1, duration_s=74.2),
        result.model_dump(mode="json"),
    )
uvicorn.run(main.app, host="127.0.0.1", port=8765)
```

Run it in the background with a fresh DB file:
`.venv/Scripts/python <scratchpad>/history_ui_launcher.py <scratchpad>/ui_history.db` (delete `ui_history.db` first if it exists).

- [ ] **Step 9: Write and run the browser check**

Create `<scratchpad>/check_history_ui.mjs`:

```js
// Drives the Riwayat panel in headless Chrome over CDP. Usage: node check_history_ui.mjs <profile-dir>
import { spawn } from "node:child_process";

const CHROME = "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const APP = "http://127.0.0.1:8765/";
const chrome = spawn(CHROME, ["--headless=new", "--remote-debugging-port=9333",
  `--user-data-dir=${process.argv[2]}`, "about:blank"]);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

let target;
for (let i = 0; i < 50 && !target; i++) {
  try { target = (await (await fetch("http://127.0.0.1:9333/json/list")).json()).find((t) => t.type === "page"); } catch (_) {}
  if (!target) await sleep(200);
}
const ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((r) => ws.addEventListener("open", r, { once: true }));
let nextId = 0;
const pending = new Map();
ws.addEventListener("message", (e) => {
  const m = JSON.parse(e.data);
  if (pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
});
const send = (method, params = {}) => new Promise((r) => {
  const id = ++nextId; pending.set(id, r); ws.send(JSON.stringify({ id, method, params }));
});
const evaluate = async (expr) =>
  (await send("Runtime.evaluate", { expression: expr, awaitPromise: true, returnByValue: true })).result.result.value;
const waitFor = async (expr, label) => {
  for (let i = 0; i < 50; i++) { if (await evaluate(expr)) return; await sleep(200); }
  throw new Error(`timed out: ${label}`);
};
const failures = [];
const check = (ok, label) => { console.log(`${ok ? "PASS" : "FAIL"} ${label}`); if (!ok) failures.push(label); };

try {
  await send("Page.navigate", { url: APP });
  await waitFor(`!document.body.classList.contains("auth-locked")`, "shell unlocked");

  await evaluate(`document.querySelector('.nav-item[data-tab="history"]').click()`);
  await waitFor(`document.querySelectorAll(".history-row").length === 2`, "two rows listed");
  check((await evaluate(`document.querySelector(".history-row").textContent`)).includes("M2_Juli.pdf"), "newest check listed first");
  check((await evaluate(`document.querySelector(".history-row td").textContent`)).includes("WIB"), "time shown in WIB");
  check(await evaluate(`document.getElementById("header-title").textContent === "Riwayat"`), "header says Riwayat");

  await evaluate(`(() => { const s = document.getElementById("history-search"); s.value = "agustus"; s.dispatchEvent(new Event("input")); })()`);
  await waitFor(`document.querySelectorAll(".history-row").length === 1`, "search narrows to one row");

  await evaluate(`document.querySelector(".history-row").click()`);
  await waitFor(`!!document.querySelector("#paired-result .history-banner")`, "entry opens");
  check(await evaluate(`document.getElementById("panel-paired").classList.contains("active")`), "entry shows on the Verifikasi panel");
  check(/^#riwayat\/[0-9a-f-]{36}$/.test(await evaluate(`location.hash`)), "URL carries #riwayat/<id>");
  check((await evaluate(`document.querySelector(".history-banner").textContent`)).includes("oleh Ivan"), "banner names the checker");
  const link = await evaluate(`location.href`);

  await evaluate(`document.querySelector(".history-back").click()`);
  await waitFor(`document.getElementById("panel-history").classList.contains("active")`, "back returns to Riwayat");
  check((await evaluate(`location.hash`)) === "", "back clears the hash");

  await waitFor(`document.querySelectorAll(".history-row").length === 1`, "list reloaded");
  await evaluate(`document.querySelector(".history-row").click()`);
  await waitFor(`!!document.querySelector("#paired-result .history-banner")`, "second open");
  check(await evaluate(`document.getElementById("paired-result").children.length === 1`), "a second open replaces the first");
  check(await evaluate(`(() => { const t = document.querySelector("#paired-result .chart-thumb"); t.click(); return t.classList.contains("zoomed"); })()`),
    "thumbnail still zooms after a second open");

  await send("Page.navigate", { url: "about:blank" });
  await sleep(300);
  await send("Page.navigate", { url: link });
  await waitFor(`!!document.querySelector("#paired-result .history-banner")`, "shared link opens on load");
  check(true, "shared link opens the entry on a fresh load");
} catch (err) {
  check(false, err.message);
} finally {
  ws.close();
  chrome.kill();
}
if (failures.length) { console.error(`${failures.length} UI check(s) failed`); process.exit(1); }
console.log("All UI checks passed");
```

Run: `node <scratchpad>/check_history_ui.mjs <scratchpad>/chrome-profile`
Expected: every line `PASS`, then `All UI checks passed`. Stop the launcher afterwards.

- [ ] **Step 10: Check a real run still renders, with the note**

With the launcher still running, in the same CDP session (or by hand at `http://127.0.0.1:8765/`), call:

```js
document.getElementById("paired-result").innerHTML =
  renderHistoryNote({ history_id: null }) + renderPairedResponse({ pdf_filename: "x.pdf",
  excel_filenames: [], excel_sheets: [], excel_units: [], total_facts: 0, entailed_count: 0,
  refuted_count: 0, inconclusive_count: 0, results: [] });
```

Expected: the amber "Hasil ini tidak tersimpan di riwayat" line above an empty result, no console errors.

- [ ] **Step 11: Run the full suite once more**

Run: `.venv/Scripts/python -m pytest -q`
Expected: all pass.

- [ ] **Step 12: Commit**

```bash
git add static/index.html
git commit -m "Add a Riwayat panel that lists, searches and reopens stored checks by link"
```

---

## After the last task (manual, by the user)

1. Create a free project at neon.tech (region: Singapore). Copy its connection string (it already ends in `?sslmode=require`).
2. In Render → the service → Environment, add `HISTORY_DATABASE_URL` = that string, and redeploy.
3. Check the Render log after the deploy: there must be **no** `HISTORY_DATABASE_URL is not set` warning. Run one check, open Riwayat, and confirm it is listed.
