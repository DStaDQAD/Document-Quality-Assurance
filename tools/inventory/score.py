"""Score the system against the inventory labels: per publication, how its claims fared.

usage: python -m tools.inventory.score [--dir eval/inventory] [--publication <key> ...]

For every labelled system claim: right as is, wrong verdict (false Sesuai / false Tidak Sesuai /
false Tidak Cukup Data), or not a claim at all (rejected). Plus the claims the system never made
(added by the labeller). Numbers only, read from the label files; no LLM.
"""

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SYS = {"Entailed": "Sesuai", "Refuted": "Tidak Sesuai", "Inconclusive": "Tidak Cukup Data"}


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


def main(argv=None):
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=str(ROOT / "eval" / "inventory"))
    ap.add_argument("--publication", action="append")
    args = ap.parse_args(argv)
    rows = score(Path(args.dir), args.publication)
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


if __name__ == "__main__":
    main()
