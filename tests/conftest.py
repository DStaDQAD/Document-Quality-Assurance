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
