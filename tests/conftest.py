import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Force a key-less provider so importing llm_provider (directly, or transitively via
# verifier/orchestrator/main) never requires real API credentials during tests.
os.environ.setdefault("LLM_PROVIDER", "ollama")

# Route the pipeline's timing log to a throwaway file so endpoint tests that run
# _run_paired_pipeline don't append to the developer's real perf_log.jsonl.
os.environ.setdefault(
    "PERF_LOG_PATH", str(Path(__file__).resolve().parent.parent / ".pytest_perf_log.jsonl")
)


@pytest.fixture(autouse=True)
def _disable_basic_auth():
    """Keep the app's Basic Auth middleware disabled for every test by ensuring
    its credentials are unset (a local .env may otherwise define them). Tests
    that exercise auth set APP_USERNAME/APP_PASSWORD explicitly via monkeypatch."""
    for var in ("APP_USERNAME", "APP_PASSWORD"):
        os.environ.pop(var, None)
    yield
