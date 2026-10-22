# Publication notes for the extractor

Status: draft for review, 2026-10-22
Inventory it builds on: `.scratch/publication-notes/rules-inventory.md`

## Problem

The extractor reads every report with the same prompt ("Bank Indonesia M2 publications"), and is never told which publication it is reading. What a phrase means in one publication is therefore patched into the general verifier after the fact. "SBT tetap tumbuh" is a level above 0, "fase ekspansi" is an index above 50, and "defisit 12,5" is the size of a negative cell. About 55 such rules live in `paired_verifier.py` / `table_model.py`, and every report sees the other publications' examples in the prompt.

## Goal

The extractor gets the conventions of the one publication the checker picked. It writes claims the way that publication means them: the right operation, row, period and sign. The verifier then has fewer meanings to guess.

## Rules (owner's)

- **The notes guide, they never block.** Nothing reads them to refuse, abstain or mark a claim "belum dipetakan". A sentence the notes do not cover is extracted exactly as today.
- **Behind a flag, default off.** With the flag off, the prompt is byte-for-byte today's.
- **Notes are written from editions 1-2 and the inventory only.** Nothing comes from a held-out third edition until it has been scored. See "Open points" for SULNI.

## What a publication's notes contain

Four sections. Each entry is one short line the model can act on, in English, with the report's Indonesian phrases quoted as written.

1. **conventions**: what a phrase means as a claim.
   - "tetap tumbuh / terjaga / masih baik" on an SBT row → `above_threshold 0`, not `is_increasing`.
   - "fase ekspansi / kontraksi" → `above/below_threshold 50`. It is never a direction.
2. **phrases**: usual report wording → the row it means, in the table's own spelling.
   - "persepsi konsumen terhadap ketersediaan lapangan kerja saat ini" → 'Indeks Ketersediaan Lapangan Kerja (IKLK)'.
   - The parser's existing `TERM_ALIASES` are appended automatically, so they are written once.
3. **periods**: how the publication dates its figures.
   - "semester II 2026" → month 'S2' (SKDU Tabel 7b-10).
   - "akhir Juni" on a quarterly row → 'Q2'.
4. **units_signs**: units, and the sign the tables store.
   - NPI is BPM6: 'Impor' and 'Pembayaran' rows are negative, so "defisit 12,5" is a value of 12,5 on a negative balance; keep the number unsigned.
   - PMI prints the index as a percent ("52,03%").

Not in the notes:
- verdict rules (tolerances, stability bands): those stay in code;
- anything the owner has not decided (the open questions in `eval/inventory/OPEN_QUESTIONS.md`);
- a list of allowed claims.

**Size cap:** about 600 words per publication. The notes go into every chunk's prompt, so 600 words is roughly 800 tokens × chunks. An NPI laporan of about 10 chunks adds about 8k input tokens, roughly Rp40 per check.

## Where they live

One file per publication, next to its parser: `publication_parsers/<module>.notes.yaml`, for example `publication_parsers/skdu.notes.yaml`.

```yaml
publication: skdu
conventions:
  - '"tetap tumbuh", "terjaga", "masih dalam kondisi baik" on an SBT/SB row: the balance is above 0 -> above_threshold 0, not a rise.'
  - '"bersumber dari LU A (SBT a%)": each named LU is a value fact (a%), and its balance is above 0.'
phrases:
  - '"SBT harga jual <LU>" -> Tabel 5 (Harga Jual), not Tabel 1.'
periods:
  - '"semester I/II <year>" -> month S1/S2. Never Q1/Q2 for a semester.'
units_signs:
  - 'SBT, SB and saldo bersih figures are levels in percent points (unit "persen"), never a yoy growth.'
```

YAML rather than free text, so that a test can check the four section names, the size cap, and that the file loads. It is read by `publication_parsers.notes_for(publication) -> str`, next to the existing `skipped_sections()`. A publication without a file gets "".

## How only the chosen publication's notes reach the prompt

1. `verify_paired` already receives `publication`. When the flag is on, it calls `notes_for(publication)` and passes the text to `extract_structured_facts_async(..., publication_notes=text)`. That is a new optional argument, default "".
2. `_HUMAN_TEMPLATE` gains one block before the REFERENCE METRIC LIST, only when the text is non-empty:

   ```
   PUBLICATION: <title> — notes from this publication's analysts. They say what its usual
   wording means; they do not limit what you extract.
   <rendered notes>
   ```

   It goes in the human message, not the system prompt. The system prompt stays identical for every publication, so Gemini's implicit prompt cache keeps working.
3. **The flag:** environment variable `PUBLICATION_NOTES=1`, read like `AI_REVIEW`. `eval.run_regression run` gets `--publication-notes` to set it for a run. Off, or an empty publication: no block, and the prompt is unchanged.
4. Every chunk of a report gets the same notes. No other publication's notes are ever loaded.

The Tinjauan AI does not get the notes in this first version. If step 4 shows the extractor gains, the same block can be added to the review prompt as a separate, measured change.

## Tests (test-first)

- Flag off: the rendered prompt for a chunk is byte-identical to today's (snapshot).
- Flag on, `publication="pmi"`: the prompt contains PMI's notes and no other publication's.
- Flag on, unknown or empty publication: no block.
- Every `*.notes.yaml` loads, has only the four sections, and is within the size cap.
- `TERM_ALIASES` of the publication appear under phrases.
- Nothing outside the prompt builder reads the notes. A grep test keeps verdict code from depending on them.

## Measurement (step 4)

Third editions with AI review on: flag off (the run of 2026-10-22) vs flag on. Scored against the labels by sentence + printed number. Report per publication: verdicts right, right by luck, not real claims, missed claims. A publication's notes are kept only where they help measurably. Only then do its verifier patches move into its notes (step 5), one publication at a time, with a replay.

## Open points for the owner

1. **SULNI's "triwulan II = June column"** was seen on SULNI Agu, a held-out edition. Writing it into SULNI's notes makes SULNI Agu no longer a fair test. The options are: leave it out of the first notes, or accept SULNI Agu as seen and use a fourth SULNI edition (Juli) as the test.
2. **Prompt passages that name one publication** (PMI's 50, SBT's 0, IKK's 100, UB's DPK rules, NPI's rule 2g, PII's AFLN examples) stay in the shared prompt for now. Moving them into the notes changes the flag-off prompt, so it belongs to step 5, not this step.
3. **Who edits the notes:** you, or analysts later. If analysts, the loader's error messages should be in Indonesian.
