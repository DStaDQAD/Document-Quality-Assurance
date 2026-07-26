"""Offline timing report for the paired pipeline — the developer-facing consumer of
perf_log.jsonl. Run it yourself; it is NOT served by the app and never touches the UI.

    python perf_report.py                 # summarise ./perf_log.jsonl
    python perf_report.py path/to/log.jsonl
    python perf_report.py --last 50       # only the most recent 50 runs

Prints, per stage (pdf/excel/extract/compare/typo) and for the total wall-clock:
count, mean, median, p95, max — plus a mean-duration bar chart and a recent-runs trend,
so you can capture a baseline and show the before/after of an optimisation with numbers.

Accepts both a clean JSONL file and raw Render/stdout log lines: each record is parsed
from its first '{', so you can paste log lines like `INFO:fact-checker:perf {...}`
straight into a file and report on them without stripping the prefix.
"""

import argparse
import json
import statistics
import sys
from typing import Dict, List

STAGE_ORDER = ["pdf", "excel", "extract", "compare", "typo"]


def _load(path: str, last: int | None) -> List[dict]:
    """Read perf records from a JSONL file OR raw log lines.

    Each line is parsed from its first '{', so a Render/stdout line such as
    `INFO:fact-checker:perf {"...": ...}` works unchanged; non-record lines and any
    other JSON logging noise are skipped (only dicts carrying 'total_s' are kept).
    """
    records = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                brace = line.find("{")
                if brace == -1:
                    continue
                try:
                    rec = json.loads(line[brace:])
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict) and "total_s" in rec:
                    records.append(rec)
    except FileNotFoundError:
        sys.exit(f"Belum ada log: {path} (jalankan pipeline dulu agar terisi).")
    return records[-last:] if last else records


def _percentile(values: List[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, round(pct / 100 * (len(ordered) - 1))))
    return ordered[k]


def _column(records: List[dict], stage: str) -> List[float]:
    if stage == "total":
        return [r["total_s"] for r in records if "total_s" in r]
    return [r["stages"][stage] for r in records if stage in r.get("stages", {})]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("path", nargs="?", default="perf_log.jsonl")
    ap.add_argument("--last", type=int, default=None, help="hanya N run terakhir")
    args = ap.parse_args()

    records = _load(args.path, args.last)
    if not records:
        sys.exit("Log kosong.")

    print(f"\n{len(records)} run · {args.path}\n")
    header = f"{'tahap':<10}{'n':>5}{'mean':>9}{'median':>9}{'p95':>9}{'max':>9}"
    print(header)
    print("-" * len(header))

    stats: Dict[str, float] = {}
    for stage in STAGE_ORDER + ["total"]:
        vals = _column(records, stage)
        if not vals:
            continue
        mean = statistics.mean(vals)
        stats[stage] = mean
        print(f"{stage:<10}{len(vals):>5}{mean:>8.2f}s{statistics.median(vals):>8.2f}s"
              f"{_percentile(vals, 95):>8.2f}s{max(vals):>8.2f}s")

    # Mean-duration bar chart (stages only; total excluded since stages overlap it).
    stage_means = {s: stats[s] for s in STAGE_ORDER if s in stats}
    if stage_means:
        widest = max(stage_means.values()) or 1.0
        print("\nrata-rata durasi per tahap:")
        for stage, mean in stage_means.items():
            bar = "█" * round(mean / widest * 40)
            print(f"  {stage:<9}{bar} {mean:.2f}s")

    # Recent-runs trend for total wall-clock.
    totals = _column(records, "total")
    if len(totals) > 1:
        recent = totals[-12:]
        peak = max(recent) or 1.0
        print("\ntren total (run terbaru → terlama, kiri=terbaru):")
        line = " ".join("▁▂▃▄▅▆▇█"[min(7, round(v / peak * 7))] for v in reversed(recent))
        print(f"  {line}   ({recent[-1]:.1f}s … {recent[0]:.1f}s)")
    print()


if __name__ == "__main__":
    main()
