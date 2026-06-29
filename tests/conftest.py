import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Force a key-less provider so importing llm_provider (directly, or transitively via
# verifier/orchestrator/main) never requires real API credentials during tests.
os.environ.setdefault("LLM_PROVIDER", "ollama")
