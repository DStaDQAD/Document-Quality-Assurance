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
