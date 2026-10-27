# Strict Extraction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A second, clean check path in a new `strict/` package: the extractor sees each Excel table's shape (no values), names the exact cells every claim needs or says it is not in the tables, and the engine reads those cells and computes, with no word matching and no row guards.

**Architecture:** `strict/` holds five small modules (tables → claims schema → extraction → evaluation → pipeline). `paired_verifier.verify_paired` hands the check to `strict.pipeline.run_check` at one point when `STRICT_EXTRACTION` is on; otherwise nothing changes. The regression runner saves strict claims so the strict engine replays for free.

**Tech Stack:** Python 3.13 (`.venv`), Pydantic v2, LangChain structured output (Gemini 2.5 Flash), pytest.

**Spec:** `docs/superpowers/specs/2026-10-26-strict-extraction-design.md`

## Global Constraints

- Flag `STRICT_EXTRACTION`: on for `1`/`on`/`true`/`yes` (case-insensitive, trimmed), off otherwise. Off: prompt, schema and every verdict exactly as today.
- Strict runs only when `mode == "excel"`, there is no PDF-internal or reference table, and at least one parsed source is a non-pointer-only temporal table. Otherwise the old path runs.
- The extractor never sees cell values.
- `strict/` may import from `paired_verifier` only: `_ExcelSource`, `MATCH_TOLERANCE`, `_unit_factor`, `_parse_scale_unit`, `_numeric_verdict`, `_value_verdict`, `_growth_tolerance`, `_stable_band`, `_period_ordinal`. Never its matching (`lookup_fuzzy`, `_evaluate_fact`, label scoring) or any guard. A test enforces this.
- Rounding: a level read off one cell → `_value_verdict` (rounds as printed); anything derived (diff, sum, average, ratio, yoy from two levels, formula) → `MATCH_TOLERANCE`, except yoy from two levels which uses `_growth_tolerance(current, prior, claimed, quote)`. A fall is negative; comparisons are signed.
- Not in the tables, or a cell that does not exist → Inconclusive straight away, no word-matching retry.
- Reason codes come from `schemas.ReasonCode` only: `di_luar_tabel` (AI said not in tables), `tidak_ditemukan` (bad table/row), `periode` (bad period), `jenis` (units cannot be compared), `klaim_tidak_lengkap` (no cells, or an all-members claim that leaves members out).
- User-facing text (reasoning, notes) in Indonesian.
- Use the `.venv` Python. Edit regex-bearing lines with the Edit/Write tools, never through Bash heredocs (backslash hazard).
- Commit messages carry no `Co-Authored-By` trailer (owner's rule).

## Review Focus

1. The AI writes a period the way the report does ("Desember 2025", "Triwulan IV 2025", "Dec 2025", "Q4 2025") → all resolve to the table's own key; Task 1 tests every form.
2. Row names with footnote marks or odd spacing ("Uang Primer Adjusted 1)", double spaces, other case) → matched on case- and whitespace-folded text only, never on words; Task 1 tests it.
3. A claim whose cells come from two tables with different units (a ratio of ULN to PDB) → each cell converted on its own table's unit; mixed units that cannot be converted → `jenis`; Task 4 tests it.
4. A contraction printed without a minus ("terkontraksi 9,0%") → the AI writes `-9,0`; the engine compares signed and a `+9,0` claim against −9,0 is Tidak Sesuai; Task 4 tests it.
5. A 254-row sheet (NPI internal) → every row reaches the prompt (the cap is 300 per table); Task 1 tests it.

---

### Task 0: Clear the uncommitted CONTEXT_EXTRACTION change

The working tree still holds the 2026-10-26 `CONTEXT_EXTRACTION` code (`structured_extractor.py`, `paired_verifier.py`, `schemas.py`, `eval/run_regression.py`, `tests/test_context_extraction.py`). It failed its gate; its useful rules (periods from the paragraph, table shape) move into the strict prompt in Task 3, and its document-wide aliases are dropped.

- [ ] **Step 1: Apply the owner's choice** — default: revert those files to `HEAD` (`git checkout -- structured_extractor.py paired_verifier.py schemas.py eval/run_regression.py`, delete `tests/test_context_extraction.py`), keep `docs/specs/context-extraction.md` as the record with a line saying it was not kept. Alternative if the owner prefers: commit it as is (flag off, already tested).
- [ ] **Step 2: Verify** — `git status --short` shows none of those files modified (revert) or one new commit (keep).

---

### Task 1: Table views (`strict/tables.py`)

**Files:**
- Create: `strict/__init__.py` (empty docstring module), `strict/tables.py`
- Test: `tests/strict/test_tables.py` (+ `tests/strict/__init__.py`)

**Interfaces:**
- Produces:
  - `@dataclass TableView: code: str; source: _ExcelSource` with properties `table`, `shape: str`.
  - `build_views(sources: List[_ExcelSource]) -> List[TableView]` — temporal, non-pointer-only, non-empty sources, codes `S1`, `S2`, … in order.
  - `period_header(year: int, month: str) -> str` — `"Q4 2025"`, `"Jun 2026"`, `"S1 2026"`.
  - `parse_period(text: str) -> Optional[Tuple[int, str]]` — accepts `"Q4 2025"`, `"Triwulan IV 2025"`, `"Tw IV 2025"`, `"Dec 2025"`, `"Desember 2025"`, `"Des 2025"`, `"S1 2026"`, `"Semester I 2026"`; returns `(2025, "Q4")`, `(2025, "Dec")`, …; `None` otherwise.
  - `find_row(view: TableView, text: str) -> Optional[str]` — the row label equal to `text` after case-folding and collapsing whitespace; `None` otherwise.
  - `table_block(views: List[TableView], chunk: str) -> str` — the prompt's table section, no values.
- Consumes: `paired_verifier._ExcelSource`, `paired_verifier._table_shape` is NOT used (it lives in the old module); write `shape` here.

- [ ] **Step 1: Write the failing tests**

```python
def test_views_get_codes_and_skip_unusable_sources(): ...
    # sources: temporal table A, pointer-only B, categorical C, temporal D
    # -> [v.code for v in build_views(...)] == ["S1", "S2"] and they wrap A and D
def test_shape_names_axis_span_and_unit():
    # quarterly Q1 2015..Q4 2025, unit "Juta USD"
    # -> view.shape == "per triwulan, Q1 2015 s.d. Q4 2025, satuan Juta USD"
@pytest.mark.parametrize("text, key", [
    ("Q4 2025", (2025, "Q4")), ("Triwulan IV 2025", (2025, "Q4")), ("Tw IV 2025", (2025, "Q4")),
    ("Dec 2025", (2025, "Dec")), ("Desember 2025", (2025, "Dec")), ("Des 2025", (2025, "Dec")),
    ("S1 2026", (2026, "S1")), ("Semester I 2026", (2026, "S1")), ("akhir tahun", None)])
def test_parse_period(text, key): assert parse_period(text) == key
def test_find_row_folds_case_and_spaces_only():
    # rows ["Uang Primer Adjusted 1)", "Aset > Investasi Lainnya"]
    # find_row(v, "uang primer  adjusted 1)") == "Uang Primer Adjusted 1)"
    # find_row(v, "Investasi Lainnya") is None   (no word matching)
def test_block_shows_rows_and_shape_but_no_values():
    # one table with value 558496.4949 -> block contains "[S1:", the title, the shape line,
    # every row, and neither "558496" nor "558.496"
def test_block_keeps_every_row_of_a_254_row_sheet(): ...
    # 254 rows -> all 254 row names in table_block(views, chunk="x")
def test_row_units_are_listed_under_the_header():
    # table.row_units = {"Ke dalam negeri": "ribuan orang"} -> "'Ke dalam negeri' = ribuan orang" in block
```

- [ ] **Step 2: Run, verify they fail**

Run: `.venv/Scripts/python -m pytest tests/strict/test_tables.py -q`
Expected: FAIL (`ModuleNotFoundError: strict.tables`).

- [ ] **Step 3: Implement `strict/tables.py`**

`table_block`: one header line per view `[S1: <title> — <shape>]`, then `Satuan khusus: '<row>' = <unit>` lines if `row_units`, then `  - <row>` per row. Row relevance per chunk reuses `structured_extractor._labels_for_chunk` (cap `LABELS_PER_SOURCE` = 300) on `[(header, rows)]`.

- [ ] **Step 4: Run, verify they pass** — same command, Expected: all PASS.

- [ ] **Step 5: Commit** — `git add strict tests/strict && git commit -m "strict: table views with codes, shape and exact row lookup (no values)"`

---

### Task 2: Claim schema and serialisation (`strict/claims.py`)

**Files:**
- Create: `strict/claims.py`
- Test: `tests/strict/test_claims.py`

**Interfaces:**
- Produces (LLM output, Pydantic):
  - `CellRef(BaseModel): table: str; row: str; period: str`
  - `StrictClaim(BaseModel)`, fields in this order (the model writes top to bottom): `anchor_quote: str` (3–8 words verbatim), `subject: str` (what is measured, copied from the sentence), `not_in_tables: bool = False`, `reason: Optional[str]`, `operation: Literal["value","yoy_growth","diff","ratio","sum","average","is_increasing","is_decreasing","is_stable","above_threshold","below_threshold","formula"]`, `cells: List[CellRef]`, `all_members: bool = False` ("seluruh/semua/N dari M": the sentence is about every member of a breakdown), `formula: Optional[str]` (grammar of `cell_formula.py`, cells `c1..cn` in `cells` order), `formula_unit: Optional[Literal["tabel","persen","jumlah"]]`, `claimed_value_raw: Optional[str]` (verbatim digits, minus sign for a fall/contraction; the threshold for above/below), `unit: Optional[str]`.
  - `StrictClaims(BaseModel): claims: List[StrictClaim]`
- Produces (internal):
  - `@dataclass Cell: code: str; row_text: str; period_text: str; row: Optional[str]; key: Optional[Tuple[int, str]]` (row/key resolved, `None` when unresolved)
  - `@dataclass Claim: operation; subject; cells: List[Cell]; context_quote: str; page_number: Optional[int]; claimed_value: Optional[float]; unit: Optional[str]; formula: Optional[str]; formula_unit: Optional[str]; all_members: bool; not_in_tables: bool; reason: Optional[str]`
  - `claim_to_dict(c: Claim) -> dict`, `claim_from_dict(d: dict) -> Claim`

- [ ] **Step 1: Failing tests** — `test_a_claim_round_trips_through_dict` (every field, including unresolved cells); `test_formula_is_an_operation` (`StrictClaim(operation="formula", ...)` validates); `test_field_order_puts_not_in_tables_before_cells` (`list(StrictClaim.model_fields)[:4] == ["anchor_quote","subject","not_in_tables","reason"]`).
- [ ] **Step 2: Run, verify fail** — `.venv/Scripts/python -m pytest tests/strict/test_claims.py -q` → FAIL.
- [ ] **Step 3: Implement** the models and the two functions (plain `dataclasses.asdict` + rebuild).
- [ ] **Step 4: Run, verify pass.**
- [ ] **Step 5: Commit** — `strict: claim schema (cells, formula, not in tables) and its serialisation`

---

### Task 3: Extraction (`strict/prompt.py`, `strict/extract.py`)

**Files:**
- Create: `strict/prompt.py`, `strict/extract.py`
- Test: `tests/strict/test_extract.py`

**Interfaces:**
- Consumes: Task 1 `TableView`, `table_block`, `parse_period`, `find_row`; Task 2 `StrictClaims`, `Claim`, `Cell`; from `structured_extractor` the text helpers `_filter_narrative`, `_split_into_page_chunks`, `_normalize_ws`, `_locate_anchor`, `_locate_by_value`, `_expand_anchor_to_sentence`, `_page_at`, `_parse_indonesian_number`, `_sig_words`, `EXTRACTION_CHUNK_CHARS`, `_PAGE_MARKER_PATTERN`.
- Produces:
  - `strict/prompt.py`: `SYSTEM_PROMPT: str`, `HUMAN_TEMPLATE: str` (placeholders `{tables_block}`, `{notes_block}`, `{narrative_text}`), `PROMPT = ChatPromptTemplate`.
  - `async extract_claims(narrative_text: str, views: List[TableView], llm, *, fallback_llm=None, notes: str = "", hints: str = "", max_chars_per_chunk: int = EXTRACTION_CHUNK_CHARS, on_progress=None, on_chunk_failed=None) -> List[Claim]`

The system prompt must say, in this order (wording is the builder's):
1. Every quantitative claim becomes one claim. Copy table codes, row names and period headers exactly from the TABLES section; the table holds them as shown.
2. Choose cells with the paragraph in view: an abbreviation means what it means in its paragraph ("KFLN" in a direct-investment paragraph is `Kewajiban > Investasi Langsung`).
3. If no table holds the data, set `not_in_tables` with a short reason; never pick a look-alike row (a breakdown, sector or series the tables lack).
4. Periods: relative ones count from the period the paragraph was just about, forecasts included ("Setelah pada triwulan sebelumnya…" after a Q2 2026 forecast is Q1 2026). A "keseluruhan tahun" paragraph compares end of year with end of previous year. A full-year figure on a table with no annual column is `not_in_tables`.
5. Operations and the cells each needs: `value` 1; `yoy_growth` the growth table's 1 cell when the table is a growth table, else 2 (same period a year apart, earlier first); `diff` 2 (earlier, later); `ratio` numerators then the denominator; `sum`/`average` ≥2; trends ≥2 consecutive periods per row, oldest first; thresholds ≥1, `claimed_value_raw` = the fixed reference point (PMI 50, SBT 0, IKK 100). A sentence the menu cannot say → `formula` over `c1..cn` with `formula_unit`, plus `claimed_value_raw` for the number it must equal (none for a condition).
6. Several rows in one sentence: list every cell; `all_members` only when the sentence covers every member ("seluruh", "semua", "12 dari 18 kota").
7. Numbers verbatim, minus sign for a fall or contraction; mtm/qtq are `value` on their own table.

`HUMAN_TEMPLATE`: `{notes_block}` (publication notes, then `hints`: the team's aliases as `"phrase" → 'row'`), `TABLES:` + `{tables_block}`, `NARRATIVE TEXT:` + `{narrative_text}`.

`extract_claims` finalises each raw claim: number via `_parse_indonesian_number` (skip the claim when `claimed_value_raw` is required and unparseable: every operation except trends, `formula` with a condition, and `not_in_tables`); sentence and page as `structured_extractor._finalize_facts` does (anchor → `_locate_anchor`, fallback `_locate_by_value`, then `_expand_anchor_to_sentence`); each cell resolved with `find_row` / `parse_period` against the view whose code it names.

- [ ] **Step 1: Failing tests** (fake LLM as in `tests/test_context_extraction.py`: a `Mock` whose `with_structured_output` returns a `RunnableLambda` answering a fixed `StrictClaims`):
  - `test_the_prompt_has_the_tables_without_values` — captured human message contains `"[S1:"` and the shape, not the cell value.
  - `test_cells_resolve_to_rows_and_periods` — raw cell `{"S1", "aset > investasi lainnya", "Triwulan IV 2025"}` → `Cell(row="Aset > Investasi Lainnya", key=(2025, "Q4"))`.
  - `test_an_unknown_code_row_or_period_stays_unresolved` — `row=None` / `key=None`, claim kept.
  - `test_the_sentence_and_page_come_from_the_text` — `context_quote` is the full sentence, `page_number` from the `[== Halaman N ==]` marker.
  - `test_a_contraction_keeps_its_minus` — `claimed_value_raw="-9,0"` → `claimed_value == -9.0`.
  - `test_not_in_tables_needs_no_cells_or_number`.
  - `test_notes_and_hints_reach_every_chunk` — two chunks, both human messages contain the notes and `"\"ipr\" → 'INDEKS TOTAL'"`.
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement** prompt and extractor.
- [ ] **Step 4: Run, verify pass.**
- [ ] **Step 5: Commit** — `strict: extraction prompt and claim finalisation`

---

### Task 4: Evaluation (`strict/evaluate.py`)

**Files:**
- Create: `strict/evaluate.py`
- Test: `tests/strict/test_evaluate.py`

**Interfaces:**
- Consumes: Task 1 `TableView`; Task 2 `Claim`, `Cell`; `cell_formula.evaluate`, `cell_formula.FormulaError`; the allowed `paired_verifier` arithmetic helpers (Global Constraints).
- Produces: `evaluate_claim(claim: Claim, views: List[TableView]) -> FactVerificationResult`

Result fields: `operation` (as claimed), `metric_label` = `claim.subject`, `matched_excel_source` = `"<filename> / <sheet>"` of the first cell's table, `periods` = one `PeriodResult(metric_label=row, year, month, excel_value)` per cell, `claimed_value`, `claimed_unit`, `computed_value`, `delta`, `verdict`, `reasoning`, `context_quote`, `page_number`, `resolved_via="strict"`, `reason_code` (Inconclusive only). Reasoning always lists the cells as `S1 · <row> · <period> = <value>` and, for a formula, the formula and its result.

Rules (order matters):
1. `not_in_tables` → Inconclusive `di_luar_tabel`, reasoning "Menurut AI, data ini tidak ada di tabel yang diunggah: <reason>".
2. No cells → `klaim_tidak_lengkap`. A cell with unknown code or row → `tidak_ditemukan`; unknown period or empty cell → `periode`. Reasoning names the bad reference as the AI wrote it.
3. `all_members` and the cited rows leave out a row with the same parent (text before the last `" > "`) and the same depth in that table → `klaim_tidak_lengkap`, reasoning names the missing rows.
4. Units: the claim's number is put in each cell's table units with `_unit_factor`; when it gives `None` and the table unit has no scale (`_parse_scale_unit(unit) is None`), scale only the claim (`_parse_scale_unit(claim.unit)` factor, else 1); otherwise `jenis`. Ratios, growth, thresholds, trends and `formula_unit` `persen`/`jumlah` need no conversion.
5. Per operation: `value` → `_value_verdict`; `yoy_growth` 1 cell → `_value_verdict`, 2 cells → `(c2/c1 - 1) * 100` against `_growth_tolerance`; `diff` → `c2 - c1`; `sum`, `average`, `ratio` (`sum(numerators) / denominator * 100`) → `_numeric_verdict`; trends → every consecutive pair per row rises / falls / stays within `_stable_band(a, b, net_balance)` (net balance when the row contains "saldo bersih" or the unit contains "SBT"); thresholds → every cell `>` / `<` the claimed bound; `formula` → `cell_formula.evaluate` on the cell values: a bool is the verdict, a number is compared with `_numeric_verdict` (after converting the claim when `formula_unit == "tabel"`); a `FormulaError` → Inconclusive `klaim_tidak_lengkap`.

- [ ] **Step 1: Failing tests** (synthetic `BITableData` sources; each Sesuai case has a Tidak Sesuai twin):
  - `test_value_rounds_as_printed` (558,5 vs 558.496,49 juta USD with unit "miliar USD" → Entailed; 558,6 → Refuted)
  - `test_yoy_from_two_levels_and_from_a_growth_cell`
  - `test_diff_is_signed` (claimed −16,8 vs a fall of 16,8 → Entailed; +16,8 → Refuted)
  - `test_ratio_sums_numerators`, `test_sum_and_average`
  - `test_trends_per_row`, `test_stable_uses_the_band`, `test_thresholds_every_cell`
  - `test_formula_count_and_condition` (`(c2<c1)+(c4<c3)` = 2 → Entailed, claimed 1 → Refuted; `c2<0 and abs(c2)<abs(c1)` true/false)
  - `test_formula_outside_the_grammar_is_inconclusive` (`"__import__('os')"`)
  - `test_not_in_tables_unknown_row_and_bad_period_reasons`
  - `test_all_members_must_list_every_member` (3 of 18 cities with `all_members=True` → `klaim_tidak_lengkap`; same 3 with `all_members=False` → judged)
  - `test_cells_from_two_tables_convert_each_unit` and `test_units_that_cannot_be_compared_are_jenis` (Review Focus 3)
  - `test_a_contraction_claimed_positive_is_refuted` (Review Focus 4)
  - `test_reasoning_lists_the_cells`
  - `test_strict_imports_no_matching_or_guards` — parse every `strict/*.py` with `ast`; names imported from `paired_verifier` ⊆ the allowed set.
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement** `evaluate_claim` and small private helpers per rule.
- [ ] **Step 4: Run, verify pass.**
- [ ] **Step 5: Commit** — `strict: evaluate claims on their cited cells`

---

### Task 5: Pipeline and the hand-over (`strict/pipeline.py`, `paired_verifier.py`, `schemas.py`, `static/index.html`)

**Files:**
- Create: `strict/pipeline.py`
- Modify: `paired_verifier.py` (one block in `verify_paired`, right after step 1a `_remember_rows(...)`), `schemas.py` (`FactVerificationResult.operation` gains `"formula"`; `PairedVerificationResponse.strict_claims: List[Dict[str, Any]] = Field(default_factory=list)`), `static/index.html` (`TYPE_LABEL.formula = "Rumus"`)
- Test: `tests/strict/test_pipeline.py`

**Interfaces:**
- Consumes: Tasks 1–4; `ai_review.review_claims`; `publication_parsers.notes_for`, `skipped_sections`; `structured_extractor.drop_sections`.
- Produces:
  - `enabled() -> bool` (flag, Global Constraints)
  - `applies(sources: List[_ExcelSource], mode: str, has_pdf_tables: bool) -> bool`
  - `async run_check(narrative_text, sources, llm, *, pdf_filename, vision_llm, publication, team_aliases: Dict[str, str], team_notes: str, review_llm, review_new_refuted, progress_cb, on_extract_gap, excel_parsers: List[str]) -> PairedVerificationResponse`

`run_check`: views → `extract_claims` (primary `vision_llm or llm`, fallback `llm` when a vision model is given; `notes = notes_for(publication)`; `hints` from `team_aliases`; narrative passed through `drop_sections(narrative, skipped_sections(publication))`) → `evaluate_claim` each → Tinjauan AI via `review_claims(results, [v.source for v in views], review_llm, new_refuted=..., team_notes=...)` when `review_llm` is set (failure keeps the results, as today) → counts → response with `strict_claims=[claim_to_dict(c) for c in claims]`, empty `chart_checks`, `ai_aliases=[]`. Emits the same progress stages as today (`extract`, `compare`).

Hook in `verify_paired`: `if strict.pipeline.enabled() and strict.pipeline.applies(parsed_sources, mode, bool(pdf_tables or reference_tables)): return await strict.pipeline.run_check(...)`.

- [ ] **Step 1: Failing tests**
  - `test_off_verify_paired_is_unchanged` — flag unset, mocked old extractor called, `strict_claims == []`.
  - `test_on_a_check_goes_strict` — flag on, fake LLM answering one `value` claim on `S1` → one Entailed result with `resolved_via == "strict"` and one `strict_claims` entry; the old `extract_structured_facts_async` is never called.
  - `test_on_but_internal_mode_stays_old` — `mode="internal"` → old path.
  - `test_team_aliases_become_hints_not_substitutions`.
  - `test_a_review_failure_keeps_the_verdicts`.
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement** pipeline, hook, schema fields, UI label.
- [ ] **Step 4: Run, verify pass**, then the old-path suites: `.venv/Scripts/python -m pytest tests/test_paired_verifier.py tests/test_main_paired_endpoint.py tests/test_main_paired_stream_endpoint.py tests/test_eval.py -q` → all PASS.
- [ ] **Step 5: Commit** — `strict: pipeline behind STRICT_EXTRACTION, handed over from verify_paired`

---

### Task 6: Regression runner (`eval/run_regression.py`)

**Files:**
- Modify: `eval/run_regression.py` (`run`: `--strict` sets `STRICT_EXTRACTION=1`; `replay`: a saved body with `strict_claims` is re-judged by the strict evaluator)
- Test: `tests/test_eval_regression.py` (add)

**Interfaces:**
- Consumes: Task 1 `build_views`; Task 2 `claim_from_dict`; Task 4 `evaluate_claim`; existing `_parsed_sources(doc, data_root)`.
- Produces: replay output in the same format as today (`{"results": [...], "_replay": {...}}`).

- [ ] **Step 1: Failing test** — `test_replay_rejudges_strict_claims`: a run body holding `strict_claims` for one `value` claim and a manifest whose source parses to a known table → the replayed result is Entailed with `resolved_via == "strict"`, and no `_facts` are needed.
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement** the flag and the replay branch.
- [ ] **Step 4: Run, verify pass.**
- [ ] **Step 5: Commit** — `eval: run and replay strict checks`

---

### Task 7: Safety check and measurement

- [ ] **Step 1: Full suite** — `.venv/Scripts/python -m pytest tests/ -q` → all PASS.
- [ ] **Step 2: Free replay, flag off** — base = `git worktree add <scratch>/wt_base <commit before Task 1>`; `bash tools/replay_all.sh <wt_base> <scratch>/ra_base` and `bash tools/replay_all.sh . <scratch>/ra_fix`; `tools/vdiff.py` per run → zero changed verdicts. Remove the worktree.
- [ ] **Step 3: Ask the owner for the paid run**, quoting the estimate (about Rp35 ribu: one control and two strict runs, 19 third-edition documents, no Tinjauan AI) and the running credit.
- [ ] **Step 4: After the OK** — runs `ctrl_strict`, `strict1`, `strict2` with the case list and `--only` filters of the 2026-10-26 context runs (`skdu-sp`, `npi-sp`), `--no-ai-review`, plus `--strict` for the two strict runs; score each with `tools/inventory/score.py --run <name>` summed over the 19 documents; tokens from the `perf … in=… out=…` log lines.
- [ ] **Step 5: Report** verdicts right, false Tidak Sesuai (gate: not above control), false Tidak Cukup Data, real errors caught (gate: not fewer), claims missed, `not_in_tables` share, tokens and cost, and the list of new false Tidak Sesuai with their cells. Recommend default-on or not; the owner decides.
