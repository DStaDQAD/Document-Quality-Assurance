"""Cek Terjemahan on UB Jun-Agu 2026: the six real ID/EN pairs, and the English reports with errors
planted from eval/translation/ub_plants.yaml. The documents are local (sample_data/sharepoint is
gitignored); the LLM is the configured one (get_llm), so a full run is paid.

    python eval/run_translation_eval.py --plants-only   # check every plant applies; no LLM
    python eval/run_translation_eval.py                 # the paid run
"""
import argparse
import asyncio
import html
import io
import sys
import zipfile
from pathlib import Path
from typing import List, Tuple

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from llm_provider import get_llm  # noqa: E402
from translation_check import check_translation  # noqa: E402
from word_extraction import read_word_document  # noqa: E402

UB = ROOT / "sample_data" / "sharepoint" / "ub"
EDITIONS = {"jun26": "Juni", "jul26": "Juli", "agu26": "Agustus"}


def documents(edition: str) -> List[Tuple[str, Path, Path]]:
    month = EDITIONS[edition]
    base = UB / edition
    return [("siaran_pers", base / f"Narasi Uang Beredar Posisi {month} 2026.docx",
             base / "EN" / f"Narasi Uang Beredar Posisi {month} 2026_EN.docx"),
            ("laporan", base / f"DSta_PS01_Analisis Uang Beredar Posisi {month} 2026.docx",
             base / "EN" / f"Analisis Uang Beredar {month} 2026_English.docx")]


def plant(docx: bytes, edits: List[Tuple[str, str]]) -> bytes:
    """The document with each `old` text replaced by `new` inside word/document.xml. Each `old`
    must occur exactly once, within one text run; anything else raises with the text."""
    src = zipfile.ZipFile(io.BytesIO(docx))
    xml = src.read("word/document.xml").decode("utf-8")
    for old, new in edits:
        escaped = html.escape(old, quote=False)
        if xml.count(escaped) != 1:
            raise ValueError(f"plant does not apply once ({xml.count(escaped)}x): {old!r}")
        xml = xml.replace(escaped, html.escape(new, quote=False))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            data = xml.encode("utf-8") if item.filename == "word/document.xml" else src.read(item.filename)
            dst.writestr(item, data)
    return out.getvalue()


def text_of(docx: bytes) -> str:
    return read_word_document(docx).narrative_text


async def run(plants: list) -> None:
    llm = get_llm(temperature=0.0)
    print("== Dokumen asli")
    for edition in EDITIONS:
        for document, id_path, en_path in documents(edition):
            report = await check_translation(text_of(id_path.read_bytes()), text_of(en_path.read_bytes()), llm,
                                             document=document, id_filename=id_path.name, en_filename=en_path.name,
                                             id_is_word=True, en_is_word=True)
            print(f"{edition} {document}: angka {report.n_number} · makna {report.n_meaning} · "
                  f"tidak terperiksa {report.n_unchecked}")
            for pair in report.pairs:
                for f in pair.findings:
                    print(f"   [{f.kind}] pair {pair.index}: {f.explanation}")
    print("\n== Kesalahan tanaman")
    caught = 0
    for edition in EDITIONS:
        mine = [p for p in plants if p["edition"] == edition]
        if not mine:
            continue
        document, id_path, en_path = documents(edition)[1]
        planted = plant(en_path.read_bytes(), [(p["old"], p["new"]) for p in mine])
        report = await check_translation(text_of(id_path.read_bytes()), text_of(planted), llm, document=document,
                                         id_filename=id_path.name, en_filename=en_path.name + " (planted)",
                                         id_is_word=True, en_is_word=True)
        for p in mine:
            pairs = [pair for pair in report.pairs if pair.en_text and p["new"] in pair.en_text]
            kinds = [f.kind for pair in pairs for f in pair.findings]
            ok = p["expect"] in kinds
            caught += ok
            print(f"{'TERTANGKAP' if ok else 'LOLOS':10} {edition} {p['kind']:6} (harap {p['expect']}) "
                  f"temuan: {kinds or '-'} | {p['new'][:70]}")
            for pair in pairs:
                for f in pair.findings:
                    print(f"      [{f.kind}] {f.explanation}")
    print(f"\n{caught}/{len(plants)} kesalahan tanaman tertangkap")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plants-only", action="store_true", help="only check that every plant applies")
    args = parser.parse_args()
    plants = yaml.safe_load((ROOT / "eval" / "translation" / "ub_plants.yaml").read_text(encoding="utf-8"))["plants"]
    if args.plants_only:
        for edition in EDITIONS:
            mine = [(p["old"], p["new"]) for p in plants if p["edition"] == edition]
            if mine:
                planted = plant(documents(edition)[1][2].read_bytes(), mine)
                missing = [new for _, new in mine if new not in text_of(planted)]
                print(edition, f"{len(mine)} plants applied", "OK" if not missing else f"NOT IN TEXT: {missing}")
        return
    asyncio.run(run(plants))


if __name__ == "__main__":
    main()
