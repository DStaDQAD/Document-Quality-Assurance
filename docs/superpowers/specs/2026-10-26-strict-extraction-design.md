# Strict extraction: the AI names the cells, the engine only computes

Status: design, agreed in conversation with the owner on 2026-10-26, awaiting their read of this
document. Nothing is built yet. Supersedes the alias part of `docs/specs/context-extraction.md`.

## Why

The architecture the owner describes is simple:

1. read the document's text;
2. an LLM turns each claim into JSON, naming the table row it is about, or saying it is not in
   the tables;
3. code reads the table and computes;
4. the operation can be anything the sentence needs, built by the AI from fixed blocks;
5. code decides Sesuai / Tidak Sesuai / Tidak Cukup Data.

The system that exists departs from that at step 2, and most of its complexity follows from there:

- The extractor is told: *copy a row name when one clearly fits, otherwise write the report's own
  words*. On the third editions (run `ctrl_1026`) only about 45 % of metric names were exact row
  names. The other 55 % ("KFLN", "PDB", "defisit neraca jasa") were left for the engine to match.
- The engine matches by words. Word matching lands on look-alike rows ("IKK" → "IKK > SMA",
  "penjualan tipe kecil" → the price index, "KFLN" → total liabilities).
- Each wrong match that produced a false Tidak Sesuai got a **guard** that turns a suspicious match
  into Tidak Cukup Data. Guards then block true claims, which got exceptions. `paired_verifier.py`
  is now 6.175 lines and 171 functions, most of it this loop.
- The extractor never sees how a table is laid out (quarterly or monthly, which periods exist,
  the unit), so period conversions were rebuilt in the engine too.
- Engine changes are free to test (replay), extraction changes cost money (a paid run). So fixes
  went where testing is cheap — the engine — rather than where the problem is.

The owner does not want more guards. The aim is to make step 2 do its job, so the engine has
nothing to guess and most guards have nothing to guard.

## What changes

### Step 2: the extractor sees every table's shape and names cells

- **Every table is shown whole, without values** (owner's decision). For each table: a code
  (`S1`, `S2`, …), the title, the unit, the period axis with its first and last period
  ("per triwulan, Q1 2015 s.d. Q4 2025"), and every row name with its hierarchy (as today, up to
  300 rows). Row units where a row differs from the table (NPI "Jumlah pelawat (ribuan orang)").
  - *Why no values:* with the numbers in view, the model can hunt for the row whose value matches
    the printed number, which would hide exactly the report errors the tool exists to find.
- **Each claim names its cells.** Instead of a free `metric_label`, a claim lists the cells it
  needs: `{table: "S1", row: <exact row name>, period: <exact period, e.g. "Q4 2025">}`. The row
  is chosen with the paragraph in view, per claim: "KFLN" in the direct-investment paragraph is
  `Kewajiban > Investasi Langsung`, the same word in the summary is `Kewajiban`. There is no
  document-wide alias dictionary.
- **The report's own words are kept** in a separate field (`subject`, copied from the sentence),
  only for display and for the reader to judge the AI's choice.
- **"Not in the tables" is an answer.** A claim whose data no table holds says so
  (`not_in_tables: true` plus a short reason). It comes back Tidak Cukup Data with that reason.
  Nothing is forced onto a look-alike row.
- **Periods are written as the table has them.** Relative periods count from the paragraph
  ("triwulan sebelumnya" after a Q2 forecast is Q1). A full-year figure on a table with no annual
  column is `not_in_tables`, never quietly Q4 (the NPI "defisit tahun 2024" error of 2026-10-26).
- **Operations.** The menu stays (`value`, `yoy_growth`, `diff`, `ratio`, `sum`, `average`,
  trends, thresholds). For anything the menu cannot say, the claim carries a `formula` over its
  cells `c1..cn`, using the grammar the Tinjauan AI already uses (`cell_formula.py`: numbers,
  cells, + − × ÷, comparisons, and/or/not, abs/sum/min/max/round — parsed, never `eval`'d), plus
  the number or condition it must reproduce. Examples: a component's net change
  `(c4-c3)-(c2-c1)`, a share `(c1+c2)/c3*100`, a count `(c2<c1)+(c4<c3)+…` = 12, "kontraksi yang
  lebih rendah" `c2<0 and abs(c2)<abs(c1)`.
- **Groups are listed, not described.** "Seluruh kota" lists every city's cells (the AI sees the
  rows). The engine checks that a group claim lists every member of that breakdown, as the
  Tinjauan AI's formula check already does.
- **Per-publication notes** (`publication_parsers/*.notes.yaml`) go into the prompt in this mode.
  The conventions that today live in engine guards (SBT "kondisi baik" = above 0, PII net
  liabilities are Kewajiban − Aset, SKDU's saldo bersih rows) become guidance for the AI's choice
  of cells and operation, written once per publication.

### Step 3: the engine reads the named cells and computes

A new evaluator for strict claims, next to `_evaluate_fact`:

- Read every cited cell from the parsed table by exact key. A table code, row or period that does
  not exist → Tidak Cukup Data, reason `tidak_ditemukan` / `periode`, with the bad reference shown.
- Compute the operation or formula. Reuse what exists: unit conversion (`_unit_factor`), the
  rounding rules (`_numeric_verdict`, `_growth_tolerance`, `rounding_rule_changes`: levels round as
  printed, a derived change is held to `MATCH_TOLERANCE`), signed comparison (a fall is negative),
  the formula evaluator, and the Tinjauan AI's cell reader (`ai_review.read_cells`) and
  `_evaluate_on_cells` as the model to follow.
- **No word matching and none of the row guards** on this path: no one-group, narrower-series,
  source-about-the-sentence, section, absent-item, unnamed-total or number-phrase guard. They exist
  to catch wrong matches, and there is no match here. Rules about *arithmetic* stay (units,
  rounding, sign).
- Every result shows its cells: `S1 · Kewajiban > Investasi Langsung · Q4 2025 = 347.640,46` and
  the formula, so a wrong choice is visible at once.
- The Tinjauan AI keeps reviewing Tidak Sesuai (and Tidak Cukup Data) as today. Its cited cells
  are checked by code as today.

### What stays as it is

- Reading Word/PDF, the narrative filter, chunking (about 6.000 characters per call).
- The parsers (they produce the tables the AI names).
- Charts, PDF-internal tables and categorical (non-time-series) tables in the first version: those
  sources are not shown with codes, and claims about them keep the current path. Only uploaded
  Excel time-series tables are in scope.
- The current path itself, behind the flag, until the measurement says to drop it.

## Flag and rollout

- `STRICT_EXTRACTION`, off by default. Off: everything exactly as today (tested the same way as
  `CONTEXT_EXTRACTION`: identical prompt and schema, and a free replay of every case's latest run
  with zero changes).
- On: the strict prompt and schema, and every claim with cells goes to the new evaluator.
- `CONTEXT_EXTRACTION` (built 2026-10-26, failed its gate) is folded in: its rule 10 (periods from
  the paragraph, which removed the PMI false Tidak Sesuai) and rule 12 (table shape) become part of
  the strict prompt; its document-wide aliases are dropped.
- If the measurement holds, strict becomes the default in code (no Render variable), and the
  guards the strict path no longer needs are removed in a later, separate change.

## Measuring

- **Documents:** the 19 third-edition documents (labels exist), then a fourth edition as the real
  held-out test.
- **Runs:** one control (flag off) and two strict runs on the same day, no Tinjauan AI (so the
  extraction itself is measured), scored with `tools/inventory/score.py --run`.
- **Gate:** false Tidak Sesuai must not rise above the control, and real errors caught must not
  fall. Beyond that: verdicts right, false Tidak Cukup Data, claims missed, and cost.
- **Cost estimate:** input grows little (the row lists are already sent; one line per table plus
  the notes, about 800 tokens a chunk). Output grows: each claim names its cells, about 50 more
  tokens a claim, and some carry a formula. Expect +30–50 % on extraction, roughly Rp10–12 ribu a
  pass for the 19 documents, about Rp35 ribu for the three runs. Announced and approved before
  running.
- Saved facts carry their cells, so once a strict run exists, engine changes on the strict path
  are replayed for free.

## Risks

- **A wrong cell gives a wrong verdict.** There is no guard behind it. Mitigations: the cells are
  always shown; the Tinjauan AI reviews every Tidak Sesuai; "not in the tables" is a valid answer;
  the gate is measured on labels before anything changes for the team.
- **More output tokens**, so higher cost per check (estimated above, measured in the first run).
- **Long reports with many tables** (NPI: 254 rows in one sheet) make the row list long. The
  existing per-chunk relevance cut (`LABELS_PER_SOURCE`, `_labels_for_chunk`) stays.
- **The model may still hedge** (write a near row instead of `not_in_tables`). Measured by the
  "right by luck" and false-Sesuai counts.

## Tests (first, before the code)

- Flag off: the prompt, the schema and every verdict are today's.
- The table block shows codes, unit, period axis and rows, without values.
- Strict evaluator, one test per operation on synthetic tables, each with a counterpart that must be
  Tidak Sesuai; unknown table/row/period → Tidak Cukup Data with the reason; a formula outside the
  grammar is rejected; a group claim that leaves out members is not judged on the members it named.
- A strict fact round-trips through `fact_to_dict` and replay.

## Decided by the owner

- No more guards as the way forward; step 2 must do the work (2026-10-26).
- The AI sees every table's structure without cell values (2026-10-26).
- A wrong cell may give a wrong verdict, as long as the cells are visible and Tidak Sesuai is
  reviewed (2026-10-26, implied by "iya" to that trade-off — to confirm on reading this).

## Where the code goes

A new package, `strict/`, written clean from the start: small modules, clear interfaces, its own
tests (owner, 2026-10-26: the codebase is too tangled for a coding agent to work in well, but a
fresh repository would throw away the parts that work). It imports only the solid parts — text
reading (`word_extraction`, `pdf_extraction`), the parsers (`publication_parsers`,
`excel_parser_bi`, `table_parser_generic`, `table_model.TableData` as a data holder),
`cell_formula`, `llm_provider` — and never `paired_verifier`'s matching or guards.
`paired_verifier.py` is not edited beyond the one place that hands a check to `strict/` when the
flag is on. Once strict passes its gate and becomes the default, the old path and its guards are
deleted.

## Not in the tables, or a cell that does not exist

Tidak Cukup Data straight away, with the reason and the bad reference shown; no second try with
word matching (Claude's recommendation, taken as the default when the owner agreed the plan;
reversible). This keeps the measurement about the AI alone and keeps the guesses out.

## Calls made in writing this (Claude's, for the owner to check)

- **Growth and trends cite every cell they use.** A yoy claim cites the growth table's cell when one
  exists (SPE '%, yoy'), else the two level cells (same period a year apart). A one-period trend
  cites the previous period too. The engine no longer completes baselines on its own.
- **The team's aliases (Pengaturan) are shown to the AI as hints** in this mode ("phrase → row"),
  instead of being applied by code.
- **The cell pointer (tier 4) does not run for strict claims**: the AI has already pointed.
- **Strict needs at least one uploaded Excel time-series table**; a check without one (PDF-internal
  mode) runs as today.

## Left to the builder

- Field names and the exact prompt wording.
- How table codes are assigned when several sheets share rows (`_merge_sources_sharing_rows`).
- Whether menu operations are evaluated by mapping them onto formulas or by the existing helpers.
