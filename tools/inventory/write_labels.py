"""Write Claude's labels (compact spec JSON) into eval/inventory/labels/<doc>.json, format v1.

Spec: {"doc": "<case>__<doc_id>", "labels": {"s001": {"kind": "data"|"not_data"|"repeat",
  "repeat_of": "s002", "note": "...",
  "claims": [{"sys": 0, "ok": true} | {"sys": 0, "reject": true, "note": ..} |
             {"sys": 0, "ok": false, <fields to override>}],
  "add": [{<manual claim fields>}]}}}
Claim fields: number, metric, row ("<sheet> › <row>" or "outside" or "unsure"), measure, period,
compared_to, direction, verdict (Sesuai|Tidak Sesuai|Tidak Cukup Data), tcd, note.
"""
import json, sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SYS_VERDICT = {"Entailed": "Sesuai", "Refuted": "Tidak Sesuai", "Inconclusive": "Tidak Cukup Data"}
OP_MEASURE = {"value": "level", "yoy_growth": "growth_yoy", "diff": "change", "ratio": "share", "sum": "sum",
              "average": "average", "is_increasing": "direction", "is_decreasing": "direction",
              "is_stable": "direction", "above_threshold": "threshold", "below_threshold": "threshold"}
OP_DIRECTION = {"is_increasing": "up", "is_decreasing": "down", "is_stable": "stable"}
MONTH_ID = {"Jan": "Januari", "Feb": "Februari", "Mar": "Maret", "Apr": "April", "May": "Mei", "Jun": "Juni",
            "Jul": "Juli", "Aug": "Agustus", "Sep": "September", "Oct": "Oktober", "Nov": "November", "Dec": "Desember"}


def _to_float(tok):
    t = tok.replace("−", "-")
    t = t.replace(".", "") if ("," in t or t.count(".") > 1 or (t.count(".") == 1 and len(t.split(".")[1]) == 3)) else t
    return float(t.replace(",", "."))


def printed(sentence, value):
    if value is None:
        return None
    for n in sentence["numbers"]:
        if n["year_like"] or n["reference"]:
            continue
        try:
            if abs(abs(_to_float(n["text"])) - abs(value)) < 1e-9:
                return n["text"]
        except ValueError:
            pass
    return None


def maps(row):
    if row in (None, "unsure"):
        return {"kind": "unsure"}
    if row == "outside":
        return {"kind": "outside_uploaded"}
    sheet, _, name = row.partition(" › ")
    return {"kind": "row", "source": sheet.strip(), "row": name.strip()}


def from_system(sentence, i):
    c = sentence["system_claims"][i]
    p = c["periods"][0] if c["periods"] else {}
    period = " ".join(x for x in (MONTH_ID.get(p.get("month"), p.get("month")), str(p.get("year") or "")) if x).strip()
    src = (c["matched_source"] or "").split("/")[-1].strip()
    return {
        "number": printed(sentence, c["claimed_value"]), "from_system": i, "system_correct": True,
        "metric_text": c["metric_label"] or "",
        "maps_to": {"kind": "row", "source": src, "row": p.get("metric_label")} if c["matched_source"] else {"kind": "unsure"},
        "measure": OP_MEASURE.get(c["operation"], "other"), "period": period, "compared_to": "",
        "direction": OP_DIRECTION.get(c["operation"], "none"),
        "expected_verdict": SYS_VERDICT.get(c["verdict"], ""), "tcd_reason": "",
    }


def override(claim, spec):
    keys = {"number": "number", "metric": "metric_text", "measure": "measure", "period": "period",
            "compared_to": "compared_to", "direction": "direction", "verdict": "expected_verdict", "tcd": "tcd_reason",
            "note": "note"}
    for k, field in keys.items():
        if k in spec:
            claim[field] = spec[k]
    if "row" in spec:
        claim["maps_to"] = maps(spec["row"])
    return claim


def build(doc, spec):
    data = json.loads((ROOT / "eval/inventory/data" / f"{doc}.json").read_text(encoding="utf-8"))
    by_sid = {s["sid"]: s for s in data["sentences"]}
    out = {}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for sid, ls in spec["labels"].items():
        s = by_sid[sid]
        claims = []
        # Shortcuts: "all_ok": true (every system claim right as is), "all_tcd": "<reason>" (every
        # system claim is a correct Tidak Cukup Data for that reason). Explicit "claims" win per index.
        explicit = {cs["sys"] for cs in ls.get("claims", [])}
        auto = []
        if ls.get("all_ok") or ls.get("all_tcd"):
            for i in range(len(s["system_claims"])):
                if i not in explicit:
                    auto.append({"sys": i, "ok": True, **({"tcd": ls["all_tcd"]} if ls.get("all_tcd") else {})})
        for cs in auto + ls.get("claims", []):
            i = cs["sys"]
            if cs.get("reject"):
                claims.append({"from_system": i, "rejected": True, "system_correct": False, "note": cs.get("note", "")})
                continue
            base = from_system(s, i)
            if cs.get("ok"):
                if "tcd" in cs:
                    base["tcd_reason"] = cs["tcd"]
                if "note" in cs:
                    base["note"] = cs["note"]
                claims.append(base)
            else:
                base["system_correct"] = False
                claims.append(override(base, cs))
        for add in ls.get("add", []):
            c = {"number": None, "from_system": None, "system_correct": None, "metric_text": "",
                 "maps_to": {"kind": "unsure"}, "measure": "level", "period": "", "compared_to": "",
                 "direction": "none", "expected_verdict": "", "tcd_reason": ""}
            claims.append(override(c, add))
        if ls["kind"] == "data":
            missing = [i for i in range(len(s["system_claims"])) if not any(c.get("from_system") == i for c in claims)]
            assert not missing, f"{doc} {sid}: system claims not labelled {missing}"
            bad = [c for c in claims if not c.get("rejected") and not c.get("expected_verdict")]
            assert not bad, f"{doc} {sid}: claim without verdict"
        out[sid] = {"status": "done", "sentence_kind": ls["kind"], "repeat_of": ls.get("repeat_of"),
                    "claims": claims if ls["kind"] == "data" else [], "note": ls.get("note", ""),
                    "labeled_by": "claude", "format_version": 1, "updated_at": now}
    missing = [s["sid"] for s in data["sentences"] if s["sid"] not in out]
    assert not missing, f"{doc}: sentences not labelled {missing}"
    path = ROOT / "eval/inventory/labels" / f"{doc}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"format_version": 1, "case": data["case"], "doc_id": data["doc_id"],
                                "publication": data["publication"], "labels": out}, ensure_ascii=False, indent=1),
                    encoding="utf-8")
    return out


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    for f in sys.argv[1:]:
        spec = json.loads(Path(f).read_text(encoding="utf-8"))
        for doc, s in spec.items():
            labels = build(doc, s)
            n = sum(len(l["claims"]) for l in labels.values())
            print(f"{doc}: {len(labels)} sentences, {n} claims labelled")
