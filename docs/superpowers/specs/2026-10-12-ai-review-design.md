# Tinjauan AI — design

**Status:** design agreed in chat on 2026-10-12. Part A (measurement) is approved to run. Part B (shipping it) depends on Part A's gate.

## Problem

Claims are checked in three steps:
1. An LLM extracts each claim into structured form.
2. Code matches the claim's label and period to a table row.
3. Code does the arithmetic.

Every claim the matching step gets wrong ends as **Tidak Sesuai** or **Tidak Cukup Data**. Fixing those cases one rule at a time never ends: each new publication brings new wording.

The 2026-10-12 spike ran 661 leftover claims from 11 publications, twice, through Gemini 2.5 Flash. The model saw the parsed tables, had to cite the cells it used, and code checked those cells.

| Change | Count | Correct |
|---|---|---|
| Refuted → Entailed | 17 | 15 |
| Refuted → Inconclusive ("not in the table") | 23 | 12/12 sampled |
| Inconclusive → Entailed | 52 | 20/20 sampled |
| Inconclusive → Refuted | 12 | ~1 |

- The 2 wrong Refuted → Entailed were number-less trend claims.
- The Inconclusive → Refuted errors had two sources:
  - The model used a total or parent row for a breakdown claim (impor minyak → Migas; remitansi → Pendapatan Sekunder).
  - The spike script itself lacked threshold operations and unit scaling (juta/miliar).
- Cost: about Rp550 per check.

## Goal

Add a stage after the code checks. It re-examines only the Tidak Sesuai and Tidak Cukup Data claims with an LLM, and it may change a verdict only where code can prove the change.

## Design

### Inputs
For each check:
- **Tables:** every parsed Excel or PDF table (no chart sources, no other-PDF sources, no empty tables). Each is rendered as markdown with every row but only the last **2 years** of period columns (categorical tables keep all columns). Each sheet gets a short code `S1`, `S2`, … to cite.
- **Claims:** the Tidak Sesuai and Tidak Cukup Data narrative claims, **30 per call**. The tables are re-sent with each batch.
- **Per claim:** the sentence (first 400 characters), the extractor's metric label, the claimed number and unit, and the operation.

### What the model answers, per claim, in this field order
The order makes it reason before it judges:
1. `subject`: the phrase naming what is measured, copied verbatim from the sentence.
2. `word_check`: each significant subject word, and whether the chosen row (or its parent, or the table title) contains it.
3. `match`: `persis` | `total_atau_induk` | `mirip` | `tidak_ada`.
4. `verdict`: `sesuai` | `tidak_sesuai` | `tidak_ada_di_tabel`. The first two are allowed only with `match = persis`. When unsure, `tidak_ada_di_tabel`.
5. `operation` (the engine's operation names) and `cells` (sheet code, row label, column header, value).
6. `explanation`: at most 2 sentences, in Indonesian.

Thinking is **on** for this stage only (`AI_REVIEW_THINKING_BUDGET`, default 1024). The rest of the pipeline keeps a thinking budget of 0.

### What code accepts (`judge_review`)
1. If the answer's first words do not match the claim sentence, the claim number was swapped: keep the code verdict.
2. If the answer is `tidak_ada_di_tabel`:
   - a Refuted claim becomes **Inconclusive**;
   - an Inconclusive claim stays as it is.
3. If `match ≠ persis`, keep the code verdict.
4. Each cited cell must exist, with that value, and all cells must come from one sheet. Otherwise keep the code verdict.
5. **Keyword guard** (`subject_is_covered`). The subject must appear verbatim in the sentence. Every significant subject word, apart from generic words, must match a word of the cited row label (including its `Parent > ` path), the table title, or the sheet name. Matching is exact or one derivational prefix apart (`_same_root`). Otherwise keep the code verdict.
6. **Rebuild and re-check.** The claim is rebuilt from the cited cells. The ordinary engine `_evaluate_fact` runs it against a copy of the sheet cut down to the cited rows, so units, thresholds, tolerances and the deficit rules all apply. If the engine says Inconclusive, or disagrees with the model, keep the code verdict.
7. If the engine says **Entailed**, the claim becomes **Entailed**.
8. If the engine says **Refuted**:
   - a Refuted claim stays as it is;
   - an Inconclusive claim with `AI_REVIEW_NEW_REFUTED=note` (the default) stays Inconclusive and is flagged "perlu ditinjau";
   - an Inconclusive claim with `AI_REVIEW_NEW_REFUTED=verdict` becomes **Refuted**.

How a changed claim is marked:
- `resolved_via = "ai_review"` on every changed claim;
- `resolved_via = "ai_review_flag"` on a flagged claim;
- the reasoning gets a `Ditinjau AI: …` part that names the cells used.
- No schema change.

### Failure handling
- A failed batch keeps its claims' code verdicts.
- A failure of the whole stage keeps every code verdict, and the check still finishes.

### Configuration
| Variable | Default | Meaning |
|---|---|---|
| `AI_REVIEW` | off | `1`/`on` enables the stage |
| `AI_REVIEW_NEW_REFUTED` | `note` | `verdict` lets a new Tidak Sesuai stand |
| `AI_REVIEW_THINKING_BUDGET` | `1024` | Thinking budget for this stage; `-1` = model default, `0` = off |

### UI
- A small badge next to the verdict: "Ditinjau AI" for `ai_review`, "Perlu ditinjau" for `ai_review_flag`.
- The cells used appear in the existing Detail list (from the reasoning).
- Riwayat shows the badges too, because it re-renders the stored response.

## Part A — measurement (before anything ships)

**Data.**
- A snapshot of the same leftover claims as the spike, built from the e2e run JSONs, in `eval/cases/ai_review/runs/`.
- Hand labels in `eval/cases/ai_review/labels.json`.
- Both are local only (gitignored), like the BI workbooks they need.

**Runs.**
- Ask twice with thinking 1024 (`t1024a`, `t1024b`).
- Ask once with thinking 0 (`t0`).
- Each answer file is then judged with and without the keyword guard. This needs no extra LLM call.

**Labels.** Every claim whose outcome changed gets a label, except Refuted → Inconclusive, which is sampled at 25 per run. A label is the true verdict, checked by hand against the workbook. The user or the mentor spot-checks 10 labels.

**Gate.** Part B ships only if the chosen configuration meets all of these:

| Measure | Required |
|---|---|
| → Entailed correct | ≥ 95% |
| → Inconclusive correct | ≥ 90% |
| Stability among changed claims (`t1024a` vs `t1024b`) | ≥ 90% |
| Cost | ≤ Rp1.000 per check (average over the 11 publications) |
| Extra wall-clock time | ≤ 30 s per check (median) |

A new Tidak Sesuai, whether shown as a verdict or flagged, must be correct **≥ 90%** for `AI_REVIEW_NEW_REFUTED=verdict` to be recommended. Below that, the default stays `note`.

The results go in `docs/ai-review-eval.md`. That file holds numbers only, no claim text.

## Part B — shipping (only if the gate passes)
- `verify_paired` gets a `review_llm` parameter. The stage runs after the cell pointer and the chart settling, and before the counts.
- `main` builds the review LLM when `AI_REVIEW` is on.
- The UI badges.
- A regression check: every publication end to end with `AI_REVIEW=on`, against a same-day `main` baseline.

## Out of scope
- Chart labels.
- Typo checking.
- Claims the code already judged Sesuai.
- Qwen. The Part A eval set is reused later for the Qwen A/B, once the Alibaba account clears.
