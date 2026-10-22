# Composed claims: the extractor builds an operation the menu lacks

Status: agreed with the owner 2026-10-22. No flag: normal behaviour once tested (unit tests, free replay of every saved case, a paid run on a fourth edition with the cost OK'd first). Count sentences are judged.

## Problem

The extractor must give every claim one operation from a fixed menu (`value`, `yoy_growth`, `diff`, `ratio`, `sum`, `average`, `is_increasing`, `is_decreasing`, `is_stable`, `above/below_threshold`), plus a group marker (`all`, `majority`, `rest`). Each operation is a fixed bundle of four choices:

| Choice | Examples |
|---|---|
| which rows | one named row, all cities, the rest, … |
| what is measured | level, yoy, mtm, qtq, change, size of a negative balance |
| compared with what | previous period, same period last year, a threshold, a printed number |
| how the rows are summed up | each, all, majority, **12 of 18** |

Writers combine these freely. When a sentence's combination is not on the menu, the extractor picks the nearest operation. The engine then judges that operation, not the sentence. Each new edition brings a few such sentences, and so far each was fixed by teaching the engine one more combination.

Examples from the third editions (2026-10-22):

- SHPR Tw IV-2025: *"dari 18 kota yang disurvei, 12 kota diantaranya mengalami perlambatan pertumbuhan dan 2 kota mengalami penurunan IHPR secara tahunan"*. This is a count over all cities of the yoy rate. It came back as `is_decreasing` on the national index and was refuted.
- SULNI Agustus 2026: *"kontraksi pertumbuhan ULN swasta yang lebih rendah"*. The yoy rate is still below 0 but smaller in size. It came back as `is_decreasing` on the level and was refuted. An operation for this one combination was added in 456faf0.
- SPE Juli 2026: *"tekanan inflasi pada Oktober 2026 dan Januari 2027 diprakirakan menurun"*. Each target month's expectation is lower than the previous survey's expectation for the same month. It came back as a trend Oct → Jan.

## Goal

When no menu operation captures the sentence, the extractor **composes one** from fixed building blocks, and the engine evaluates it. A new combination then needs no new code, only blocks that already exist.

## Rules

- **The AI composes, the program computes.** The extractor only names rows, periods, a measure, a comparison, a test and a summary. Every number is read from the table and every calculation is done by the engine. The AI never writes code, and never states a computed number as the answer.
- **The menu stays.** About 90% of claims fit it, and it is proven. `composed` is only for a sentence the menu cannot say. The prompt tells the extractor not to force such a sentence into the nearest operation.
- **No new abstain.** A composition the engine cannot evaluate (unknown row, missing period) comes back Tidak Cukup Data with the reason, like any claim today. Nothing is refused for being composed.
- **No flag** (owner, 2026-10-22). It ships as normal behaviour, but only after the tests and the measurement below. If the measurement shows it makes things worse, it does not ship.
- **A composition that fails is Tidak Sesuai**, with its reading printed. Like every Tidak Sesuai, it goes to the Tinjauan AI, whose cited cells the engine re-checks. A wrong composition can still be overturned there.
- **Shown to the reader.** A composed claim's result spells out how it was read, e.g. "18 kota, pertumbuhan yoy Q4 vs Q3, lebih rendah: 12 dari 18". An analyst can see a wrong reading at once.

## The building blocks

One new operation value, `composed`, with one new field, `composition`:

```
composition:
  rows:     {named: [labels]}                       the rows the sentence names
          | {all_of: <one member's label>}          every sibling of that row (all cities)
          | {rest_of: <member label>, except: [..]} the siblings not named
  measure:  level | yoy | mtm | qtq | change | size
  at:       [{year, month}, …]                      the claim's period(s); with several, each is
                                                    tested on its own against its own `compare`
  compare:  previous_period | same_period_last_year | {period: {year, month}} | none
  test:     a condition on `now` and `before` (the measure at `at` and at `compare`):
            now > before, now < 0, abs(now) < abs(before), now >= 50, now == 12,3 …
            joined with `and` / `or`
  summary:  each | all | majority | none | {count: 12} | {at_least: 3}
```

- `size` is the magnitude of a balance the table stores negative (defisit, kewajiban neto).
- `test` is parsed by a small grammar with only `now`, `before`, numbers, `abs()`, comparison signs, `and`, `or`. It is **not** Python `eval`, and nothing else is accepted.
- An `==` against a printed number uses the existing rounding rule (`rounding_rule_changes`: levels round as printed, a change printed without decimals is not widened).

The three examples, composed:

```
SHPR "12 dari 18 kota … mengalami perlambatan pertumbuhan":
  rows {all_of: "Medan"}, measure yoy, at Q4 2025, compare previous_period,
  test "now < before", summary {count: 12}
SHPR "… dan 2 kota mengalami penurunan IHPR secara tahunan":
  rows {all_of: "Medan"}, measure yoy, at Q4 2025, compare none,
  test "now < 0", summary {count: 2}
SULNI "kontraksi pertumbuhan ULN swasta yang lebih rendah":
  rows {named: ["Swasta"]}, measure yoy, at Jun 2026, compare previous_period,
  test "now < 0 and abs(now) < abs(before)", summary each
```

```
SPE Juli "tekanan inflasi pada Oktober 2026 dan Januari 2027 diprakirakan menurun":
  rows {named: ["Ekspektasi Harga Umum"]}, measure level, at [Oct 2026, Jan 2027],
  compare previous_period, test "now < before", summary each
  -> Oct 146,5 < Sep 155,2 and Jan 166,0 < Dec 168,1: Sesuai
```

The expectation rows are keyed by target month (`spe._add_target_month_rows`). So "previous period" is the previous target month of the same horizon, which is also the previous survey's figure. The June report states this basis itself: "IEH Desember 2026 … 168,1, lebih tinggi dibandingkan dengan IEH November 2026 sebesar 167,5". No special `previous_survey` block is needed. The old error was reading the two months as one trend Oct → Jan.

### Context from the sentence before

The extractor reads the whole chunk, so a composition writes its periods and rows explicitly. "Triwulan sebelumnya" after a forecast sentence becomes `at: Q1 2026`. "Kelompok lainnya" after named groups becomes `rest_of` with the named groups in `except`. The engine no longer has to recover what the sentence before said.

## How the engine evaluates it

1. Resolve `rows` with the existing matching (aliases, siblings, as `_judge_group_scope` does today). No row → Tidak Cukup Data, reason `tidak_ditemukan`.
2. For each row, read the measure at `at` and at `compare`. It uses the existing yoy / mtm / growth-table logic, so a yoy claim still reads a yoy table when one exists. A missing period → Tidak Cukup Data, reason `periode`.
3. Apply `test` per row, then `summary` across rows.
4. Sesuai / Tidak Sesuai, with a reasoning line that lists every row's numbers, as the group claims already do.

Today's group code (`_judge_group_scope`, `_judge_per_group`) already does steps 1, 3 and 4 for `all`/`majority`/`rest` with a fixed test. The composed path generalises it, and those operations can later be rewritten as compositions (not in the first version).

## What can go wrong

- **The extractor composes the wrong reading.** The engine then refutes a true sentence, the same failure as today but in a new form. Mitigations:
  - The reading is printed with the result.
  - It is measured on labels before any default change.
  - See the first open point about Tidak Sesuai.
- **The extractor composes too often** for claims the menu handles. The prompt allows `composed` only when no operation fits, and the measurement counts how often it is used. A menu claim moving to `composed` is a regression.
- **Flash without thinking may compose poorly.** The measurement runs both with and without the thinking budget (1024), on the user's OK for the cost.

## Tests (first, before the code)

- The grammar accepts the documented conditions and rejects anything else (names, calls other than `abs`, attribute access).
- One engine test per example above, on synthetic tables like `tests/test_group_scope.py`. Each also has a counterpart that must come back Tidak Sesuai (e.g. 11 of 18 slowed when the sentence says 12).
- Unknown row / missing period → Tidak Cukup Data with the reason code.
- Every existing extraction and engine test still passes: menu claims are untouched.
- A fact with `operation: composed` round-trips through `fact_to_dict` and replay.

## Measurement

- **Documents:** the fourth editions (new held-out set, because 456faf0 was fixed from the third). The third editions serve as a second check.
- **Runs:** the commit before vs after, 2 runs each to see past LLM variance, AI review on. Cost estimated and asked for before running.
- **Scored with the labels and `score.py --run`:**
  - false Tidak Sesuai (must not rise),
  - real errors caught (must not fall),
  - claims judged Sesuai/Tidak Sesuai instead of Tidak Cukup Data,
  - how many claims came back `composed`, and how many of those are right.
- Kept only if false Tidak Sesuai does not rise and the other numbers improve beyond the run-to-run noise. Then default on, per the "default on when proven" rule.

## Decisions (2026-10-22)

1. A failed composition is Tidak Sesuai, with the reading shown and reviewed by the Tinjauan AI (see Rules).
2. No `previous_survey` block. Several periods each against their own previous period cover the SPE/SKDU expectations (see the SPE example). If the owner later says BI means "lower than the current month", that is a `compare: {period: …}` and needs no new block.
3. Count sentences are judged (closes OPEN_QUESTIONS 10 for counts; rankings keep the existing `_check_ranking`).
4. No flag.
