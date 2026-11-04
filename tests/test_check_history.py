import gzip
import json
import logging
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select, text

import check_history
from schemas import CheckSummary, TranslationCheckSummary

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

    items, has_more, _ = check_history.list_checks()

    assert [i.id for i in items] == [second, first]
    assert has_more is False
    assert "result_gz" not in items[0].model_dump()


def test_created_at_comes_back_as_utc(clock):
    _save()

    items, _, _ = check_history.list_checks()

    assert items[0].created_at == datetime(2026, 9, 28, 7, 0, tzinfo=timezone.utc)
    assert items[0].created_at.utcoffset() == timedelta(0)


def test_lists_round_trip(clock):
    _save(excel_files=["TABEL1_1.xls", "TABEL2_1.xls"], reference_files=["M2_Juli.pdf"])

    items, _, _ = check_history.list_checks()

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

    page1, more1, _ = check_history.list_checks(limit=2)
    page3, more3, _ = check_history.list_checks(limit=2, offset=4)

    assert [p.id for p in page1] == [ids[4], ids[3]]
    assert more1 is True
    assert [p.id for p in page3] == [ids[0]]
    assert more3 is False


def test_filter_by_publication_and_mismatch_with_the_total(clock):
    _save("skdu_a.docx", publication="skdu", n_mismatch=2)
    _save("skdu_b.docx", publication="skdu")
    _save("ub.docx", publication="uang-beredar", n_mismatch=1)

    skdu, more, total = check_history.list_checks(publication="skdu")
    assert [i.filename for i in skdu] == ["skdu_b.docx", "skdu_a.docx"] and not more and total == 2
    flagged = check_history.list_checks(mismatch_only=True)[0]
    assert [i.filename for i in flagged] == ["ub.docx", "skdu_a.docx"]
    assert [i.filename for i in check_history.list_checks(publication="skdu", mismatch_only=True)[0]] == ["skdu_a.docx"]


def test_total_counts_every_match_not_just_the_page(clock):
    for i in range(5):
        _save(f"r{i}.pdf")
    items, more, total = check_history.list_checks(limit=2)
    assert len(items) == 2 and more and total == 5


def test_date_filter_is_whole_wib_days(monkeypatch):
    # 27 Sep 23:30 WIB, 28 Sep 00:30 WIB, 28 Sep 23:30 WIB, 29 Sep 00:30 WIB — in UTC.
    times = iter([datetime(2026, 9, 27, 16, 30, tzinfo=timezone.utc), datetime(2026, 9, 27, 17, 30, tzinfo=timezone.utc),
                  datetime(2026, 9, 28, 16, 30, tzinfo=timezone.utc), datetime(2026, 9, 28, 17, 30, tzinfo=timezone.utc)])
    monkeypatch.setattr(check_history, "_now", lambda: next(times))
    for name in ("27-late", "28-early", "28-late", "29-early"):
        _save(name)

    def names(**kw):
        return [i.filename for i in check_history.list_checks(**kw)[0]]

    day = date(2026, 9, 28)
    assert names(date_from=day, date_to=day) == ["28-late", "28-early"]
    assert names(date_from=day) == ["29-early", "28-late", "28-early"]
    assert names(date_to=day) == ["28-late", "28-early", "27-late"]


def test_translation_list_filters_by_publication_and_date(clock):
    _tsave("sk.docx", publication="sk")
    _tsave("ub.docx", publication="uang-beredar")

    items, _, total = check_history.list_translation_checks(publication="sk")
    assert [i.en_filename for i in items] == ["sk.docx"] and total == 1
    assert check_history.list_translation_checks(date_from=date(2026, 9, 29))[0] == []


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


def test_publication_is_stored_and_unknown_or_blank_becomes_none(clock):
    _save("a.pdf", publication="uang-primer-m0")
    _save("b.pdf", publication=" SK ")
    _save("c.pdf", publication="bukan-publikasi")
    _save("d.pdf", publication="")
    _save("e.pdf")

    by_file = {i.filename: i.publication for i in check_history.list_checks()[0]}

    assert by_file == {"a.pdf": "uang-primer-m0", "b.pdf": "sk", "c.pdf": None,
                       "d.pdf": None, "e.pdf": None}
    check_id = _save("f.pdf", publication="cadangan-devisa")
    assert check_history.get_check(check_id)[0].publication == "cadangan-devisa"


def test_a_table_created_before_publication_existed_gets_the_column(tmp_path, monkeypatch):
    # Production's Neon table was created by the first history release, without this column;
    # create_all never alters an existing table.
    url = f"sqlite:///{(tmp_path / 'old.db').as_posix()}"
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE checks (id VARCHAR(36) PRIMARY KEY, created_at DATETIME NOT NULL, "
            "filename VARCHAR(512) NOT NULL, file_kind VARCHAR(8) NOT NULL, "
            "checker_name VARCHAR(80), mode VARCHAR(16) NOT NULL, excel_files TEXT NOT NULL, "
            "reference_files TEXT NOT NULL, n_facts INTEGER NOT NULL, n_match INTEGER NOT NULL, "
            "n_mismatch INTEGER NOT NULL, n_unverified INTEGER NOT NULL, n_typos INTEGER NOT NULL, "
            "n_charts INTEGER NOT NULL, duration_s FLOAT NOT NULL, tokens_in INTEGER NOT NULL, "
            "tokens_out INTEGER NOT NULL, result_gz BLOB NOT NULL)"))
    engine.dispose()
    monkeypatch.setenv("HISTORY_DATABASE_URL", url)
    check_history.reset_engine()

    _save("lama.pdf", publication="sulni")

    assert [i.publication for i in check_history.list_checks()[0]] == ["sulni"]


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


# --- Cek Terjemahan: its own table ------------------------------------------------------------

def _tsave(en_filename="UB_EN.docx", result=None, **overrides):
    base = dict(document="laporan", id_filename="UB_ID.docx", en_filename=en_filename)
    base.update(overrides)
    return check_history.save_translation_check(TranslationCheckSummary(**base), result or {"pairs": []})


def test_translation_check_round_trips_and_lists_apart_from_checks(clock):
    tid = _tsave(n_number=1, n_meaning=2, result={"pairs": [{"index": 0}]})

    items, more, _ = check_history.list_translation_checks()
    assert [i.id for i in items] == [tid] and not more
    assert items[0].n_number == 1 and items[0].n_meaning == 2
    assert check_history.list_checks()[0] == []
    summary, result = check_history.get_translation_check(tid)
    assert summary.en_filename == "UB_EN.docx" and result == {"pairs": [{"index": 0}], "history_id": tid}
    assert check_history.get_translation_check("does-not-exist") is None


def test_translation_search_matches_either_filename_and_checker(clock):
    _tsave("UB_Agustus_EN.docx", id_filename="UB_Agustus_ID.docx", checker_name="Ivan")
    _tsave("SK_Juli_EN.docx", id_filename="SK_Juli_ID.docx", checker_name="Budi")

    assert [i.en_filename for i in check_history.list_translation_checks(q="agustus_id")[0]] == ["UB_Agustus_EN.docx"]
    assert [i.en_filename for i in check_history.list_translation_checks(q="sk_juli_en")[0]] == ["SK_Juli_EN.docx"]
    assert [i.en_filename for i in check_history.list_translation_checks(q="BUDI")[0]] == ["SK_Juli_EN.docx"]


def test_translation_publication_and_checker_are_normalised_like_checks(clock):
    tid = _tsave(publication=" SK ", checker_name="  Ivan   Jehuda ")
    _tsave("b.docx", publication="bukan-publikasi", checker_name="  ")

    summary = check_history.get_translation_check(tid)[0]
    assert (summary.publication, summary.checker_name) == ("sk", "Ivan Jehuda")
    other = [i for i in check_history.list_translation_checks()[0] if i.en_filename == "b.docx"][0]
    assert (other.publication, other.checker_name) == (None, None)


# --- Column filters and sorting (the ▾ on each History column) --------------------------------

from schemas import HistoryColumnFilter, HistoryQuery  # noqa: E402


def _q(**kw):
    columns = {k: HistoryColumnFilter(**v) for k, v in kw.pop("columns", {}).items()}
    return HistoryQuery(columns=columns, **kw)


def _names(query, kind="paired"):
    items = check_history.search_history(kind, query)[0]
    return [i.filename if kind == "paired" else i.en_filename for i in items]


def test_a_column_filter_keeps_the_ticked_values_and_umum_is_the_empty_value(clock):
    _save("a.docx", publication="sk", checker_name="Ivan")
    _save("b.docx", publication="skdu", checker_name="Dita")
    _save("c.docx", checker_name=None)

    assert _names(_q(columns={"publication": {"values": ["sk", ""]}})) == ["c.docx", "a.docx"]
    assert _names(_q(columns={"checker": {"values": [""]}})) == ["c.docx"]
    # Unticked values instead: the rows without a value are kept, not lost to a NULL comparison.
    assert _names(_q(columns={"publication": {"values": ["sk"], "exclude": True}})) == ["c.docx", "b.docx"]
    assert _names(_q(columns={"checker": {"values": ["Ivan", ""], "exclude": True}})) == ["b.docx"]
    # Nothing ticked keeps nothing; filters on two columns both apply.
    assert _names(_q(columns={"mode": {"values": []}})) == []
    assert _names(_q(columns={"publication": {"values": ["sk", "skdu"]},
                              "checker": {"values": ["Dita"]}})) == ["b.docx"]


def test_a_day_filter_ticks_whole_wib_days(monkeypatch):
    times = iter([datetime(2026, 9, 27, 16, 30, tzinfo=timezone.utc), datetime(2026, 9, 27, 17, 30, tzinfo=timezone.utc),
                  datetime(2026, 9, 28, 17, 30, tzinfo=timezone.utc)])
    monkeypatch.setattr(check_history, "_now", lambda: next(times))
    for name in ("27-late", "28-early", "29-early"):
        _save(name)

    assert _names(_q(columns={"day": {"values": ["2026-09-28", "2026-09-29"]}})) == ["29-early", "28-early"]
    assert _names(_q(columns={"day": {"values": ["2026-09-28"], "exclude": True}})) == ["29-early", "27-late"]
    assert _names(_q(columns={"day": {"values": ["bukan-tanggal"]}})) == []
    assert check_history.history_facet("paired", _q(), "day") == [
        ("2026-09-29", 1), ("2026-09-28", 1), ("2026-09-27", 1)]


def test_sorting_by_a_column_breaks_ties_newest_first(clock):
    _save("b.docx", n_mismatch=1, duration_s=30.0)
    _save("A.docx", n_mismatch=3, duration_s=10.0)
    _save("c.docx", n_mismatch=1, duration_s=20.0)

    assert _names(_q(sort="filename", descending=False)) == ["A.docx", "b.docx", "c.docx"]
    assert _names(_q(sort="result")) == ["A.docx", "c.docx", "b.docx"]
    assert _names(_q(sort="duration", descending=False)) == ["A.docx", "c.docx", "b.docx"]
    assert _names(_q(sort="created_at", descending=False)) == ["b.docx", "A.docx", "c.docx"]
    items, more, total = check_history.search_history("paired", _q(sort="filename", limit=2, offset=1, descending=False))
    assert [i.filename for i in items] == ["b.docx", "c.docx"] and not more and total == 3


def test_search_keeps_the_other_filters(clock):
    _save("skdu_a.docx", publication="skdu", n_mismatch=2, checker_name="Ivan")
    _save("skdu_b.docx", publication="skdu")
    _save("ub.docx", publication="uang-beredar", n_mismatch=1)

    assert _names(_q(q="skdu", mismatch_only=True)) == ["skdu_a.docx"]
    assert _names(_q(q="ivan")) == ["skdu_a.docx"]
    assert _names(_q(date_from=date(2026, 9, 29))) == []


def test_a_facet_lists_values_under_the_other_filters_but_not_its_own(clock):
    _save("a.docx", publication="sk", checker_name="Ivan")
    _save("b.docx", publication="sk", checker_name="Dita")
    _save("c.docx", checker_name="Ivan")
    _save("d.docx", publication="skdu", checker_name="Dita", n_mismatch=1)

    narrowed = _q(columns={"publication": {"values": ["sk"]}, "checker": {"values": ["Ivan"]}})
    assert check_history.history_facet("paired", narrowed, "publication") == [("", 1), ("sk", 1)]
    assert check_history.history_facet("paired", narrowed, "checker") == [("Dita", 1), ("Ivan", 1)]
    assert check_history.history_facet("paired", _q(mismatch_only=True), "filename") == [("d.docx", 1)]
    assert check_history.history_facet("paired", _q(), "mode") == [("excel", 4)]


def test_the_translation_list_filters_its_english_file_and_document_kind(clock):
    _tsave("UB_EN.docx", document="laporan", n_number=1, n_meaning=1)
    _tsave("SK_EN.docx", document="siaran_pers", n_number=3)
    _tsave("PMI_EN.docx", document="laporan")

    assert _names(_q(columns={"mode": {"values": ["laporan"]}}), "translation") == ["PMI_EN.docx", "UB_EN.docx"]
    assert _names(_q(columns={"filename": {"values": ["SK_EN.docx"]}}), "translation") == ["SK_EN.docx"]
    assert _names(_q(sort="result"), "translation") == ["SK_EN.docx", "UB_EN.docx", "PMI_EN.docx"]
    # The Tidak Sesuai toggle is Verifikasi's; it narrows nothing here.
    assert len(_names(_q(mismatch_only=True), "translation")) == 3
    assert check_history.history_facet("translation", _q(), "mode") == [("laporan", 2), ("siaran_pers", 1)]


def test_sorting_by_publication_follows_the_label_order_the_ui_sends(clock):
    _save("skdu.docx", publication="skdu")      # "Laporan Survei Kegiatan Dunia Usaha"
    _save("sk.docx", publication="sk")          # "Survei Konsumen"
    _save("umum.docx")                          # "Umum"

    assert _names(_q(sort="publication", descending=False)) == ["umum.docx", "sk.docx", "skdu.docx"]
    by_label = _q(sort="publication", descending=False, publication_order=["skdu", "sk", ""])
    assert _names(by_label) == ["skdu.docx", "sk.docx", "umum.docx"]
    assert _names(_q(sort="publication", publication_order=["skdu", "sk", ""])) == ["umum.docx", "sk.docx", "skdu.docx"]
