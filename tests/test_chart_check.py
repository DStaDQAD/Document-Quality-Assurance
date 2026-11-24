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


# --- final review (2026-11-24) --------------------------------------------------------------------

@pytest.mark.parametrize("written, growth", [("false", False), ("no", False), (None, False), ("true", True), (True, True)])
def test_growth_written_as_text_is_read_as_its_meaning(written, growth):
    # bool("false") is True: a levels series would have been judged as the yoy of its levels.
    assert SeriesRef.model_validate({"name": "M2", "growth": written}).growth is growth


def _annual_views():
    t = BITableData(title="Inflasi", unit="%", row_labels=["Inflasi"])
    t._data.update({("Inflasi", 2024, "Dec"): 1.57, ("Inflasi", 2025, "Dec"): 2.72, ("Inflasi", 2026, "Jun"): 2.1})
    t.annual.update({("Inflasi", 2023): 2.61, ("Inflasi", 2024): 1.57, ("Inflasi", 2025): 2.72})
    return build_views([_ExcelSource(table=t, filename="skdu.xlsx", sheet="T7")])


def test_a_chart_ending_on_a_year_reads_the_annual_values_up_to_that_year():
    # SKDU Grafik 7 "Realisasi Inflasi Tahun 2018 … 2025": its last period is a year, not a month.
    read = ChartRead(last_period="Tahun 2025", series=[SeriesRef(name="Realisasi", table="S1", row="Inflasi")])

    assert series_data(read, _annual_views()) == {
        "Realisasi": [("Tahun 2023", 2.61), ("Tahun 2024", 1.57), ("Tahun 2025", 2.72)]}


def test_a_card_names_the_table_row_each_series_was_compared_with(monkeypatch):
    # NPI Grafik 5 draws c.i.f. imports; the workbook only has fob: the card says which row was used.
    read = ChartRead(series=[SeriesRef(name="y.o.y", table="S1", row="TOTAL"),
                             SeriesRef(name="M2", table="S1", row="Pertanian", growth=True),
                             SeriesRef(name="PDB", table=None, row=None)])
    _fake_steps(monkeypatch, {"Grafik 5": read})

    [card], _ = asyncio.run(check_charts([ChartImage("Grafik 5", "", None, _png())], [], _views(), object()))

    assert card.series_rows == ["y.o.y: TOTAL (skdu.xlsx, sheet T1)",
                                "M2: Pertanian (skdu.xlsx, sheet T1), pertumbuhan yoy dihitung dari level"]


# --- small findings of the final review (2026-11-24) -----------------------------------------------

def test_a_label_finds_its_series_whatever_the_spacing_and_case():
    read = _read([("12,97", "sbt ", "Q2 2026")])

    [result] = label_results(read, _views(), "Grafik 1")

    assert result.verdict == "Entailed"


def test_the_caption_number_does_not_decide_a_labels_decimals():
    # "Grafik 2 … = 2,0": the "2" of the caption is not how the label is printed.
    t = BITableData(title="T", unit="%", row_labels=["TOTAL"])
    t._data.update({("TOTAL", 2026, "Q2"): 2.05})
    views = build_views([_ExcelSource(table=t, filename="skdu.xlsx", sheet="T1")])
    read = ChartRead(unit="%", series=[SeriesRef(name="SBT", table="S1", row="TOTAL")],
                     labels=[ChartLabel(text="2,0", series="SBT", period="Q2 2026")])

    [result] = label_results(read, views, "Grafik 2")

    assert result.verdict == "Refuted"                  # 2,05 shows as 2,1 at one decimal; a 0,05 tolerance let it by
    assert "1 desimal" in result.reasoning


def test_a_label_the_model_sends_as_a_number_keeps_its_decimals():
    label = ChartLabel.model_validate({"text": 1.287, "series": "SBT", "period": "Q2 2026"})

    assert label.text == "1,287" and parse_label(label.text) == 1.287


def test_a_table_code_sent_as_a_number_is_read_as_its_code():
    assert SeriesRef.model_validate({"name": "SBT", "table": 1, "row": "TOTAL"}).table == "S1"
    assert SeriesRef.model_validate({"name": "SBT", "table": "s2", "row": "TOTAL"}).table == "s2"


def test_at_most_four_charts_are_read_at_once_whatever_the_model(monkeypatch):
    running = {"now": 0, "most": 0}

    async def card(chart, views, llm):
        running["now"] += 1
        running["most"] = max(running["most"], running["now"])
        await asyncio.sleep(0.01)
        running["now"] -= 1
        return None

    monkeypatch.setattr(chart_check, "_card", card)
    charts = [ChartImage(f"Grafik {i}", "", None, _png()) for i in range(10)]

    asyncio.run(check_charts(charts, [], _views(), object()))

    assert running["most"] == 4


# --- a label that does not fit, read once more (2026-11-24) -----------------------------------------

def _two_series_read(labels):
    return ChartRead(unit="%", series=[SeriesRef(name="SBT", table="S1", row="TOTAL"),
                                       SeriesRef(name="Tani", table="S1", row="Pertanian")],
                     labels=[ChartLabel(text=t, series=s, period=p) for t, s, p in labels])


def _reread_steps(monkeypatch, read, reread=None):
    calls = []

    async def read_chart(chart, views, llm):
        return read

    async def judge(chart, data, llm):
        return None

    async def again(chart, read_, labels, llm):
        calls.append([l.text for l in labels])
        if isinstance(reread, Exception):
            raise reread
        return reread or []

    monkeypatch.setattr(chart_check, "read_chart", read_chart)
    monkeypatch.setattr(chart_check, "judge_drawing", judge)
    monkeypatch.setattr(chart_check, "reread_labels", again)
    return calls


def test_a_label_on_the_wrong_series_is_read_again_and_sesuai_when_the_second_reading_fits(monkeypatch):
    # SK Sep 2026 Grafik 11: the model put IEKLK's pink 123,1 on IEKU's grey line.
    calls = _reread_steps(monkeypatch, _two_series_read([("8,1", "SBT", "Q2 2026")]),
                          [ChartLabel(text="8,1", series="Tani", period="Q2 2026")])

    [card], _ = asyncio.run(check_charts([ChartImage("Grafik 11", "", None, _png())], [], _views(), object()))

    assert calls == [["8,1"]]
    assert (card.entailed_count, card.refuted_count) == (1, 0) and card.verdict == "Entailed"


def test_a_label_one_period_off_is_read_again(monkeypatch):
    # SPE Agu 2026 Grafik 10: 11,7 belongs to Aug 2025; the model read Jul 2025.
    calls = _reread_steps(monkeypatch, _two_series_read([("11,46", "SBT", "Q2 2026")]),
                          [ChartLabel(text="11,46", series="SBT", period="Q3 2026")])

    [card], _ = asyncio.run(check_charts([ChartImage("Grafik 10", "", None, _png())], [], _views(), object()))

    assert calls == [["11,46"]] and card.verdict == "Entailed"


def test_a_second_reading_that_still_does_not_fit_stays_tidak_sesuai(monkeypatch):
    _reread_steps(monkeypatch, _two_series_read([("8,1", "SBT", "Q2 2026")]),
                  [ChartLabel(text="8,1", series="SBT", period="Q2 2026")])

    [card], _ = asyncio.run(check_charts([ChartImage("Grafik 11", "", None, _png())], [], _views(), object()))

    assert card.refuted_count == 1 and card.verdict == "Refuted"
    assert "dicek ulang" in card.issues[0].reasoning.lower()


def test_a_label_whose_number_is_nowhere_in_the_chart_is_not_read_again(monkeypatch):
    calls = _reread_steps(monkeypatch, _two_series_read([("99,9", "SBT", "Q2 2026")]))

    [card], _ = asyncio.run(check_charts([ChartImage("Grafik 1", "", None, _png())], [], _views(), object()))

    assert calls == [] and card.refuted_count == 1


def test_a_second_reading_that_fails_keeps_the_first_verdict(monkeypatch):
    _reread_steps(monkeypatch, _two_series_read([("8,1", "SBT", "Q2 2026")]), ChartUnreadable("timeout"))

    [card], unread = asyncio.run(check_charts([ChartImage("Grafik 11", "", None, _png())], [], _views(), object()))

    assert card.refuted_count == 1 and unread == []


def test_the_second_reading_asks_only_about_the_labels_and_their_colours():
    from chart_check import reread_labels
    model = _Model('{"labels": [{"text": "8,1", "series": "Tani", "period": "Q2 2026"}]}')
    read = _two_series_read([("8,1", "SBT", "Q2 2026")])

    labels = asyncio.run(reread_labels(ChartImage("Grafik 11", "IEK", None, _png()), read, read.labels, model))

    assert [(l.text, l.series, l.period) for l in labels] == [("8,1", "Tani", "Q2 2026")]
    prompt = model.messages[0][0].content[0]["text"]
    assert "8,1" in prompt and "warna" in prompt.lower() and "SBT" in prompt and "Tani" in prompt


def test_the_prompt_prefers_a_growth_row_the_tables_already_have():
    model = _Model(_READ_REPLY)

    asyncio.run(read_chart(ChartImage("Grafik 1", "", None, _png()), _views(), model))

    prompt = model.messages[0][0].content[0]["text"]
    assert "TAHUNAN (YOY)" in prompt and "jangan hitung" in prompt.lower()


def test_a_series_on_its_own_axis_carries_its_own_unit():
    # SHPR Tw II-2026 Grafik 1: IHPR (index, left axis) with growth in % on the right axis.
    t = BITableData(title="IHPR", unit="Indeks (2018=100)", row_labels=["TOTAL"])
    t._data.update({("TOTAL", 2025, "Q2"): 110.13})
    views = build_views([_ExcelSource(table=t, filename="shpr.xlsx", sheet="TABEL 1")])
    read = ChartRead.model_validate({"unit": "%", "series": [{"name": "IHPR (lhs)", "table": "S1", "row": "TOTAL",
                                                               "unit": "Indeks"}],
                                     "labels": [{"text": "110,13", "series": "IHPR (lhs)", "period": "Q2 2025"}]})

    [result] = label_results(read, views, "Grafik 1")

    assert result.verdict == "Entailed"


def test_a_label_that_is_not_a_number_is_left_out():
    # SPE Agu 2026 Gambar 1, a map: the model listed the city names as labels.
    read = _read([("MEDAN", "SBT", "Q2 2026"), ("12,97", "SBT", "Q2 2026")])

    assert [r.verdict for r in label_results(read, _views(), "Gambar 1")] == ["Entailed"]


def test_the_prompt_asks_for_a_series_unit_on_a_second_axis():
    model = _Model(_READ_REPLY)

    asyncio.run(read_chart(ChartImage("Grafik 1", "", None, _png()), _views(), model))

    assert "sumbu kanan" in model.messages[0][0].content[0]["text"]
