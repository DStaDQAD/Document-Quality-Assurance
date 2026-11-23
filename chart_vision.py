"""One model call about a chart picture: the Copilot Studio agent through Direct Line's upload
endpoint, any other chat model through a LangChain multimodal message."""
import base64
import io
from typing import Any

from langchain_core.messages import HumanMessage
from PIL import Image

from copilot_chat import CopilotStudioChat

NO_IMAGE = "TIDAK ADA GAMBAR"
_MAX_PX = 1600


class ChartUnreadable(RuntimeError):
    """The model gave nothing usable for a chart: no reply, no picture seen, or no JSON."""


def _fitted(png: bytes, mime: str) -> tuple:
    """The picture with its long side at most _MAX_PX (re-encoded as PNG when scaled)."""
    image = Image.open(io.BytesIO(png))
    if max(image.size) <= _MAX_PX:
        return png, mime
    ratio = _MAX_PX / max(image.size)
    image = image.convert("RGB").resize((round(image.width * ratio), round(image.height * ratio)), Image.LANCZOS)
    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    return out.getvalue(), "image/png"


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content or [])


async def ask_chart(llm: Any, prompt: str, png: bytes, mime: str = "image/png") -> str:
    """The model's answer to `prompt` about the chart picture. ChartUnreadable when the answer is
    empty or says the picture was not seen (the prompt asks for NO_IMAGE then)."""
    png, mime = _fitted(png, mime)
    if isinstance(llm, CopilotStudioChat):
        reply = await llm.ainvoke_with_image(prompt, png, mime)
    else:
        url = f"data:{mime};base64," + base64.b64encode(png).decode("ascii")
        message = HumanMessage(content=[{"type": "text", "text": prompt},
                                        {"type": "image_url", "image_url": {"url": url}}])
        reply = _text((await llm.ainvoke([message])).content)
    if not reply.strip() or NO_IMAGE in reply.upper():
        raise ChartUnreadable(reply.strip()[:200] or "no reply")
    return reply
