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
