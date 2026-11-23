"""Compare emf_render with Windows' own EMF rendering for every chart of the given Word reports.

Windows only (PIL draws EMF through GDI there). For each chart EMF after a "Grafik N" caption it
writes <out>/<report>_<caption>.png with Windows on the left and ours on the right, and prints the
mean absolute pixel difference (0-255) after both are scaled to the same size.

    .venv/Scripts/python.exe tools/emf_render_compare.py <out_dir> <report.docx> [...]
"""
import io
import os
import re
import sys
import zipfile
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PIL import Image, ImageChops, ImageStat  # noqa: E402

import word_extraction as we  # noqa: E402
from emf_render import render_emf  # noqa: E402


def chart_emfs(docx: str):
    z = zipfile.ZipFile(docx)
    body = ET.fromstring(z.read("word/document.xml")).find(we._W + "body")
    paragraphs = []
    we._walk(body, paragraphs)
    rels = we._relationships(z)
    pending = None
    for p in paragraphs:
        match = re.match(r"^\s*(Grafik\s*\d+)", p.text)
        if match:
            pending = match.group(1)
        for rid in p.pictures:
            if pending:
                path = rels.get(rid, "")
                if path.lower().endswith(".emf"):
                    yield pending, z.read(path)
                pending = None


def main(out_dir: str, reports):
    os.makedirs(out_dir, exist_ok=True)
    for report in reports:
        name = os.path.splitext(os.path.basename(report))[0][:30]
        for caption, data in chart_emfs(report):
            ours_png = render_emf(data, max_px=1600)
            if ours_png is None:
                print(f"{name} {caption}: NOT RENDERED")
                continue
            ours = Image.open(io.BytesIO(ours_png)).convert("RGB")
            windows = Image.open(io.BytesIO(data))
            windows.load()
            windows = windows.convert("RGB").resize(ours.size)
            diff = ImageStat.Stat(ImageChops.difference(windows, ours)).mean
            side = Image.new("RGB", (ours.width * 2 + 10, ours.height), (255, 0, 255))
            side.paste(windows, (0, 0))
            side.paste(ours, (ours.width + 10, 0))
            side.save(os.path.join(out_dir, f"{name}_{caption.replace(' ', '')}.png"))
            print(f"{name} {caption}: mean abs diff {sum(diff) / 3:.1f}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2:])
