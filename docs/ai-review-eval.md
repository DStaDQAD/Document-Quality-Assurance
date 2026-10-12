# Tinjauan AI — measurement (Part A)

Spec: `docs/superpowers/specs/2026-10-12-ai-review-design.md`. Runner: `eval/run_ai_review_eval.py`.
Data (local only): 661 claims the code left Tidak Sesuai / Tidak Cukup Data in the latest e2e runs of
12 publications (`eval/cases/ai_review/runs/`), hand labels in `eval/cases/ai_review/labels.json`
(120 claims, every claim whose outcome changed in either run). Gemini 2.5 Flash, thinking budget 1024.
Measured 2026-10-12.

## Runs

| | t1024a | t1024b |
|---|---|---|
| Batches | serial | parallel (8) |
| Claims answered | 661/661 | 637/661 (one NPI batch came back with 6 of 30 answers) |
| Cost | Rp7.773 (Rp648/check) | Rp10.257 (Rp855/check) |
| Seconds per publication, median / max | 37,7 / 215,8 | 27,3 / 256,5 (NPI) |

`t0` (thinking off) was not run: the first attempt was stopped by the machine running low on memory, and the two
thinking runs already settle the gate.

## Precision, with the subject guard as shipped (guard only on a new Tidak Sesuai)

| Outcome | t1024a | t1024b |
|---|---|---|
| → Sesuai | 61/61 (100%) | 76/76 (100%) |
| → Tidak Cukup Data | 33/33 (100%) | 28/28 (100%) |
| New Tidak Sesuai (flag / verdict) | 0 | 0 |
| True Tidak Sesuai lost | 0 | 0 |

Variants on the same answers:

| Variant | → Sesuai | → Tidak Cukup Data | Flags |
|---|---|---|---|
| Guard on every change (as first planned), t1024a | 12/12 — blocked 49 correct | 33/33 | 0 |
| No guard at all, t1024a | 61/61 | 33/33 | 2, both wrong |
| No guard at all, t1024b | 76/76 | 28/28 | 6, all wrong |

So the guard is kept only where it earns its place: the 8 flags it stops were all wrong (a city's growth read off
the all-cities index; October 2026 read off October 2025; "tumbuh" as SBT > 0 read as a rise).

V0 (2026-10-12 spike, old prompt): → Sesuai 15/17 + 20/20 sampled; → Tidak Cukup Data 12/12 sampled;
new Tidak Sesuai ~1/12.

## Stability (t1024a vs t1024b)

- Same outcome: 611/637 = 95,9%.
- Among claims changed in either run: 86/112 = 76,8%.
- All 26 differences are "found in one run, left unchanged in the other" (18 + 3 → Sesuai, 5 → Tidak Cukup Data).
  No claim got opposite verdicts in the two runs.

## Gate

| Measure | Required | Result | |
|---|---|---|---|
| → Sesuai correct | ≥ 95% | 100% (137/137) | ✅ |
| → Tidak Cukup Data correct | ≥ 90% | 100% (61/61) | ✅ |
| Stability among changed claims | ≥ 90% | 76,8% — no conflicting verdicts | ❌ as written |
| Cost per check | ≤ Rp1.000 | Rp648–855 | ✅ |
| Extra time per check, median | ≤ 30 s | 27,3 s (parallel batches) | ✅ |
| New Tidak Sesuai correct, for `AI_REVIEW_NEW_REFUTED=verdict` | ≥ 90% | 0 produced; 0/8 without the guard | keep `note` |

Labels were checked by the author only; the human spot-check of 10 labels is still open.

## Open findings

- A batch can come back short (NPI t1024b: 6 of 30 answers, 256 s, double the output tokens). The unanswered
  claims keep the code verdict, so this costs coverage and time, not correctness. A fix for Part B: re-ask the
  claims a batch did not answer, or smaller batches for long reports.
- Instability is coverage, not correctness: a second ask of the claims left unchanged would recover most of the
  26.

## Recommendation

`AI_REVIEW_THINKING_BUDGET=1024`, `AI_REVIEW_NEW_REFUTED=note`. Every gate passes except stability as written; its
failure is in how many claims get fixed, never in a wrong verdict. Whether that is enough to start Part B is the
user's decision.

## Part B regression (2026-10-12, branch `ai-review`, `AI_REVIEW=1`)

Every publication end to end through `/api/verify-paired` once, with the shipped stage (parallel batches,
re-ask of unanswered claims, the three prompt clarifications). Instead of a second same-day run of `main`,
each claim the stage changed carries `resolved_via = ai_review` / `ai_review_flag`, so the code-path verdict
of every other claim is exactly what `main` produces in that run; claims the code judged Sesuai are never
reviewed. All 114 changed claims were checked by hand.

| Publication | Facts | Sesuai | Tidak Sesuai | Tidak Cukup Data | → Sesuai by AI | → TCD by AI |
|---|---|---|---|---|---|---|
| npi | 323 | 66 | 12 | 245 | 7 | 17 |
| pii | 85 | 39 | 2 | 44 | 16 | 2 |
| pmi | 52 | 41 | 7 | 4 | 6 | 0 |
| skdu | 179 | 138 | 5 | 36 | 34 | 0 |
| sbank | 46 | 38 | 1 | 7 | 4 | 0 |
| shpr | 64 | 21 | 0 | 43 | 0 | 0 |
| sk | 83 | 55 | 2 | 26 | 1 | 3 |
| spe | 71 | 55 | 0 | 16 | 18 | 0 |
| sulni | 17 | 14 | 0 | 3 | 0 | 0 |
| uang-beredar | 84 | 21 | 2 | 61 | 0 | 3 |
| uang-primer-m0 | 95 | 33 | 5 | 57 | 0 | 3 |
| cadangan-devisa | 3 | 3 | 0 | 0 | 0 | 0 |
| **Total** | 1102 | 524 | 36 | 542 | **86** | **28** |

- → Sesuai: 85/86 correct (98,8%). The wrong one: NPI "defisit neraca minyak 10,2 miliar USD" read off the
  Migas row (−10.180); oil is not oil-and-gas, and the Q1 figures (6,0 vs −5.087) show the Q2 match is a
  coincidence. This is the case the subject guard caught before it was limited to new Tidak Sesuai.
- → Tidak Cukup Data: 27/28 (96%). The miss: PII "KFLN surat utang 27,3%" — truly Sesuai (27,26%), was a
  wrong Tidak Sesuai; now Tidak Cukup Data.
- New Tidak Sesuai: none. No flags.
- The regression also showed `_compute_ratio` raising (contained) on 3-cell "ratio" answers; such answers are
  now refused before the engine.
