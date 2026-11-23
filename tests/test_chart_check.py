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
