"""Server-side timing instrumentation for the paired pipeline.

Deliberately observability-only: durations are written to a JSONL log and the app's
stdout, never to the API response or the frontend. A separate offline tool
(perf_report.py) reads the accumulated log to summarise averages/trends — the app itself
never renders timing to end users.

The recorder taps the pipeline's EXISTING stage events (the same "running"/"done"
progress dicts the streaming endpoint already emits), so no timing calls are scattered
through the pipeline: it just observes each stage's first "running" and its "done".
"""

import json
import logging
import os
from datetime import datetime, timezone
from time import perf_counter
from typing import Dict, Optional

logger = logging.getLogger("fact-checker")

# Where the JSONL timing log accumulates. Configurable so a deployment can point it at a
# mounted disk; on an ephemeral filesystem (e.g. Render's default) it resets per deploy,
# which is fine for baseline measurement — download it before redeploying to keep history.
DEFAULT_PERF_LOG_PATH = "perf_log.jsonl"


class StageTimer:
    """Records wall-clock duration per pipeline stage from stage-event dicts.

    Feed every progress event (``{"stage": ..., "status": "running"|"done", ...}``) to
    ``observe``. Each stage is timed from its FIRST "running" to its "done" — stages that
    fire "running" several times (e.g. "excel" per source, "compare" for the pointer pass)
    are timed across the whole span. Durations may overlap in wall-clock time because some
    stages run concurrently (typo vs. fact verification); that overlap is intentional and
    visible when the per-stage sum exceeds ``total``.
    """

    def __init__(self) -> None:
        self._t0 = perf_counter()
        self._starts: Dict[str, float] = {}
        self._durations: Dict[str, float] = {}

    def observe(self, event: dict) -> None:
        stage = event.get("stage")
        status = event.get("status")
        if not stage:
            return
        now = perf_counter()
        if status == "running":
            self._starts.setdefault(stage, now)
        elif status == "done" and stage in self._starts:
            self._durations[stage] = round(now - self._starts[stage], 3)

    def durations(self) -> Dict[str, float]:
        return dict(self._durations)

    def total(self) -> float:
        return round(perf_counter() - self._t0, 3)


def build_record(
    *,
    pdf_filename: str,
    n_pages: int,
    n_chars: int,
    n_facts: int,
    n_excel_sources: int,
    timer: StageTimer,
) -> dict:
    """Assemble one timing record. Kept pure so it is trivially testable."""
    return {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pdf_filename": pdf_filename,
        "n_pages": n_pages,
        "n_chars": n_chars,
        "n_facts": n_facts,
        "n_excel_sources": n_excel_sources,
        "stages": timer.durations(),
        "total_s": timer.total(),
    }


def append_perf_record(record: dict, path: Optional[str] = None) -> None:
    """Append one record to the JSONL log and echo it to the app log.

    Best-effort: a failed file write is logged and swallowed so instrumentation can never
    break a verification request.
    """
    logger.info("perf %s", json.dumps(record, ensure_ascii=False))
    path = path or os.getenv("PERF_LOG_PATH", DEFAULT_PERF_LOG_PATH)
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.warning("Could not write perf log to %s: %s", path, exc)
