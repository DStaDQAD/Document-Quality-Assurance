"""Score the system against the inventory labels: per publication, how its claims fared.

usage: python -m tools.inventory.score [--dir eval/inventory] [--publication <key> ...] [--run <name>]

For every labelled system claim: right as is, wrong verdict (false Sesuai / false Tidak Sesuai /
false Tidak Cukup Data), or not a claim at all (rejected). Plus the claims the system never made
(added by the labeller). Numbers only, read from the label files; no LLM.

--run <name> scores eval/cases/<case>/<name>/<doc>.json instead of the run the labels were made on.
A label points at a claim by its index in that run (from_system), which means nothing in another
run, so the new run's claims are placed on the sentences again and paired with the labelled claims
by printed number, then measure, row and period (match_sentence). Score a baseline the same way
(--run run1) before comparing.
"""

import argparse
import copy
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.inventory.write_labels import MONTH_ID, OP_MEASURE, _to_float  # noqa: E402

SYS = {"Entailed": "Sesuai", "Refuted": "Tidak Sesuai", "Inconclusive": "Tidak Cukup Data"}
_NUMBERLESS_OPS = {"above_threshold", "below_threshold", "is_increasing", "is_decreasing", "is_stable"}
_NUMBERLESS_MEASURES = {"threshold", "direction"}


def score(base: Path, only=None):
    rows = defaultdict(lambda: defaultdict(int))
    for lf in sorted((base / "labels").glob("*.json")):
        labels = json.loads(lf.read_text(encoding="utf-8"))
        pub = labels["publication"]
        if only and pub not in only:
            continue
        data = json.loads((base / "data" / lf.name).read_text(encoding="utf-8"))
        by_sid = {s["sid"]: s for s in data["sentences"]}
        r = rows[pub]
        r["documents"] += 1
        for sid, lab in labels["labels"].items():
            if lab.get("status") != "done":
                continue
            r["sentences"] += 1
            for c in lab.get("claims", []):
                if c.get("from_system") is None:
                    r["missed_by_system"] += 1
                    continue
                sys_claim = by_sid[sid]["system_claims"][c["from_system"]]
                got = SYS.get(sys_claim["verdict"])
                r["system_claims"] += 1
                if c.get("rejected"):
                    r["not_a_claim"] += 1
                    r[f"not_a_claim_judged_{got}"] += 1
                    continue
                want = c["expected_verdict"]
                if got == want:
                    r["verdict_right"] += 1
                    if not c.get("system_correct"):
                        r["right_verdict_wrong_reading"] += 1
                else:
                    r[f"false_{got}"] += 1
                    r[f"false_{got}_should_be_{want}"] += 1
    return rows


def _gold_number(claim):
    if claim.get("measure") in _NUMBERLESS_MEASURES or not claim.get("number"):
        return None
    try:
        return abs(_to_float(claim["number"]))
    except ValueError:
        return None


def _sys_number(claim):
    if claim.get("operation") in _NUMBERLESS_OPS or claim.get("claimed_value") is None:
        return None
    return abs(claim["claimed_value"])


def _words(text):
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) > 1}


def _sys_row(claim):
    periods = claim.get("periods") or []
    return periods[0].get("metric_label") if periods else None


def _sys_period(claim):
    periods = claim.get("periods") or []
    if not periods:
        return ""
    p = periods[-1]
    return " ".join(x for x in (MONTH_ID.get(p.get("month"), p.get("month")), str(p.get("year") or "")) if x).strip()


def _pair_score(gold, system):
    """How well a system claim fits a labelled claim; None when they cannot be the same claim. The
    printed number must agree when either has one; measure, row words and period rank the rest."""
    g, s = _gold_number(gold), _sys_number(system)
    same_measure = OP_MEASURE.get(system.get("operation"), "other") == gold.get("measure")
    if g is None and gold.get("measure") not in _NUMBERLESS_MEASURES:
        # A number the sentence does not print where the label sits (a sentence split across a page
        # break): the measure has to agree instead.
        if not same_measure or s is None:
            return None
    elif (g is None) != (s is None) or (g is not None and abs(g - s) > 1e-6):
        return None
    fit = 3.0 if same_measure else 0.0
    row = (gold.get("maps_to") or {}).get("row") or gold.get("metric_text")
    a, b = _words(row), _words(_sys_row(system) or system.get("metric_label"))
    if a and b:
        fit += len(a & b) / len(a | b)
    if gold.get("period") and gold.get("period") == _sys_period(system):
        fit += 1.0
    return fit


def match_sentence(sentence, gold_claims, system_claims):
    """(pairs, missed, extra): labelled claims paired one to one with the system claims of the same
    sentence, best fit first; the labelled claims left over were missed, the system claims left over
    are not real claims."""
    scored = sorted(
        ((fit, gi, si) for gi, gold in enumerate(gold_claims) for si, system in enumerate(system_claims)
         if (fit := _pair_score(gold, system)) is not None),
        key=lambda t: (-t[0], t[1], t[2]),
    )
    used_g, used_s, pairs = set(), set(), []
    for _fit, gi, si in scored:
        if gi in used_g or si in used_s:
            continue
        used_g.add(gi)
        used_s.add(si)
        pairs.append((gold_claims[gi], system_claims[si]))
    missed = [g for i, g in enumerate(gold_claims) if i not in used_g]
    extra = [s for i, s in enumerate(system_claims) if i not in used_s]
    return pairs, missed, extra


def _same_row(a, b):
    return " ".join((a or "").lower().split()) == " ".join((b or "").lower().split())


def tally(pairs, missed, extra):
    """The score.py counters for one sentence's pairing."""
    r = Counter()
    for gold, system in pairs:
        got, want = SYS.get(system.get("verdict")), gold.get("expected_verdict")
        r["system_claims"] += 1
        if got == want:
            r["verdict_right"] += 1
            maps = gold.get("maps_to") or {}
            row = _sys_row(system)
            if got != "Tidak Cukup Data" and maps.get("kind") == "row" and row and not _same_row(maps.get("row"), row):
                r["right_verdict_wrong_reading"] += 1
        else:
            r[f"false_{got}"] += 1
            r[f"false_{got}_should_be_{want}"] += 1
    r["missed_by_system"] += len(missed)
    for system in extra:
        got = SYS.get(system.get("verdict"))
        r["system_claims"] += 1
        r["not_a_claim"] += 1
        r[f"not_a_claim_judged_{got}"] += 1
    return r


def score_run(base: Path, run: str, only=None, cases_root: Path = ROOT / "eval" / "cases"):
    """Like score(), for the run named `run` of every labelled document (see the module docstring)."""
    from tools.inventory.export import attach_claims
    rows = defaultdict(lambda: defaultdict(int))
    for lf in sorted((base / "labels").glob("*.json")):
        labels = json.loads(lf.read_text(encoding="utf-8"))
        pub = labels["publication"]
        if only and pub not in only:
            continue
        r = rows[pub]
        run_file = cases_root / labels["case"] / run / f"{labels['doc_id']}.json"
        if not run_file.exists():
            r["documents_without_run"] += 1
            continue
        data = json.loads((base / "data" / lf.name).read_text(encoding="utf-8"))
        sentences = copy.deepcopy(data["sentences"])
        for s in sentences:
            s["system_claims"] = []
        results = json.loads(run_file.read_text(encoding="utf-8")).get("results", [])
        r["unplaced"] += len(attach_claims(sentences, results))
        by_sid = {s["sid"]: s for s in sentences}
        r["documents"] += 1
        for sid, lab in labels["labels"].items():
            if lab.get("status") != "done" or sid not in by_sid:
                continue
            r["sentences"] += 1
            kind = lab.get("sentence_kind")
            claims = by_sid[sid]["system_claims"]
            if kind == "data":
                gold = [c for c in lab.get("claims", []) if not c.get("rejected")]
                counts = tally(*match_sentence(by_sid[sid], gold, claims))
            elif kind == "not_data":
                counts = tally([], [], claims)
            else:   # a repeat: its claims are the twin sentence's, counted there
                continue
            for k, v in counts.items():
                r[k] += v
    return rows


def main(argv=None):
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=str(ROOT / "eval" / "inventory"))
    ap.add_argument("--publication", action="append")
    ap.add_argument("--run", help="score eval/cases/<case>/<RUN>/ instead of the labelled run")
    args = ap.parse_args(argv)
    rows = (score_run(Path(args.dir), args.run, args.publication) if args.run
            else score(Path(args.dir), args.publication))
    for pub, r in sorted(rows.items()):
        n = r["system_claims"]
        print(f"\n## {pub}: {r['documents']} documents, {r['sentences']} sentences, {n} system claims")
        print(f"  verdict right:          {r['verdict_right']} ({100 * r['verdict_right'] / max(n, 1):.0f} %)"
              f"   of which right by luck (wrong row/reading): {r['right_verdict_wrong_reading']}")
        for k in sorted(k for k in r if k.startswith("false_") and "_should_be_" in k):
            print(f"  {k.replace('_', ' ')}: {r[k]}")
        print(f"  not a claim at all:     {r['not_a_claim']}"
              + "".join(f"  (judged {k.split('judged_')[1]}: {r[k]})" for k in sorted(r) if k.startswith("not_a_claim_judged_")))
        print(f"  claims the system missed: {r['missed_by_system']}")
        if r.get("unplaced") or r.get("documents_without_run"):
            print(f"  claims on no sentence: {r['unplaced']}   documents without this run: "
                  f"{r['documents_without_run']}")


if __name__ == "__main__":
    main()
