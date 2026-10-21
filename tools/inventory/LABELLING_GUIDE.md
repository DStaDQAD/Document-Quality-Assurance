# Labelling guide: sentence inventory

How a sentence of a BI report or press release is labelled, so labels from different people (or
agents) agree. Format v1 plus `labeled_by`. Rules come from wave 1 (UB, Cadev, SULNI, 3 editions).

## Tools (no LLM, no network)

```
python tools/inventory/dump.py eval/inventory/data/<case>__<doc>.json      # sentences + system claims
python tools/inventory/lookup.py eval/cases/<case>/manifest.yaml <doc_id> "rowA|rowB" "2026:Jun,2025:Jun" ["SheetX|SheetY"]
python tools/inventory/write_labels.py <spec.json>                          # writes eval/inventory/labels/
python -m tools.inventory.score                                             # system vs labels
```
`lookup.py` parses the workbooks the way the engine does: always pass the sheet filter (5th
argument) when you know the sheet, it is much faster and lighter on memory. Run one at a time.

## Spec file (input of write_labels.py)

```json
{"<case>__<doc_id>": {"labels": {
  "s001": {"kind": "data", "all_ok": true},
  "s002": {"kind": "data", "all_tcd": "di_luar_tabel", "note": "..."},
  "s003": {"kind": "data", "claims": [
      {"sys": 0, "ok": true},
      {"sys": 1, "ok": false, "row": "<sheet> › <row>", "verdict": "Sesuai", "period": "Juni 2026", "note": "..."},
      {"sys": 2, "reject": true, "note": "why it is not a claim"}],
   "add": [{"number": "9,0", "metric": "M1", "row": "<sheet> › <row>", "measure": "growth_yoy",
            "period": "Agustus 2026", "verdict": "Sesuai", "note": "..."}]},
  "s004": {"kind": "repeat", "repeat_of": "s001"},
  "s005": {"kind": "not_data", "note": "footnote"}}}}
```
- `row`: `"<sheet> › <row>"` exactly as the parsed table names it, or `"outside"` (not in the
  uploaded tables), or `"unsure"`.
- `measure`: level, growth_yoy, growth_mtm, growth_qtq, growth_ytd, change, share, sum, average,
  direction, threshold, ranking, other. `direction`: up, down, stable, none.
- `verdict`: Sesuai | Tidak Sesuai | Tidak Cukup Data. With Tidak Cukup Data give `tcd`:
  di_luar_tabel, periode, jenis, tabel_gagal, lainnya.
- Every system claim of a `data` sentence must be labelled (ok / ok:false / reject); the writer
  refuses otherwise.

## Rules

1. **The truth is the uploaded table**, read with `lookup.py`, never the system's reasoning and never
   your memory of BI figures. Compute the check yourself and write the numbers in `note`
   ("453.368,5 / 434.167,0 = +4,42%").
2. **Sesuai**: the table value, rounded to the decimals the sentence prints, equals the printed number
   (levels, growth rates, shares alike). A change printed without decimals is NOT widened
   ("23 juta" vs 22,78 → Tidak Sesuai, owner's rule).
3. **A system "not found" is not the truth.** Before labelling `outside`, look for the row under
   another name (`lookup.py` with a few words). If it exists, map it and judge it (`ok: false`).
   If the metric is genuinely absent, it is `di_luar_tabel` (e.g. bank credit, DPK, interest rates,
   M0 are not in SEKI I.1A; months of imports are not in V.9).
4. **Quarterly claims on monthly tables**: "triwulan II 2026" = the position at the end of June 2026
   (quarter-end month column). Quarterly releases (SULNI Agustus) depend on this.
5. **Wrong row, right verdict by luck** is still `ok: false` with the right row (the score counts it
   as "right by luck").
6. **Not a claim** (`reject`): qualitative words read as a trend ("tetap terjaga", "tetap sehat",
   titles, "struktur ULN"); a claim the extractor attached to the wrong sentence; a number the LLM
   computed that the sentence does not print (e.g. a change from "dari 4,4 menjadi 7,8"). For the
   last, add what the sentence does say (a direction with `compared_to`, or the two levels).
7. **Missed claims** (`add`): every data number in the sentence that no system claim covers, and
   direction/stability statements ("relatif stabil", "melambat") the system did not extract. A number
   repeated from an earlier sentence that the duplicate filter dropped is a missed claim too.
8. **Repeats**: a sentence marked `same_numbers_as` that restates another one is `kind: repeat`.
   Footnotes, definitions, methodology are `not_data`.
9. **Revisions**: if the table disagrees with the report by a revision-sized amount, label by the
   table and say so in `note`.
10. **The report contradicting itself** (says "naik" while its own numbers fall): label by the table
    as usual and start the note with `REPORT LOOKS INCONSISTENT:`.
11. **Unsure**: never guess. `row: "unsure"`, `verdict: "Tidak Cukup Data"`, `tcd: "lainnya"`, and a
    note starting `UNSURE:` with the question. These go to the owner.
12. Publication conventions that are not plain Indonesian (SBT is a net balance where "tumbuh" means
    above 0; BPM6 deficits and net liabilities are stored negative; fob vs c.i.f.) decide what the
    sentence means; if unsure which applies, rule 11.
