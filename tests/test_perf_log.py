import logging

from perf_log import StageTimer, log_perf


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


def test_log_perf_emits_readable_line(caplog):
    t = StageTimer()
    t.observe({"stage": "pdf", "status": "running"})
    t.observe({"stage": "pdf", "status": "done"})
    t.observe({"stage": "extract", "status": "running"})
    t.observe({"stage": "extract", "status": "done"})

    with caplog.at_level(logging.INFO, logger="fact-checker"):
        log_perf(pdf_filename="a.pdf", n_pages=3, n_facts=9, n_excel_sources=5, timer=t)

    line = caplog.text
    assert "perf a.pdf" in line
    assert "total=" in line and "pdf=" in line and "extract=" in line
    assert "9 fakta" in line and "5 sumber" in line


def test_log_perf_reports_token_totals_across_models(caplog):
    # A run can touch a vision/primary model and a text fallback; what we tune against is
    # the document's total, so the per-model counts are summed.
    t = StageTimer()
    usage = {
        "gemini-2.5-flash": {"input_tokens": 12000, "output_tokens": 3400},
        "llama-3.3-70b-versatile": {"input_tokens": 500, "output_tokens": 100},
    }

    with caplog.at_level(logging.INFO, logger="fact-checker"):
        log_perf(pdf_filename="a.pdf", n_pages=1, n_facts=1, n_excel_sources=1,
                 timer=t, usage_metadata=usage)

    assert "in=12.500" in caplog.text
    assert "out=3.500" in caplog.text


def test_log_perf_omits_tokens_when_the_provider_reports_none(caplog):
    # Ollama and every mocked test run report no usage — the line just drops the section.
    t = StageTimer()

    with caplog.at_level(logging.INFO, logger="fact-checker"):
        log_perf(pdf_filename="a.pdf", n_pages=1, n_facts=1, n_excel_sources=1, timer=t)

    assert "in=" not in caplog.text
    assert "perf a.pdf" in caplog.text
