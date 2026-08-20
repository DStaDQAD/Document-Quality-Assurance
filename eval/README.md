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
| **2** | full pipeline (extraction + verdict) | yes | no (snapshot) | `run_e2e_eval.py` |

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
    operation: value                  # value|yoy_growth|average|sum|diff|ratio
                                      # |is_increasing|is_decreasing|is_stable
                                      # |above_threshold|below_threshold
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
- Threshold ops (`above_threshold`/`below_threshold`) put the BOUND in `claimed_value`
  and compare dimensionlessly — no unit conversion runs, so the table `unit` does not
  enter the verdict. The inequality is strict: a value sitting exactly on the bound is
  `Refuted`. Both these and the trend ops are refused as `Inconclusive` when the quote
  hedges over an unnamed subset ("beberapa komponen", "sebagian besar kelompok").
- Keep labelled verdicts *aspirational* (what a correct verifier should do). A case that
  fails is the harness earning its keep — investigate the engine, don't just relabel it.

### Non-time-series tables (attribute columns)

A table whose columns are attributes rather than periods — an item list, a budget, anything
the Tier-2 generic parser reads as `rows x attribute columns` — uses `col_label` in place of
`year`/`month`, on both the cells and the fact's data points:

```yaml
  table:
    title: "Daftar Harga Barang Elektronik"
    data:
      - {label: "Laptop ASUS", col_label: "Harga (Rp)", value: 7500000}
      - {label: "Laptop ASUS", col_label: "Stok", value: 12}
  fact:
    operation: value
    unit: "Rp"
    claimed_value: 7500000
    periods:
      - {metric_label: "Laptop ASUS", col_label: "Harga (Rp)"}
```

- The unit comes from the matched COLUMN's trailing parenthetical (`Harga (Rp)` -> `Rp`),
  falling back to the table's own `unit`. A column with no unit at all (`Stok`) compares raw.
- One table cannot mix the two forms — the loader rejects it, because `_evaluate_fact`
  resolves a data point only against a source of the matching axis kind, so half the cells
  would be silently unreachable.
- Time-only operations (`yoy_growth`, `is_*`) over a `col_label` point, and dated claims
  against a table with no time axis, are both expected to come back `Inconclusive`.

### Several reference sources (source_conflict)

Replace `table:` with a `tables:` list to check one claim against more than one source — the
same situation the live pipeline is in when a report's own tables and an uploaded workbook
both answer a claim. Each entry takes the same keys plus `origin` (`excel` | `pdf`):

```yaml
  tables:
    - {title: "...", unit: "Miliar Rp", filename: "TABEL1_1.xls", sheet: "I.1", data: [...]}
    - {title: "...", unit: "Miliar Rp", filename: "M2-April-2026.pdf", sheet: "Lampiran 1",
       origin: pdf, data: [...]}
  expected:
    verdict: Entailed        # still the best-matching source's verdict
    source_conflict: cross   # internal | cross | omit when the sources should agree
```

- A conflict is not a fourth verdict: the headline verdict still comes from the source whose
  row label matches the claim best, and `source_conflict` reports separately that the
  references disagree. Two disagreeing `pdf` sources are `internal`, anything else `cross`.
- Omitting `source_conflict` means *the sources agree*. A case that starts conflicting then
  fails instead of passing on its verdict alone, so the guards that must NOT fire (identical
  readings, levels-vs-%-yoy units, a looser label match) stay under the same gate.

## Layer 2 — end-to-end eval

Document-level labels: for a real PDF + Excel, list the claims that should be extracted and
their expected verdicts. The runner runs the full pipeline (`extract_narrative_text` +
`verify_paired`, i.e. real LLM extraction), matches the returned facts to the labels, and
reports three separate axes:

- **Extraction recall** — of the labelled claims, how many the pipeline actually found.
- **Spurious facts** — extracted facts matching no label (a proxy for hallucinated claims).
- **Verdict accuracy** — precision/recall/F1 + confusion matrix, computed *only over matched
  claims* (extraction quality and verdict quality are different failures, kept apart).

Matching (`matching.py`) is tolerant but anchored: same operation, the claim's periods present
among the result's periods (a subset — `yoy_growth` adds the prior-year point), and containment
on the metric name. When a label pins `claimed_value`, a candidate stating that number wins over
an earlier one that does not — needed when a document states the same claim twice with different
figures. It is unit-tested deterministically (`tests/test_eval_e2e.py`); only the runner needs a
provider.

### Run it (needs API keys + local files)

```bash
python -m eval.run_e2e_eval                     # runs eval/cases/e2e/*.yaml
python -m eval.run_e2e_eval --json out.json
```

Requires `LLM_PROVIDER` + the matching `*_API_KEY` in `.env`; without them the runner prints a
clear message and exits rather than throwing. Cases point at **local** PDF/Excel files (the BI
samples are gitignored), so this runs on the reviewer's machine, not from a fresh clone.

### Label a document

See `eval/cases/e2e/example_m2_april_2026.yaml` for a worked, verified example. Each case:

```yaml
- id: unique_id
  mode: excel                          # excel (default) | internal | both
  pdf: "sample_data/report.pdf"       # local path (relative to repo root)
  excel: "sample_data/TABEL1_1.xls"   # a string, or a list for multiple sources
  sheets: "I.1"                        # a string, or one per Excel file
  claims:
    - metric: "M2"                     # matched tolerantly against the LLM's extracted label
      operation: yoy_growth
      periods:
        - {metric_label: "M2", year: 2026, month: Apr}
      expected_verdict: Entailed
      claimed_value: 9.2               # optional; only needed to tell repeated claims apart
      note: "optional reviewer note"
```

Aim for a mix of Entailed, Refuted, and Inconclusive claims so the verdict metrics mean
something — especially Refuted, the class that proves the tool catches wrong numbers.

### Scoring internal mode

`mode:` picks the reference pool, exactly as the endpoint's parameter does (see the main
README). `mode: internal` omits `excel:` entirely — the report is scored against the tables
printed inside it, which the runner transcribes with `extract_tables_from_pdf` before verifying:

```yaml
- id: report_internal
  mode: internal
  pdf: "sample_data/report.pdf"
  claims: [...]
```

`eval/cases/e2e/internal_m2_april_2026.yaml` is the worked example: the same BI report twice,
once untouched and once with a single figure doctored in its summary bullet, so the pair scores
both the Entailed and the Refuted class from the document alone. The doctored file keeps the
correct figure in its body paragraph, which is why those two labels carry `claimed_value`.

The JSON report records, per case, the mode and the sources that answered it —
`"sources": ["Hal. 7 · Lampiran 1… [pdf-generic]", …]`. A `-unverified` suffix there means the
table came off a scanned page, so its numbers were read by the model rather than out of the
PDF's text layer; that is the first thing to check when an internal-mode case scores badly.

## Typo-checker eval (deterministic pass)

The fact-checker is only half of what the pipeline shows a user; the other tab is the
spelling/grammar report. This scores it — the deterministic half of it.

```bash
python -m eval.run_typo_eval                    # print the report
python -m eval.run_typo_eval --fail-under 1.0   # exit non-zero if precision < 100% (CI gate)
```

`check_typos(text, llm=None)` runs the curated tidak-baku list, the reduplication rule and the
prefix rules, and **drops** anything it would otherwise escalate. A word unknown to the id_ID
dictionary — a plain misspelling like "Likuidiaftas" — is therefore never reported in this
mode. So the harness measures:

- **Precision**, fully: given real Bank Indonesia wording, does the checker leave correct text
  alone? Report vocabulary is exactly what a naive spell checker mangles — English financial
  terms in parentheses (`Loans`, `Debt Securities`), abbreviations (`DPK`, `SBT`, `yoy`),
  `M1`/`M2`. This is the half that is gated, because a false accusation sends a reader to
  verify something that was never wrong.
- **Recall for the rule-driven categories** only: the curated non-standard spellings and
  reduplication written with a space. Recall on free-form misspellings belongs to an
  LLM-in-the-loop layer that does not exist yet — labelling one here records an honest miss
  rather than a pass.

### Add a case

Drop an entry into any `eval/cases/typo/*.yaml`:

```yaml
- id: unique_snake_case_id
  description: "what this case checks"
  text: "Bank Indonesia terus memantau resiko likuiditas perbankan."
  expect_flagged:
    - {word: "resiko", category: tidak_baku, suggestion: "risiko"}   # category/suggestion optional
  expect_clean: ["likuiditas", "perbankan"]   # words that must NOT be flagged
```

Every labelled word — flagged or clean — must actually occur in `text`; the loader refuses the
case otherwise, so a typo in the LABEL never reads as a finding about the checker. Anything the
checker flags that no `expect_flagged` entry asked for is a false positive, which is why a
clean case carries `expect_flagged: []` rather than being left out of the dataset.
