# Pengaturan: publications, aliases and notes edited by the team

Status: agreed with the owner 2026-10-23 (layout: list + detail; aliases whole-name only; no shortcut from results). Design follows the impeccable skill's guidance (Operate mode, existing visual world).

## Problem

The 13 publications live in code, in three places that must stay in step:

| Place | What | Kind |
|---|---|---|
| `static/index.html` `PUBLICATIONS` | label, documents (siaran pers / laporan / both), Excel hint, checks | data |
| `check_history.PUBLICATIONS` | the keys the server stores in Riwayat | data |
| `publication_parsers/<pub>.py` | how the publication's Excel is read, including its aliases (`TERM_ALIASES`, `LABEL_ALIASES`, …) and conventions (`level_phrases`, `SKIPPED_SECTIONS`, …) | code |

An analyst who knows that "AFLN Indonesia" is the 'Aset' row cannot tell the system without a code change. Aliases measurably help both the engine and the Tinjauan AI (456faf0, 6bc5cae). The per-publication notes (`*.notes.yaml`) are not used: they sit behind `PUBLICATION_NOTES`, which is off, and in extraction they did not help (runs notes_1022, notes2_1022).

## Decisions (owner, 2026-10-23)

- A **Pengaturan** tab where the team can:
  - **hide / show** each of the 13 publications. No publication is added or deleted; the list stays the 13 in code.
  - **add / remove aliases**: a phrase in the report → a row of the publication's table.
  - **edit notes**: conventions in the team's own words, e.g. *"secara triwulanan" → tabel %qtq; "secara tahunan" → tabel %yoy; tanpa keterangan dan menyebut level → tabel indeks*.
- **Everyone signed in can edit.** Every change is logged: who, when, what.
- **Shared across the team**: one setting, stored in the history database (Neon on Render, SQLite locally).
- **Notes are seeded from the YAML notes**, minus their alias lines (the parsers hold those) and anything the notes test already rejects.
- **Notes go to the Tinjauan AI only**, not to extraction. Its answers are still checked by the program (cells exist, rows match the sentence, the calculation reproduces the claim), so a wrong note cannot create a Sesuai or drop a claim.
- Out of scope:
  - adding publications;
  - structured conventions (level phrases, skipped sections, signs) as forms;
  - notes in extraction;
  - "secara tahunan" → yoy as an engine rule (owner: unnecessary complexity).

## What each setting does

### Hide / show

A hidden publication disappears from the "Jenis publikasi" choices on the upload card. Nothing else changes:
- Riwayat rows keep their publication name;
- an old check opened from Riwayat still shows it;
- the server still accepts the key (a page opened before the change can still submit).

### Aliases

- **Built-in aliases** (from the parser modules) are listed read-only, marked *bawaan*, so nobody adds them twice.
- **Team aliases** are added on top. Each one has:
  - `frasa`: as the report writes it, compared case- and space-insensitively;
  - `baris`: the table row's name, as the parser reads it.
- **Whole name only** (`TableData.label_aliases`): the metric the extractor wrote must BE the phrase. "penjualan eceran" never rewrites "penjualan eceran Kelompok Sandang". A variant needs its own alias.
- **Applied where the built-ins are:** in `publication_parsers.parse_for_publication`, after the parser, and only for rows that sheet actually has. An alias for a row the sheet lacks is ignored there, not an error, because a publication has several sheets.
- **The engine and the Tinjauan AI both see them**, through the same `TableData` fields they already read.
- **Conflict:** a team alias whose phrase equals a built-in one wins. The page warns when it is saved.
- **Row names:** the page cannot read the Excel. It suggests row names from the last check of that publication: each check stores the row names it parsed, per publication, in the settings tables (row names only, no values). A row never seen is allowed, with a warning "baris ini belum pernah terlihat di tabel publikasi ini".

### Notes

- **A list of short entries per publication.** Each entry has its text, author and date. Entries are added, edited, removed and reordered.
- **Limit:** 600 words per publication (the existing `NOTES_MAX_WORDS`).
- **Shown to the Tinjauan AI** as a block before the claims: "CATATAN TIM UNTUK <publikasi>". Only the publication of the check is shown; a check under "Umum" gets none.
- **Save-time warning** (not a block) on wording that made the extractor drop claims before: "tidak ada di tabel", "tidak memuat", "abaikan", "jangan diperiksa", "belum dipetakan".
- **Seed:**
  - On first read, when a publication has no notes row yet, its `*.notes.yaml` entries become notes. This covers the `conventions`, `periods` and `units_signs` sections; `phrases` is left out, because the parser's aliases already cover those.
  - Seeded entries are marked *dari catatan awal* and are normal entries after that.
  - A seed happens once: removing every note does not bring them back. "Kembalikan ke awal" does.

### Change log

Every add, edit, remove, hide and show is logged with: who (the name the user enters, the same field Riwayat uses), when, the publication, and the value before and after. The page shows the latest 50, filterable by publication.

### Reset

Per publication, "Kembalikan ke awal" does three things, after a confirmation step built into the page (no browser `confirm()`):
- removes its team aliases;
- restores the seeded notes;
- shows it if it was hidden.

The reset itself is logged.

## Storage

These are new tables in the history database, through `check_history`'s engine. It already chooses Neon or the local SQLite.

```
settings_publication (publication PK, hidden bool, updated_by, updated_at)
settings_alias       (id PK, publication, phrase, row, created_by, created_at)
settings_note        (id PK, publication, position, text, seeded bool, created_by, created_at, updated_by, updated_at)
settings_note_seeded (publication PK, seeded_at)          -- the seed ran once
settings_known_row   (publication, row PK pair, last_seen) -- suggestions for the alias form
settings_change      (id PK, at, by, publication, kind 'visibility'|'alias'|'note'|'reset', action, before, after)
```

A check reads the settings once, at the start of `verify-paired`. That is one small query per table. A failure to read them is logged, and the check runs on the built-ins alone. A settings problem never stops a check.

## API (behind the existing login gate)

```
GET    /api/settings                       publications with hidden flag, built-in + team aliases, notes
GET    /api/settings/changes?publication=  latest 50 changes
PUT    /api/settings/{pub}/visibility      {hidden, by}
POST   /api/settings/{pub}/aliases         {phrase, row, by}
DELETE /api/settings/{pub}/aliases/{id}?by=
POST   /api/settings/{pub}/notes           {text, by}
PUT    /api/settings/{pub}/notes/{id}      {text, by}
DELETE /api/settings/{pub}/notes/{id}?by=
PUT    /api/settings/{pub}/notes/order     {ids, by}
POST   /api/settings/{pub}/reset           {by}
```

- An unknown publication key → 404.
- An empty phrase, row or text, or more than 600 note words → 422, with the reason in Indonesian.
- `by` is required (1-80 chars, as `CHECKER_NAME_MAX_CHARS`).

## Page

- The **Pengaturan** nav item goes below Riwayat.
- **List + detail:**
  - On the left, the 13 publications, each with its visibility switch. A hidden one is greyed, not removed.
  - On the right, the chosen publication's Padanan nama, Catatan and Riwayat perubahan.
- **No shortcut from the check results** (owner): aliases are added on this page only.
- The visual design follows the existing app (tokens and components in `static/index.html`). It will be done with the design skill the owner picks; this spec fixes behaviour, not looks.

## Tests (first)

- **Storage:**
  - add / remove / reorder;
  - the seed runs once;
  - reset restores the seed;
  - every change writes a log row.
- **API:** each route's happy path; 404 for an unknown publication; 422 for an empty or overlong value; `by` required.
- **Aliases reach the table:**
  - a team alias resolves a metric whose whole name is the phrase, and leaves a longer name alone;
  - an alias for a row the sheet lacks is ignored there;
  - a team alias overrides a built-in one.
- **Notes reach the Tinjauan AI prompt** for the check's publication only; "Umum" gets none.
- **Hidden publications** are absent from `GET /api/settings`'s visible list but still accepted by `verify-paired`.
- **A settings read failure** leaves a check running on the built-ins.
- **The frontend** builds the publication choices from the server list (existing headless-Chrome check, `ui_browser_check`).

## Measurement

The page itself changes nothing until someone edits. When the team adds aliases or notes, their effect can be measured cheaply with `run_regression replay --ai-review` on saved runs. That costs about Rp5 ribu for 19 documents, and is paid, so the owner's OK comes first.
