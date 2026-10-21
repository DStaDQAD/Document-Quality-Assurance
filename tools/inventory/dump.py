"""Compact view of one inventory document for labelling: sentences, numbers, system claims."""
import json, sys
sys.stdout.reconfigure(encoding="utf-8")
path = sys.argv[1]
d = json.load(open(path, encoding="utf-8"))
print(f"# {d['case']} / {d['doc_id']} ({d['publication']}) run={d['system_run']} sources={[s.split('/')[-1] for s in d['sources']]}")
for s in d["sentences"]:
    nums = [n["text"] for n in s["numbers"] if not n["year_like"] and not n["reference"]]
    flags = ("" if s["reaches_extractor"] else " [FILTERED]") + (f" [same_as {s['same_numbers_as']}]" if s.get("same_numbers_as") else "")
    print(f"\n{s['sid']} p{s['page']}{flags} nums={nums}\n  {s['text']}")
    for i, c in enumerate(s["system_claims"]):
        per = ",".join(f"{p.get('month')} {p.get('year')}" for p in c["periods"])
        row = (c["periods"][0].get("metric_label") if c["periods"] else "")
        reason = (c.get("reasoning") or "").replace("\n", " ")[:170]
        print(f"  [{i}] {c['operation']} v={c['claimed_value']} '{c['metric_label']}' @{per} -> {c['verdict']}"
              f" src={(c['matched_source'] or '-').split('/')[-1].strip()} row='{row}' comp={c.get('computed_value')} via={c.get('resolved_via')}\n      {reason}")
for u in d["unplaced_claims"]:
    print("UNPLACED", u["operation"], u["claimed_value"], u["context_quote"][:100])
