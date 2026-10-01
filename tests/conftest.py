import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Force a key-less provider so importing llm_provider (directly, or transitively via
# verifier/orchestrator/main) never requires real API credentials during tests.
os.environ.setdefault("LLM_PROVIDER", "ollama")


@pytest.fixture(autouse=True)
def _disable_basic_auth():
    """Keep the app's Basic Auth middleware disabled for every test by ensuring
    its credentials are unset (a local .env may otherwise define them). Tests
    that exercise auth set APP_USERNAME/APP_PASSWORD explicitly via monkeypatch."""
    for var in ("APP_USERNAME", "APP_PASSWORD"):
        os.environ.pop(var, None)
    yield


@pytest.fixture(autouse=True)
def _fresh_pdf_table_cache():
    """main caches each PDF's transcribed tables by content, so that a PDF sent as a reference
    in one request is not transcribed again in the next. Tests reuse the same fake bytes with
    different mocked transcriptions, so every test starts from an empty cache."""
    import main
    main._PDF_TABLE_CACHE.clear()
    yield


@pytest.fixture(autouse=True)
def _isolated_history_db(tmp_path, monkeypatch):
    """Every test gets its own empty SQLite check history, so no test ever writes to the real
    history (a local history.db, or Neon when .env sets HISTORY_DATABASE_URL)."""
    import check_history
    monkeypatch.setenv("HISTORY_DATABASE_URL", f"sqlite:///{(tmp_path / 'history.db').as_posix()}")
    check_history.reset_engine()
    yield
    check_history.reset_engine()
