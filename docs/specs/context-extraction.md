# Context extraction (CONTEXT_EXTRACTION)

Agreed with the owner 2026-10-26. Status: **not kept.** Built behind the flag and measured the same day (it failed: false Tidak Sesuai 14 → 17/18); the code was reverted. Its period and table-shape rules moved into strict extraction (`docs/superpowers/specs/2026-10-26-strict-extraction-design.md`); its document-wide aliases were dropped.

## Why

The extractor already reads about 6.000 characters per call (several paragraphs). Context is lost
in two other places: when that understanding is squeezed into one claim (row, period, operation),
and when the engine judges the claim with its own sentence only. Third-edition examples:

- PMI Tw I s021: "Setelah pada triwulan sebelumnya berada pada fase kontraksi" follows a sentence
  about the Q2 2026 forecast, so it means Q1 2026; the extractor dated it Q4 2025.
- PII Tw IV: changes in a "keseluruhan tahun 2025" paragraph were read quarter on quarter.
- "AFLN investasi lainnya" and similar names never reached their rows until a parser alias was
  written by hand (5ce1dec). Every new edition brings a few such names.
- NPI: "6,2 bulan impor" was dated December on a quarterly table.

## What the flag does

1. **AI aliases (rule 11).** Each chunk's answer also lists `aliases`: a metric_label the extractor
   wrote that is not a row name but that it is sure names a row (an abbreviation the text spells out,
   a publication's fixed name). Code keeps only those whose row a table has exactly, drops a phrase
   given two rows, never overrides a parser's or the team's alias, and applies them as label aliases
   (whole metric name only) for **this check only** (owner's choice: used immediately, not proposed
   for approval). Each result that used one says so in its reasoning ("Padanan AI: 'x' → 'y'"), and
   the response lists them in `ai_aliases`. They are not saved to Pengaturan.
2. **Relative periods from the paragraph (rule 10).** "triwulan/bulan sebelumnya" counts back from
   the period the text was just talking about (possibly a forecast); a "keseluruhan tahun" paragraph
   dates its changes from the end of the previous year.
3. **Table shape (rule 12).** Each table's header in the prompt says its period axis, first and last
   period and unit ("per triwulan, Q1 2015 s.d. Q4 2025, satuan Juta USD"), so claims are dated the
   way the table is laid out.

With the flag off the prompt, the schema and every verdict are exactly as before (tests in
`tests/test_context_extraction.py`; free replay of every case's latest run unchanged).

## Not in this round

The engine reading the previous sentence for its guards (SK "Bandung, Pontianak, dan Samarinda"
tested on all 18 cities; SPE "beberapa kota, terutama Medan"). Those are engine rules, measured by a
free replay, and come as a separate change.

## Cost

Rules 10-12 add about 500 input tokens per chunk (system prompt, cacheable) and one line per table;
aliases add a few hundred output tokens per chunk. On the 19 third-edition documents (~32 chunks)
that is roughly +5-10 % of an extraction run's cost, about Rp30-70 per document.

## Measuring

Paid: the 19 third-edition documents, `eval.run_regression run --context-extraction`, two runs, plus
one control run without the flag on the same day (LLM variance makes old runs a poor baseline).
Scored against `eval/inventory/labels` with `tools/inventory/score.py --run`. Gate: false Tidak
Sesuai must not rise and real errors caught must not fall. If it holds, the flag becomes the default
in code (no Render variable).
