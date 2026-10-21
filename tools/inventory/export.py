"""Sentence inventory export: every numeric sentence of every saved case, with the system's guess.

usage: python -m tools.inventory.export [--case <case_dir_name> ...] [--out eval/inventory]

For each document in eval/cases/<case>/manifest*.yaml (seeded manifests skipped) this writes
<out>/data/<case>__<doc_id>.json holding:
  - every sentence of the document that contains a number, read WITHOUT any LLM (the PDF text
    layer, or the Word reader), each with the numbers printed in it;
  - whether that sentence reaches the extractor at all (the same narrative filters the
    pipeline applies) — a numeric sentence that is filtered out can never become a claim;
  - the claims the newest saved run made from it (a SUGGESTION for the labeller, never a label);
  - the row labels of the document's workbooks, parsed the way replay parses them (no LLM),
    so the labelling page can offer them as choices.
and <out>/overview.md + overview.json: numeric sentences per document and publication, and
the share the system turned into at least one claim.

Nothing here calls an LLM or the network. A PDF without a usable text layer is listed with
needs_vision=true and no sentences: reading it would need a paid vision call.
"""

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FORMAT_VERSION = 1

# Saved-run folders that are replays or reverted experiments, not real runs of the pipeline.
_SKIP_RUN = re.compile(r"^(rp_|replay|rf_|tablecol|xcheck|__)")

_PAGE_MARKER = re.compile(r"\[== Halaman (\d+) ==\]")
# A printed number: 1.234,5 / 9,2 / -0,3 / 2026. Thousands dots and decimal commas (Indonesian),
# also an English-style 10,432.0 as some editions typeset it.
# "Rp10.448,1" and "US$5" are numbers; the 2 of "M2" and the 1 of "T1" are names, not numbers.
_NUMBER = re.compile(r"(?:(?<=Rp)|(?<=\$)|(?<![\w.,]))[-−]?\d+(?:[.,]\d+)*(?![\w])")
_YEAR = re.compile(r"^(19|20)\d\d$")
_WORD = re.compile(r"[A-Za-zÀ-ÿ]{2,}")


def split_sentences(text: str) -> List[str]:
    """Sentences of `text`, line wraps joined; uses the extractor's own boundary rule."""
    from structured_extractor import _SENT_START_RE
    flat = re.sub(r"\s+", " ", text).strip()
    if not flat:
        return []
    starts = [0] + [m.end() for m in _SENT_START_RE.finditer(flat)]
    out = []
    for a, b in zip(starts, starts[1:] + [len(flat)]):
        s = flat[a:b].strip()
        if s:
            out.append(s)
    return out


_REFERENCE_BEFORE = re.compile(r"(?:Tabel|Grafik|Gambar|Lampiran|Boks|Box|Halaman)\s*$", re.IGNORECASE)


def printed_numbers(sentence: str) -> List[Dict]:
    """Numbers printed in the sentence. year_like / reference mark those that are not data:
    a bare year, or the number of a table, chart or appendix ("Tabel 2", "Lampiran 4")."""
    out = []
    for m in _NUMBER.finditer(sentence):
        tok = m.group(0)
        before, after = sentence[:m.start()], sentence[m.end():]
        footnote = not before.strip() and after.startswith(")")   # "1) Uang Primer adjusted …"
        joined = re.search(r"(?:Tabel|Grafik|Lampiran)\s+\d+\s+dan\s*$", before, re.IGNORECASE)
        out.append({"text": tok, "start": m.start(),
                    "year_like": bool(_YEAR.match(tok.lstrip("-−"))),
                    "reference": bool(_REFERENCE_BEFORE.search(before) or footnote or joined)})
    return out


def data_numbers(sentence: Dict) -> List[str]:
    return sorted(n["text"] for n in sentence["numbers"] if not n["year_like"] and not n["reference"])


def mark_repeats(sentences: List[Dict]) -> None:
    """A sentence with no claim that prints exactly the same data numbers as one that has claims
    is the same statement repeated (summary and body): point at it instead of labelling twice."""
    for s in sentences:
        s["same_numbers_as"] = None
        if s["system_claims"] or not data_numbers(s):
            continue
        twin = next((t for t in sentences if t is not s and t["system_claims"]
                     and data_numbers(t) == data_numbers(s)), None)
        if twin:
            s["same_numbers_as"] = twin["sid"]


def is_numeric_sentence(sentence: str) -> bool:
    """A prose sentence that prints a number other than a bare year or a table/chart number.
    Table rows (mostly numbers, few words) are not sentences and are left out."""
    nums = printed_numbers(sentence)
    if not any(not n["year_like"] and not n["reference"] for n in nums):
        return False
    words = _WORD.findall(sentence)
    return len(words) >= 5 and len(words) >= len(nums)


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", (text or "")).lower()


def _read_document(path: Path) -> Dict:
    """Raw text per page (one 'page' for Word), and the text the extractor is actually shown."""
    from structured_extractor import _filter_narrative
    data = path.read_bytes()
    if path.suffix.lower() == ".docx":
        from word_extraction import read_word_document
        text = read_word_document(data).narrative_text
        return {"pages": [(None, text)], "extractor_text": _filter_narrative(text), "needs_vision": False}
    from pdf_extraction import (MIN_PAGE_CHARS, MIN_USEFUL_CHARS, PAGE_MARKER_RE,
                                _strip_tabular_content, extract_text_from_pdf)
    text = extract_text_from_pdf(data)
    content = PAGE_MARKER_RE.sub("", text).strip()
    parts = _PAGE_MARKER.split(text)
    pages = [(int(parts[i]), parts[i + 1]) for i in range(1, len(parts) - 1, 2)]
    with_text = sum(1 for _, body in pages if len(body.strip()) >= MIN_PAGE_CHARS)
    needs_vision = len(content) < MIN_USEFUL_CHARS or (pages and with_text / max(1, len(pages)) < 0.5)
    return {"pages": pages, "extractor_text": _filter_narrative(_strip_tabular_content(text)),
            "needs_vision": bool(needs_vision)}


def _newest_run(case_dir: Path, doc_id: str) -> Optional[Path]:
    runs = [d for d in case_dir.iterdir() if d.is_dir() and not _SKIP_RUN.match(d.name)]
    for run in sorted(runs, key=lambda d: d.stat().st_mtime, reverse=True):
        if (run / f"{doc_id}.json").exists():
            return run / f"{doc_id}.json"
    return None


def _claim_view(r: Dict) -> Dict:
    return {
        "operation": r.get("operation"),
        "metric_label": r.get("metric_label"),
        "claimed_value": r.get("claimed_value"),
        "claimed_unit": r.get("claimed_unit"),
        "periods": [{k: p.get(k) for k in ("metric_label", "year", "month", "col_label")}
                    for p in r.get("periods") or []],
        "verdict": r.get("verdict"),
        "matched_source": r.get("matched_excel_source"),
        "computed_value": r.get("computed_value"),
        "resolved_via": r.get("resolved_via"),
        "reasoning": r.get("reasoning"),
        "context_quote": r.get("context_quote"),
    }


def attach_claims(sentences: List[Dict], results: List[Dict]) -> List[Dict]:
    """Put each result on the sentence its context_quote came from; return the ones that fit none."""
    squashed = [_squash(s["text"]) for s in sentences]
    unplaced = []
    for r in results:
        q = _squash(r.get("context_quote"))
        if not q:
            unplaced.append(_claim_view(r))
            continue
        hit = next((i for i, s in enumerate(squashed) if q in s or (len(s) > 40 and s in q)), None)
        if hit is None:  # quote spans two of our sentences: take the one holding its start
            head = q[:60]
            hit = next((i for i, s in enumerate(squashed) if head and head in s), None)
        if hit is None:
            unplaced.append(_claim_view(r))
            continue
        # A sentence repeated in the document (summary and body) gets the claim on every copy:
        # the pipeline dedupes the facts, so only one copy was ever extracted.
        for i, s in enumerate(squashed):
            if s == squashed[hit]:
                sentences[i]["system_claims"].append(_claim_view(r))
    return unplaced


def _row_choices(doc, data_root: Path) -> List[Dict]:
    from eval.run_regression import _parsed_sources
    out = []
    for src in _parsed_sources(doc, data_root):
        out.append({"file": src.filename, "sheet": src.sheet,
                    "unit": getattr(src.table, "unit", None),
                    "rows": list(getattr(src.table, "row_labels", []) or [])})
    return out


def export_document(case: str, doc, data_root: Path) -> Dict:
    read = _read_document(data_root / doc.doc)
    in_extractor = _squash(read["extractor_text"])
    candidates = []
    for page, body in read["pages"]:
        # Word paragraphs are real boundaries (a title has no full stop and would otherwise be
        # glued to the first sentence); PDF lines are only wraps.
        blocks = body.split("\n") if page is None else [body]
        for block in blocks:
            for text in split_sentences(block):
                candidates.append({
                    "page": page,
                    "text": text,
                    "has_number": is_numeric_sentence(text),
                    "numbers": printed_numbers(text),
                    "reaches_extractor": _squash(text) in in_extractor,
                    "system_claims": [],
                })
    run_path = _newest_run(data_root / "eval" / "cases" / case, doc.id)
    results = json.loads(run_path.read_text(encoding="utf-8"))["results"] if run_path else []
    unplaced = attach_claims(candidates, results)
    # Every numeric sentence, plus any sentence the system made a claim from (trend claims such
    # as "peningkatan ULN publik" carry no number but still get a verdict).
    sentences = [s for s in candidates if s["has_number"] or s["system_claims"]]
    for i, s in enumerate(sentences, start=1):
        s["sid"] = f"s{i:03d}"
    mark_repeats(sentences)
    return {
        "format_version": FORMAT_VERSION,
        "case": case,
        "doc_id": doc.id,
        "publication": doc.publication,
        "doc": doc.doc,
        "sources": [s.file for s in doc.sources],
        "needs_vision": read["needs_vision"],
        "system_run": str(run_path.relative_to(data_root)).replace("\\", "/") if run_path else None,
        "row_choices": _row_choices(doc, data_root),
        "sentences": sentences,
        "unplaced_claims": unplaced,
    }


def _manifests(cases_root: Path, only: List[str]):
    for case_dir in sorted(p for p in cases_root.iterdir() if p.is_dir()):
        if only and case_dir.name not in only:
            continue
        for m in sorted(case_dir.glob("manifest*.yaml")):
            if "seeded" in m.name:
                continue
            yield case_dir.name, m


def summarize(doc_files: List[Dict]) -> Dict:
    per_pub = defaultdict(lambda: {"documents": 0, "numeric_sentences": 0, "with_claim": 0,
                                   "filtered_before_llm": 0, "needs_vision": 0})
    per_doc = []
    for d in doc_files:
        numeric = [s for s in d["sentences"] if s["has_number"]]
        n = len(numeric)
        with_claim = sum(1 for s in numeric if s["system_claims"] or s.get("same_numbers_as"))
        filtered = sum(1 for s in numeric if not s["reaches_extractor"])
        per_doc.append({"case": d["case"], "doc_id": d["doc_id"], "publication": d["publication"],
                        "numeric_sentences": n, "with_claim": with_claim,
                        "filtered_before_llm": filtered, "needs_vision": d["needs_vision"],
                        "has_system_run": d["system_run"] is not None,
                        "unplaced_claims": len(d["unplaced_claims"])})
        p = per_pub[d["publication"]]
        p["documents"] += 1
        p["numeric_sentences"] += n
        p["with_claim"] += with_claim
        p["filtered_before_llm"] += filtered
        p["needs_vision"] += int(d["needs_vision"])
    return {"per_publication": dict(per_pub), "per_document": per_doc}


def _pct(a: int, b: int) -> str:
    return f"{100 * a / b:.0f} %" if b else "—"


def overview_markdown(summary: Dict) -> str:
    lines = ["# Sentence inventory: overview", "",
             "Numeric sentences = prose sentences that print a number other than a bare year. "
             "'With claim' = the newest saved run made at least one claim from it. "
             "'Filtered' = the narrative filter removes it before the LLM ever sees it.", "",
             "| Publication | Documents | Numeric sentences | With claim | Filtered before LLM | Needs vision |",
             "|---|---:|---:|---:|---:|---:|"]
    for pub, p in sorted(summary["per_publication"].items()):
        lines.append(f"| {pub} | {p['documents']} | {p['numeric_sentences']} | "
                     f"{p['with_claim']} ({_pct(p['with_claim'], p['numeric_sentences'])}) | "
                     f"{p['filtered_before_llm']} | {p['needs_vision']} |")
    lines += ["", "| Case | Document | Sentences | With claim | Filtered | Unplaced claims | Note |",
              "|---|---|---:|---:|---:|---:|---|"]
    for d in summary["per_document"]:
        note = "needs vision (paid) — no sentences read" if d["needs_vision"] else (
            "" if d["has_system_run"] else "no saved run — no system guess")
        lines.append(f"| {d['case']} | {d['doc_id']} | {d['numeric_sentences']} | "
                     f"{d['with_claim']} ({_pct(d['with_claim'], d['numeric_sentences'])}) | "
                     f"{d['filtered_before_llm']} | {d['unplaced_claims']} | {note} |")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    import logging
    logging.basicConfig(level=logging.ERROR)
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", action="append", default=[])
    ap.add_argument("--out", default=str(ROOT / "eval" / "inventory"))
    args = ap.parse_args(argv)
    from eval.regression import load_manifest

    out = Path(args.out)
    (out / "data").mkdir(parents=True, exist_ok=True)
    exported = []
    for case, manifest in _manifests(ROOT / "eval" / "cases", args.case):
        for doc in load_manifest([manifest]):
            try:
                d = export_document(case, doc, ROOT)
            except FileNotFoundError as exc:
                print(f"SKIP {case}/{doc.id}: {exc}")
                continue
            (out / "data" / f"{case}__{doc.id}.json").write_text(
                json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
            exported.append(d)
            print(f"{case}/{doc.id}: {len(d['sentences'])} numeric sentences"
                  f"{' (needs vision)' if d['needs_vision'] else ''}")
    summary = summarize(exported)
    (out / "overview.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "overview.md").write_text(overview_markdown(summary), encoding="utf-8")
    print(f"\n{len(exported)} documents -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
