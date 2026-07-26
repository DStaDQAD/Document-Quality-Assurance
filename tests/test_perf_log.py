import json

from perf_log import StageTimer, append_perf_record, build_record


def test_stage_timer_records_first_running_to_done():
    t = StageTimer()
    t.observe({"stage": "pdf", "status": "running"})
    t.observe({"stage": "pdf", "status": "done"})
    d = t.durations()

    assert "pdf" in d
    assert d["pdf"] >= 0


def test_stage_timer_spans_multiple_running_events():
    # "excel"/"compare" fire "running" several times before one "done" — timed across all.
    t = StageTimer()
    t.observe({"stage": "compare", "status": "running", "detail": "9 klaim"})
    t.observe({"stage": "compare", "status": "running", "detail": "penunjukan sel AI"})
    t.observe({"stage": "compare", "status": "done"})

    assert "compare" in t.durations()


def test_stage_timer_ignores_events_without_stage_or_matching_start():
    t = StageTimer()
    t.observe({"status": "done"})                         # no stage
    t.observe({"stage": "typo", "status": "done"})        # done without a prior running

    assert t.durations() == {}


def test_stage_timer_total_is_positive():
    t = StageTimer()
    assert t.total() >= 0


def test_build_record_shape():
    t = StageTimer()
    t.observe({"stage": "pdf", "status": "running"})
    t.observe({"stage": "pdf", "status": "done"})

    rec = build_record(
        pdf_filename="a.pdf", n_pages=3, n_chars=1200, n_facts=9, n_excel_sources=5, timer=t,
    )

    assert rec["pdf_filename"] == "a.pdf"
    assert rec["n_facts"] == 9
    assert rec["n_excel_sources"] == 5
    assert "pdf" in rec["stages"]
    assert "total_s" in rec and "ts" in rec


def test_append_perf_record_writes_one_jsonl_line(tmp_path):
    path = tmp_path / "perf.jsonl"
    append_perf_record({"pdf_filename": "x.pdf", "total_s": 1.5}, path=str(path))
    append_perf_record({"pdf_filename": "y.pdf", "total_s": 2.5}, path=str(path))

    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["pdf_filename"] == "x.pdf"
    assert json.loads(lines[1])["total_s"] == 2.5


def test_append_perf_record_swallows_write_errors():
    # A bad path must never raise — instrumentation can't break a request.
    append_perf_record({"total_s": 1.0}, path="/nonexistent_dir_xyz/perf.jsonl")
