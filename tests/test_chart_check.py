"""The chart check: a picture to the model, its series mapped to table rows, its labels judged by
strict, its drawing judged against the tables' series."""
import asyncio
import io
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from PIL import Image

from chart_vision import NO_IMAGE, ChartUnreadable, ask_chart


def _png(width=40, height=20):
    out = io.BytesIO()
    Image.new("RGB", (width, height), (200, 0, 0)).save(out, format="PNG")
    return out.getvalue()


class _Model:
    """A non-Copilot chat model that answers `reply` and records the messages it got."""
    def __init__(self, reply):
        self.reply, self.messages = reply, []

    async def ainvoke(self, messages, **kwargs):
        self.messages.append(messages)
        return AIMessage(content=self.reply)


def test_a_non_copilot_model_gets_a_multimodal_message():
    model = _Model("oke")

    assert asyncio.run(ask_chart(model, "Baca grafik.", _png())) == "oke"
    [[message]] = model.messages
    assert isinstance(message, HumanMessage)
    kinds = [part["type"] for part in message.content]
    assert kinds == ["text", "image_url"] and message.content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_a_reply_saying_no_image_is_unreadable():
    with pytest.raises(ChartUnreadable):
        asyncio.run(ask_chart(_Model(NO_IMAGE), "Baca grafik.", _png()))
    with pytest.raises(ChartUnreadable):
        asyncio.run(ask_chart(_Model("  "), "Baca grafik.", _png()))


def test_a_large_picture_is_scaled_down_before_sending():
    import base64
    model = _Model("oke")

    asyncio.run(ask_chart(model, "Baca grafik.", _png(3200, 1000)))

    url = model.messages[0][0].content[1]["image_url"]["url"]
    sent = Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1])))
    assert sent.size == (1600, 500)


# --- scanned pages ------------------------------------------------------------------------------

from chart_images import ScannedPage
from chart_vision import locate_charts


def test_each_chart_on_a_scanned_page_is_cropped_with_its_caption():
    reply = ('```json\n{"grafik": [{"keterangan": "Grafik 3. Ekspor Nonmigas", "kotak": [0.1, 0.1, 0.6, 0.4]},'
             ' {"keterangan": "", "kotak": [0.1, 0.5, 0.9, 0.9]}]}\n```')

    charts = asyncio.run(locate_charts(ScannedPage(4, _png(200, 400)), _Model(reply)))

    assert [(c.caption, c.title, c.page_number) for c in charts] == [
        ("Grafik 3", "Ekspor Nonmigas", 4), ("Grafik hal. 4 (2)", "", 4)]
    assert [Image.open(io.BytesIO(c.png)).size for c in charts] == [(100, 120), (160, 160)]


def test_a_page_without_charts_gives_nothing():
    assert asyncio.run(locate_charts(ScannedPage(2, _png(200, 400)), _Model('{"grafik": []}'))) == []


def test_a_box_outside_the_page_is_clamped_and_a_sliver_dropped():
    reply = '{"grafik": [{"keterangan": "Grafik 1", "kotak": [-0.2, 0.5, 1.3, 1.2]}, {"keterangan": "Grafik 2", "kotak": [0.1, 0.1, 0.12, 0.5]}]}'

    [chart] = asyncio.run(locate_charts(ScannedPage(1, _png(200, 400)), _Model(reply)))

    assert chart.caption == "Grafik 1" and Image.open(io.BytesIO(chart.png)).size == (200, 200)


# --- reading a chart ----------------------------------------------------------------------------

from chart_check import ChartLabel, ChartRead, SeriesRef, read_chart
from chart_images import ChartImage
from excel_parser_bi import BITableData
from paired_verifier import _ExcelSource
from strict.tables import build_views
from word_extraction import EmfText


def _views():
    sbt = BITableData(title="T1 Kegiatan Usaha", unit="% Saldo Bersih Tertimbang - SBT", row_labels=["TOTAL", "Pertanian"])
    sbt._data.update({("TOTAL", 2026, "Q2"): 12.97, ("TOTAL", 2026, "Q3"): 11.46, ("TOTAL", 2025, "Q3"): 11.55,
                      ("Pertanian", 2026, "Q2"): 8.1})
    gas = BITableData(title="Neraca Pembayaran", unit="Juta USD", row_labels=["Gas > Ekspor"])
    gas._data.update({("Gas > Ekspor", 2026, "Q3"): 1661.83})
    return build_views([_ExcelSource(table=sbt, filename="skdu.xlsx", sheet="T1"),
                        _ExcelSource(table=gas, filename="npi.xlsx", sheet="Id")])


_READ_REPLY = ('```json\n{"title": "Kegiatan Usaha", "unit": "%", "last_period": "Q3 2026",'
               ' "series": [{"name": "SBT Kegiatan Usaha", "table": "S1", "row": "TOTAL"},'
               ' {"name": "Pertumbuhan PDB", "table": null, "row": null}],'
               ' "labels": [{"text": "12,97", "series": "SBT Kegiatan Usaha", "period": "Q2 2026"}]}\n```')


def test_a_chart_read_names_each_series_row_and_each_label():
    chart = ChartImage("Grafik 1", "Kegiatan Usaha", None, _png())

    read = asyncio.run(read_chart(chart, _views(), _Model(_READ_REPLY)))

    assert read.series == [SeriesRef(name="SBT Kegiatan Usaha", table="S1", row="TOTAL"),
                           SeriesRef(name="Pertumbuhan PDB", table=None, row=None)]
    assert read.labels == [ChartLabel(text="12,97", series="SBT Kegiatan Usaha", period="Q2 2026")]
    assert (read.unit, read.last_period) == ("%", "Q3 2026")


def test_the_prompt_shows_the_tables_rows_never_their_values():
    model = _Model(_READ_REPLY)

    asyncio.run(read_chart(ChartImage("Grafik 1", "", None, _png()), _views(), model))

    prompt = model.messages[0][0].content[0]["text"]
    assert "[S1:" in prompt and "- TOTAL" in prompt and "[S2:" in prompt
    assert "12.97" not in prompt and "1661" not in prompt


def test_an_emf_chart_sends_its_texts_to_copy_labels_from():
    model = _Model(_READ_REPLY)
    chart = ChartImage("Grafik 1", "", None, _png(), emf_boxes=[EmfText(2899, 481, "12,97")])

    asyncio.run(read_chart(chart, _views(), model))

    assert "x=2899 y=481 teks='12,97'" in model.messages[0][0].content[0]["text"]


def test_a_reply_that_is_not_json_is_unreadable():
    with pytest.raises(ChartUnreadable):
        asyncio.run(read_chart(ChartImage("Grafik 1", "", None, _png()), _views(), _Model("Grafik ini menunjukkan ...")))


# --- labels as strict claims --------------------------------------------------------------------

from chart_check import label_results, parse_label


def _read(labels, unit="%", series=None):
    return ChartRead(unit=unit, series=series or [SeriesRef(name="SBT", table="S1", row="TOTAL"),
                                                   SeriesRef(name="Ekspor gas", table="S2", row="Gas > Ekspor"),
                                                   SeriesRef(name="PDB", table=None, row=None)],
                     labels=[ChartLabel(text=t, series=s, period=p) for t, s, p in labels])


def test_a_label_equal_to_its_cell_is_sesuai():
    [result] = label_results(_read([("12,97", "SBT", "Q2 2026")]), _views(), "Grafik 1")

    assert (result.verdict, result.checked_item) == ("Entailed", "chart")
    assert result.context_quote == "Grafik 1: SBT Q2 2026 = 12,97"


def test_a_label_off_its_cell_is_tidak_sesuai():
    [result] = label_results(_read([("13,10", "SBT", "Q2 2026")]), _views(), "Grafik 1")

    assert result.verdict == "Refuted"


def test_a_label_in_the_charts_unit_meets_a_table_in_another():
    [result] = label_results(_read([("1,7", "Ekspor gas", "Tw III 2026")], unit="miliar dolar AS"), _views(), "Grafik 9")

    assert result.verdict == "Entailed"


def test_a_series_in_no_table_is_tidak_cukup_data():
    [result] = label_results(_read([("4,04", "PDB", "Q2 2025")]), _views(), "Grafik 1")

    assert (result.verdict, result.reason_code) == ("Inconclusive", "di_luar_tabel")


def test_a_row_or_period_the_table_lacks_is_tidak_cukup_data():
    bad_row = _read([("8,1", "Sektor", "Q2 2026")], series=[SeriesRef(name="Sektor", table="S1", row="Industri")])
    bad_period = _read([("12,97", "SBT", "Q2 2019")])
    unknown_series = _read([("12,97", "Lainnya", "Q2 2026")])

    for read in (bad_row, bad_period, unknown_series):
        [result] = label_results(read, _views(), "Grafik 1")
        assert result.verdict == "Inconclusive"


@pytest.mark.parametrize("text, value", [("12,97", 12.97), ("-1,5", -1.5), ("(1,2)", -1.2), ("22.6%", 22.6),
                                         ("10,77*", 10.77), ("1.287", 1287.0), ("−0,1", -0.1), ("Tw.II", None)])
def test_printed_forms_parse(text, value):
    assert parse_label(text) == value


# --- the drawing against the data ---------------------------------------------------------------

from chart_check import DrawingJudgement, judge_drawing, series_data


def test_series_data_reads_the_mapped_row_up_to_the_last_period():
    read = ChartRead(last_period="Q2 2026", series=[SeriesRef(name="SBT", table="S1", row="TOTAL"),
                                                    SeriesRef(name="PDB", table=None, row=None)])

    assert series_data(read, _views()) == {"SBT": [("Q3 2025", 11.55), ("Q2 2026", 12.97)]}


def test_no_mapped_series_means_no_call():
    model = _Model("{}")

    assert asyncio.run(judge_drawing(ChartImage("Grafik 1", "", None, _png()), {}, model)) is None
    assert model.messages == []


def test_the_prompt_carries_the_data_and_the_judgement_parses():
    reply = ('{"last_period_same": true, "series": [{"name": "SBT", "fits": "tidak", "off_periods": ["Q2 2026"],'
             ' "note": "turun"}], "verdict": "perlu dicek", "reason": "Q2 2026 menyimpang"}')
    model = _Model(reply)

    judged = asyncio.run(judge_drawing(ChartImage("Grafik 1", "Kegiatan Usaha", None, _png()),
                                       {"SBT": [("Q3 2025", 11.55), ("Q2 2026", 12.97)]}, model))

    assert "Q2 2026 = 12,97" in model.messages[0][0].content[0]["text"]
    assert isinstance(judged, DrawingJudgement)
    assert (judged.verdict, judged.series[0].off_periods, judged.last_period_same) == ("perlu dicek", ["Q2 2026"], True)


# --- cards, and the check runs them -------------------------------------------------------------

import chart_check
from chart_check import check_charts


def _fake_steps(monkeypatch, reads, judged=None, unreadable=()):
    async def read(chart, views, llm):
        if chart.caption in unreadable:
            raise ChartUnreadable("no picture")
        return reads[chart.caption]

    async def judge(chart, data, llm):
        return judged

    monkeypatch.setattr(chart_check, "read_chart", read)
    monkeypatch.setattr(chart_check, "judge_drawing", judge)


def test_a_chart_card_counts_its_labels_and_carries_the_drawing(monkeypatch):
    _fake_steps(monkeypatch, {"Grafik 1": _read([("12,97", "SBT", "Q2 2026"), ("13,00", "SBT", "Q3 2026"),
                                                 ("4,04", "PDB", "Q2 2025")])},
                DrawingJudgement(verdict="perlu dicek", reason="Q3 2026 menyimpang"))

    cards, unread = asyncio.run(check_charts([ChartImage("Grafik 1", "Kegiatan Usaha", None, _png())], [], _views(), object()))

    [card] = cards
    assert (card.caption, card.label_count, card.entailed_count, card.refuted_count, card.inconclusive_count) == (
        "Grafik 1", 3, 1, 1, 1)
    assert card.verdict == "Refuted"                    # a tie goes to the worse
    assert (card.drawing, card.drawing_note) == ("perlu dicek", "Q3 2026 menyimpang")
    assert card.unchecked_series == ["PDB"]                  # in no uploaded table
    assert len(card.issues) == 2 and unread == []


def test_a_chart_with_no_labels_takes_its_status_from_the_drawing(monkeypatch):
    _fake_steps(monkeypatch, {"Grafik 2": _read([])}, DrawingJudgement(verdict="masuk akal"))

    [card], _ = asyncio.run(check_charts([ChartImage("Grafik 2", "", None, _png())], [], _views(), object()))

    assert (card.label_count, card.verdict, card.drawing) == (0, "Entailed", "masuk akal")


def test_one_unreadable_chart_does_not_stop_the_others(monkeypatch):
    _fake_steps(monkeypatch, {"Grafik 2": _read([("12,97", "SBT", "Q2 2026")])}, unreadable={"Grafik 1"})
    charts = [ChartImage("Grafik 1", "", None, _png()), ChartImage("Grafik 2", "", None, _png())]

    cards, unread = asyncio.run(check_charts(charts, [], _views(), object()))

    assert [c.caption for c in cards] == ["Grafik 2"] and unread == ["Grafik 1"]


def test_scanned_pages_are_located_before_they_are_checked(monkeypatch):
    _fake_steps(monkeypatch, {"Grafik 5": _read([("12,97", "SBT", "Q2 2026")])})

    async def locate(page, llm):
        return [ChartImage("Grafik 5", "", page.page_number, _png())]

    monkeypatch.setattr(chart_check, "locate_charts", locate)

    cards, _ = asyncio.run(check_charts([], [ScannedPage(7, _png())], _views(), object()))

    assert [(c.caption, c.page_number) for c in cards] == [("Grafik 5", 7)]


# --- after the measurement (2026-11-24) -----------------------------------------------------------

def test_a_chart_without_a_legend_answers_with_nulls_and_still_parses():
    # SKDU Tw III Grafik 4: one series, no legend; the agent wrote null names and a null title.
    reply = ('{"title": null, "unit": "SB, %", "last_period": "Q3 2026", "series": [{"name": null, "table": "S1",'
             ' "row": "TOTAL"}], "labels": [{"text": "15,56", "series": null, "period": "Q3 2025"}]}')

    read = asyncio.run(read_chart(ChartImage("Grafik 4", "", None, _png()), _views(), _Model(reply)))

    assert read.title == "" and read.series[0].name == "" and read.labels[0].series == ""
    [result] = label_results(read, _views(), "Grafik 4")
    assert result.verdict == "Refuted"                   # 15,56 against TOTAL Q3 2025 = 11,55


def test_the_prompt_asks_for_a_name_when_there_is_no_legend_and_marks_growth():
    model = _Model(_READ_REPLY)

    asyncio.run(read_chart(ChartImage("Grafik 1", "", None, _png()), _views(), model))

    prompt = model.messages[0][0].content[0]["text"]
    assert "tidak punya legenda" in prompt and '"growth"' in prompt


def _level_views():
    m2 = BITableData(title="I.1A Uang Beredar", unit="Miliar Rp", row_labels=["M2"])
    m2._data.update({("M2", 2025, "Sep"): 9_780_000.0, ("M2", 2025, "Aug"): 9_700_000.0,
                     ("M2", 2026, "Aug"): 10_495_400.0, ("M2", 2026, "Sep"): 10_587_929.0})
    return build_views([_ExcelSource(table=m2, filename="seki.xls", sheet="I.1A")])


def test_a_growth_series_is_computed_from_its_levels():
    # UB Sep 2026 Grafik 1 draws M2 growth (% yoy); SEKI I.1A holds M2 levels.
    read = ChartRead(series=[SeriesRef(name="M2", table="S1", row="M2", growth=True)])

    assert series_data(read, _level_views()) == {"M2": [("Aug 2026", 8.2), ("Sep 2026", 8.26)]}


def test_a_growth_label_is_a_yoy_claim_on_two_cells():
    read = ChartRead(unit="%", series=[SeriesRef(name="M2", table="S1", row="M2", growth=True)],
                     labels=[ChartLabel(text="8,3", series="M2", period="Sep 2026"),
                             ChartLabel(text="9,1", series="M2", period="Sep 2026")])

    right, wrong = label_results(read, _level_views(), "Grafik 1")

    assert (right.verdict, wrong.verdict) == ("Entailed", "Refuted")
    assert [(p.year, p.month) for p in right.periods] == [(2025, "Sep"), (2026, "Sep")]
