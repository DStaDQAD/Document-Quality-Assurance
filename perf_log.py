"""Server-side per-stage timing for the paired pipeline — logged to stdout only.

Observability, not a product feature: durations go to the app log (one readable line per
document, visible in Render's Logs tab), never to the API response or the frontend.

The recorder taps the pipeline's EXISTING stage events (the same "running"/"done"
progress dicts the streaming endpoint already emits), so no timing calls are scattered
through the pipeline: it just observes each stage's first "running" and its "done".
"""

import logging
from time import perf_counter
from typing import Dict, Mapping, Optional

logger = logging.getLogger("fact-checker")

STAGE_ORDER = ["pdf", "excel", "extract", "compare", "typo"]


def format_usage(usage_metadata: Optional[Mapping[str, Mapping]]) -> str:
    """Render a UsageMetadataCallbackHandler's per-model totals as ``in=… out=…``.

    Token counts are what make a prompt-size change provable, so they ride the same log
    line as the durations. The handler keys its totals by model name and a run may touch
    two models (a vision/primary plus a text fallback), so the counts are summed across
    them — the per-model split is not what we are tuning against.

    Returns "" when nothing was recorded, so the log line simply omits the section
    (providers that don't report usage, e.g. Ollama, and every mocked test run).
    """
    if not usage_metadata:
        return ""
    total_in = sum(int(u.get("input_tokens", 0) or 0) for u in usage_metadata.values())
    total_out = sum(int(u.get("output_tokens", 0) or 0) for u in usage_metadata.values())
    if not total_in and not total_out:
        return ""
    return f"in={total_in:,} out={total_out:,}".replace(",", ".")


class StageTimer:
    """Records wall-clock duration per pipeline stage from stage-event dicts.

    Feed every progress event (``{"stage": ..., "status": "running"|"done", ...}``) to
    ``observe``. Each stage is timed from its FIRST "running" to its "done" — stages that
    fire "running" several times (e.g. "excel" per source, "compare" for the pointer pass)
    are timed across the whole span. Durations may overlap in wall-clock time because some
    stages run concurrently (typo vs. fact verification); that overlap is intentional.
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


def log_perf(
    *,
    pdf_filename: str,
    n_pages: int,
    n_facts: int,
    n_excel_sources: int,
    timer: StageTimer,
    usage_metadata: Optional[Mapping[str, Mapping]] = None,
) -> None:
    """Log one readable timing + token line for a processed document.

    Example: ``perf laporan.pdf: total=67.0s pdf=0.8s excel=0.5s extract=45.3s
    compare=12.1s typo=8.4s in=48.120 out=6.310 (9 fakta, 5 sumber, 3 hal)``.
    """
    durations = timer.durations()
    parts = [f"{s}={durations[s]:.1f}s" for s in STAGE_ORDER if s in durations]
    tokens = format_usage(usage_metadata)
    if tokens:
        parts.append(tokens)
    logger.info(
        "perf %s: total=%.1fs %s (%d fakta, %d sumber, %d hal)",
        pdf_filename, timer.total(), " ".join(parts), n_facts, n_excel_sources, n_pages,
    )
