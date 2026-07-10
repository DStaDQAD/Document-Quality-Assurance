# Accuracy evaluation harness

Measures how correct the paired PDF↔Excel fact-checker actually is — not just that the
code runs, but that it assigns the right verdict to real claims. The unit tests in
`tests/` check plumbing; this harness checks *quality*.

## Why two layers

The pipeline mixes two very different components:

1. A **deterministic comparison engine** (`paired_verifier._evaluate_fact`): arithmetic,
   unit conversion, YoY, tolerance, verdict. Given a fact + an Excel table its output is
   fixed. This part must be ~100% correct.
2. A **non-deterministic LLM extraction** step (`structured_extractor`): reads the PDF
   narrative and produces facts. Its output varies run to run.

Scoring both with one number hides which half is at fault. So the harness is split:

| Layer | Scores | LLM? | Reproducible | Runner |
|------|--------|------|--------------|--------|
| **1** | comparison engine only | no | yes (CI-safe) | `run_comparison_eval.py` |
| **2** | full pipeline (extraction + verdict) | yes | no (snapshot) | `run_e2e_eval.py` *(planned)* |

## Layer 1 — comparison-engine eval (available now)

Each case is self-contained: an inline Excel table (real Bank Indonesia M2 numbers,
frozen into YAML so no `.xls` file or `xlrd` is needed at runtime), one fully specified
extracted fact, and the verdict a correct verifier should return. The runner feeds the
fact + table straight into `_evaluate_fact` and compares.

### Run it

```bash
python -m eval.run_comparison_eval                 # print the report
python -m eval.run_comparison_eval --json out.json # also dump machine-readable metrics
python -m eval.run_comparison_eval --fail-under 1.0 # exit non-zero if accuracy < 100% (CI gate)
```

Report includes overall accuracy, macro-F1, per-verdict precision/recall/F1, a confusion
matrix, and a list of any failing cases with the engine's reasoning.

### Add a case

Drop a new entry into any `eval/cases/comparison/*.yaml` (each file is a YAML list):

```yaml
- id: unique_snake_case_id
  description: "what this case checks"
  table:
    title: "Uang Beredar dan faktor-faktor yang mempengaruhinya"
    unit: "Miliar Rp"                 # the Excel unit
    data:
      - {label: "Uang Beredar Luas(M2)", year: 2026, month: Apr, value: 10253651.888}
  fact:
    operation: value                  # value|yoy_growth|average|sum|diff|ratio|is_increasing|is_decreasing|is_stable
    unit: "triliun Rp"                # the unit the PDF claim is stated in (omit for trend ops)
    claimed_value: 10253.7            # omit for is_increasing/is_decreasing/is_stable
    context_quote: "M2 ... Rp10.253,7 triliun"
    periods:
      - {metric_label: "Uang Beredar Luas(M2)", year: 2026, month: Apr}
  expected:
    verdict: Entailed                 # Entailed|Refuted|Inconclusive
    computed_value: 10253.6519        # optional; cross-checks the number the engine computed
```

Notes:
- `value`/`average`/`sum`/`diff` need a compatible `unit` for conversion; the table `unit`
  must be one the engine knows (see `paired_verifier._UNIT_FACTORS`).
- `yoy_growth` fetches the prior-year same-month point automatically — include it in `data`
  but list only the current point under `fact.periods`.
- Trend ops (`is_*`) carry no `claimed_value`/`unit`.
- Keep labelled verdicts *aspirational* (what a correct verifier should do). A case that
  fails is the harness earning its keep — investigate the engine, don't just relabel it.

## Layer 2 — end-to-end eval (planned)

Document-level labels: for a real PDF + Excel, list the claims that should be extracted and
their expected verdicts. The runner runs the full `verify_paired`, matches extracted facts to
labels, and reports extraction recall, spurious-fact count (hallucination proxy), and verdict
accuracy. Requires API keys and is run on demand, not in CI.
