"""One model call about a chart picture: the Copilot Studio agent through Direct Line's upload
endpoint, any other chat model through a LangChain multimodal message."""
import base64
import io
import json
from typing import Any, List

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


# --- scanned PDF pages -----------------------------------------------------------------------------

_MIN_SIDE = 0.05          # a box narrower or lower than this fraction of the page is not a chart

LOCATE_PROMPT = f"""Terlampir satu halaman laporan statistik Bank Indonesia (hasil pindai). Temukan SETIAP grafik di
halaman ini (bukan tabel angka, bukan foto atau logo). Untuk setiap grafik, salin keterangannya bila tercetak
("Grafik 3. Ekspor Nonmigas"), dan beri kotaknya sebagai pecahan lebar dan tinggi halaman, 0 sampai 1:
[kiri, atas, kanan, bawah], mencakup judul sumbu, label, dan legenda grafik itu.
Jika kamu tidak menerima atau tidak bisa melihat gambar, jawab persis '{NO_IMAGE}'.
Jawab HANYA JSON: {{"grafik": [{{"keterangan": "...", "kotak": [0.1, 0.2, 0.9, 0.6]}}]}}; tanpa grafik: {{"grafik": []}}"""


async def locate_charts(page: "ScannedPage", llm: Any) -> List["ChartImage"]:
    """The charts the model finds on a scanned page, each cropped out of it."""
    from chart_images import ChartImage, _split_caption
    from copilot_chat import extract_json

    reply = await ask_chart(llm, LOCATE_PROMPT, page.png)
    try:
        found = json.loads(extract_json(reply)).get("grafik") or []
    except (ValueError, AttributeError):
        raise ChartUnreadable(f"halaman {page.page_number}: jawaban bukan JSON")
    image = Image.open(io.BytesIO(page.png)).convert("RGB")
    charts: List[ChartImage] = []
    for item in found:
        try:
            left, top, right, bottom = (min(1.0, max(0.0, float(v))) for v in item.get("kotak", []))
        except (TypeError, ValueError):
            continue
        if right - left < _MIN_SIDE or bottom - top < _MIN_SIDE:
            continue
        box = (round(left * image.width), round(top * image.height), round(right * image.width), round(bottom * image.height))
        out = io.BytesIO()
        image.crop(box).save(out, format="PNG", optimize=True)
        label = (item.get("keterangan") or "").strip()
        caption, title = _split_caption(label) if label else (f"Grafik hal. {page.page_number} ({len(charts) + 1})", "")
        charts.append(ChartImage(caption, title, page.page_number, out.getvalue()))
    return charts
