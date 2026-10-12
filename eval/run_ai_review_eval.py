"""Tinjauan AI eval — does the AI second look turn leftover verdicts into right ones?

Local only: reads the BI workbooks under tests/fixtures/publications/ (never committed) and the
snapshot, answers and hand labels under eval/cases/ai_review/ (gitignored).

    python -m eval.run_ai_review_eval snapshot --from <dir holding <run>/e2e_<pub>.json>
    python -m eval.run_ai_review_eval ask --tag t1024a --thinking 1024
    python -m eval.run_ai_review_eval judge --tag t1024a [--no-guard] [--new-refuted verdict]
    python -m eval.run_ai_review_eval stability --tag t1024a --other t1024b

`ask` is the only command that calls the model; `judge` replays saved answers, so the guard and
the new-Refuted policy can be compared for free. The spec's gate is in
docs/superpowers/specs/2026-10-12-ai-review-design.md.
"""

import argparse
import asyncio
import hashlib
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Dict, List, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CASES = ROOT / "eval" / "cases" / "ai_review"
FIXTURES = ROOT / "tests" / "fixtures" / "publications"
# Which run directory of the 2026-10-12 spike holds each publication's latest e2e result.
SPIKE_RUNS = {
    "npi": "p_new", "pii": "run3", "pmi": "run3", "skdu": "reg_new2", "spe": "reg_new2",
    "sbank": "reg_new2", "shpr": "reg_new2", "uang-beredar": "reg_new", "uang-primer-m0": "reg_new",
    "cadangan-devisa": "reg_new", "sk": "reg_new", "sulni": "reg_new",
}
USD_TO_RP = 16_500
PRICE_IN, PRICE_OUT = 0.30, 2.50          # Gemini 2.5 Flash, USD per 1M tokens (thinking = output)
EXPECTED = {"to_entailed": "Entailed", "to_inconclusive": "Inconclusive",
            "to_refuted": "Refuted", "flag": "Refuted"}


def claim_id(pub: str, r: dict) -> str:
    key = f"{r.get('context_quote')}|{r.get('operation')}|{r.get('claimed_value')}|{r.get('metric_label')}"
    return f"{pub}:{hashlib.sha1(key.encode('utf-8')).hexdigest()[:10]}"


def score(rows: List[dict], labels: Dict[str, dict]) -> dict:
    out: dict = {}
    for kind, expected in EXPECTED.items():
        mine = [r for r in rows if r["outcome"] == kind]
        labelled = [r for r in mine if r["id"] in labels]
        correct = sum(labels[r["id"]]["truth"] == expected for r in labelled)
        out[kind] = {"n": len(mine), "labelled": len(labelled), "correct": correct,
                     "precision": correct / len(labelled) if labelled else None}
    out["lost_findings"] = sum(
        1 for r in rows
        if r["before"] == "Refuted" and r["outcome"] in ("to_entailed", "to_inconclusive")
        and labels.get(r["id"], {}).get("truth") == "Refuted"
    )
    out["unlabelled"] = sorted({r["id"] for r in rows if r["outcome"] != "unchanged" and r["id"] not in labels})
    return out


def stability(rows_a: List[dict], rows_b: List[dict]) -> Tuple[int, int, int, int]:
    """(same outcome, compared, same among changed-in-either, changed-in-either)."""
    b = {r["id"]: r["outcome"] for r in rows_b}
    pairs = [(r["outcome"], b[r["id"]]) for r in rows_a if r["id"] in b]
    changed = [(x, y) for x, y in pairs if x != "unchanged" or y != "unchanged"]
    return (sum(x == y for x, y in pairs), len(pairs),
            sum(x == y for x, y in changed), len(changed))


def cost_rupiah(tokens_in: int, tokens_out: int) -> float:
    return (tokens_in * PRICE_IN + tokens_out * PRICE_OUT) / 1e6 * USD_TO_RP


# --- the parts below need the workbooks and (for `ask`) an API key ---------------------------

def _runs() -> Dict[str, dict]:
    return {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in sorted((CASES / "runs").glob("*.json"))}


def _sources(run: dict):
    from paired_verifier import _ExcelSource
    from publication_parsers import parse_for_publication

    out = []
    for name, sheet in zip(run["excel_filenames"], run["excel_sheets"]):
        hits = sorted(FIXTURES.rglob(name))
        if not hits:
            raise SystemExit(f"Workbook {name!r} tidak ada di {FIXTURES}")
        owner = hits[0].relative_to(FIXTURES).parts[0]
        table = parse_for_publication(owner, hits[0].read_bytes(), sheet)
        if table is not None:
            out.append(_ExcelSource(table=table, filename=name, sheet=sheet))
    return out


def _results(run: dict):
    from schemas import FactVerificationResult
    return [FactVerificationResult(**r) for r in run["results"]]


def cmd_snapshot(args) -> None:
    (CASES / "runs").mkdir(parents=True, exist_ok=True)
    for pub, sub in SPIKE_RUNS.items():
        src = Path(args.src) / sub / f"e2e_{pub}.json"
        if src.exists():
            shutil.copy(src, CASES / "runs" / f"{pub}.json")
            print(f"  {pub}: {src}")
        else:
            print(f"  {pub}: TIDAK ADA ({src})")


def cmd_ask(args) -> None:
    from langchain_core.callbacks import BaseCallbackHandler

    from ai_review import ask_reviews, review_candidates, reviewable_sources
    from llm_provider import get_review_llm

    class _Usage(BaseCallbackHandler):
        def __init__(self):
            self.inp = self.out = 0

        def on_llm_end(self, response, **kw):
            for gens in response.generations:
                for g in gens:
                    um = getattr(getattr(g, "message", None), "usage_metadata", None) or {}
                    self.inp += um.get("input_tokens", 0)
                    self.out += um.get("output_tokens", 0)

    out = {"thinking": args.thinking, "pubs": {}}
    for pub, run in _runs().items():
        usage = _Usage()
        llm = get_review_llm(thinking_budget=args.thinking, callbacks=[usage])
        results = _results(run)
        codes = reviewable_sources(_sources(run))
        indices = review_candidates(results)
        t0 = time.time()
        answers = asyncio.run(ask_reviews(results, indices, codes, llm)) if codes and indices else {}
        seconds = round(time.time() - t0, 1)
        out["pubs"][pub] = {
            "seconds": seconds, "tokens_in": usage.inp, "tokens_out": usage.out,
            "answers": {claim_id(pub, run["results"][i]): a.model_dump() for i, a in answers.items()},
        }
        print(f"  {pub}: {len(indices)} klaim, {len(answers)} dijawab, {seconds}s, "
              f"tok {usage.inp}/{usage.out}, Rp{cost_rupiah(usage.inp, usage.out):.0f}")
    (CASES / f"answers_{args.tag}.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")


def _judged_rows(tag: str, use_guard: bool, new_refuted: str) -> List[dict]:
    from ai_review import ClaimReview, judge_review, review_candidates, reviewable_sources

    saved = json.loads((CASES / f"answers_{tag}.json").read_text(encoding="utf-8"))
    rows = []
    for pub, run in _runs().items():
        answers = saved["pubs"].get(pub, {}).get("answers", {})
        results = _results(run)
        codes = reviewable_sources(_sources(run))
        for i in review_candidates(results):
            cid = claim_id(pub, run["results"][i])
            if cid not in answers:
                continue
            final, outcome = judge_review(results[i], ClaimReview(**answers[cid]), codes,
                                          new_refuted=new_refuted, use_guard=use_guard)
            rows.append({"id": cid, "pub": pub, "before": results[i].verdict, "after": final.verdict,
                         "outcome": outcome, "quote": results[i].context_quote,
                         "note": final.reasoning.split(" | ")[-1]})
    return rows


def cmd_judge(args) -> None:
    labels_path = CASES / "labels.json"
    labels = json.loads(labels_path.read_text(encoding="utf-8")) if labels_path.exists() else {}
    rows = _judged_rows(args.tag, not args.no_guard, args.new_refuted)
    s = score(rows, labels)
    saved = json.loads((CASES / f"answers_{args.tag}.json").read_text(encoding="utf-8"))
    t_in = sum(p["tokens_in"] for p in saved["pubs"].values())
    t_out = sum(p["tokens_out"] for p in saved["pubs"].values())
    secs = sorted(p["seconds"] for p in saved["pubs"].values())
    variant = f"{args.tag}{'_noguard' if args.no_guard else ''}{'_verdict' if args.new_refuted == 'verdict' else ''}"
    lines = [f"# {variant}", "", "| Hasil | n | dilabeli | benar | presisi |", "|---|---|---|---|---|"]
    for kind in EXPECTED:
        k = s[kind]
        p = "-" if k["precision"] is None else f"{k['precision']:.0%}"
        lines.append(f"| {kind} | {k['n']} | {k['labelled']} | {k['correct']} | {p} |")
    lines += ["", f"Temuan Tidak Sesuai benar yang hilang: {s['lost_findings']}",
              f"Biaya total: Rp{cost_rupiah(t_in, t_out):.0f} untuk {len(saved['pubs'])} publikasi "
              f"(Rp{cost_rupiah(t_in, t_out) / max(len(saved['pubs']), 1):.0f}/pengecekan)",
              f"Waktu per publikasi: median {secs[len(secs) // 2] if secs else 0}s, maks {secs[-1] if secs else 0}s",
              f"Belum dilabeli: {len(s['unlabelled'])}"]
    (CASES / f"report_{variant}.md").write_text("\n".join(lines), encoding="utf-8")
    todo = [r for r in rows if r["id"] in set(s["unlabelled"])]
    (CASES / f"to_label_{variant}.md").write_text("\n\n".join(
        f"## {r['id']} ({r['before']} → {r['outcome']})\n> {r['quote']}\n\n{r['note']}" for r in todo),
        encoding="utf-8")
    print("\n".join(lines))


def cmd_stability(args) -> None:
    a = _judged_rows(args.tag, True, "note")
    b = _judged_rows(args.other, True, "note")
    same, n, same_changed, n_changed = stability(a, b)
    print(f"Sama: {same}/{n} = {same / max(n, 1):.1%}; di antara yang berubah: "
          f"{same_changed}/{n_changed} = {same_changed / max(n_changed, 1):.1%}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("snapshot")
    p.add_argument("--from", dest="src", required=True)
    p = sub.add_parser("ask")
    p.add_argument("--tag", required=True)
    p.add_argument("--thinking", type=int, default=1024)
    p = sub.add_parser("judge")
    p.add_argument("--tag", required=True)
    p.add_argument("--no-guard", action="store_true")
    p.add_argument("--new-refuted", choices=["note", "verdict"], default="note")
    p = sub.add_parser("stability")
    p.add_argument("--tag", required=True)
    p.add_argument("--other", required=True)
    args = ap.parse_args()
    {"snapshot": cmd_snapshot, "ask": cmd_ask, "judge": cmd_judge, "stability": cmd_stability}[args.cmd](args)


if __name__ == "__main__":
    main()
