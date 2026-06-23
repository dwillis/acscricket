# Malayan Interstate Cricket 1899–1957

Tools for parsing, validating, and editing structured scorecard data extracted from the
[ACS Cricket archive](https://archive.acscricket.com/research/rm/malayan_interstate_cricket_1899-1957/rm_malayan_interstate_cricket_scorecards/index.html).

---

## Overview

The source material is a FlippingBook publication of ~355 scanned scorecard pages.
Each page is fetched, cached as plain text, and sent to an LLM which returns a
structured JSON object.  The pipeline then validates arithmetic consistency and
allows manual correction via a browser-based editor.

```
pages/          raw text cache (one .txt file per page number)
scorecards.py   fetch + LLM parse → scorecards.json
validate.py     arithmetic cross-checks
compare.py      pick the better parse from two candidate files
gold_editor.py  local web editor for gold_scorecards.json
gold_scorecards.json  hand-verified ground truth
site/           output consumed by the public website
```

---

## Setup

```bash
uv sync          # install dependencies from pyproject.toml
```

The parser needs an LLM key configured via the `llm` CLI:

```bash
llm keys set anthropic   # paste your Anthropic API key
```

---

## Parsing scorecards

### Single-threaded (sequential)

```bash
uv run python malayan_interstate_cricket/scorecards.py
```

Parse specific pages or ranges:

```bash
uv run python malayan_interstate_cricket/scorecards.py --pages 1,3,5-10
```

Re-parse pages that already exist in the output file:

```bash
uv run python malayan_interstate_cricket/scorecards.py --pages 42 --force
```

Change the model (default: `claude-sonnet-4.6`):

```bash
uv run python malayan_interstate_cricket/scorecards.py --model gemini-2.5-flash
```

Write to a different output file:

```bash
uv run python malayan_interstate_cricket/scorecards.py --output my_run.json
```

### Batch mode (Anthropic Message Batches — 50 % cost)

```bash
uv run python malayan_interstate_cricket/scorecards.py --batch
uv run python malayan_interstate_cricket/scorecards.py --batch --pages 50-100
```

If interrupted, resume with the printed batch ID:

```bash
uv run python malayan_interstate_cricket/scorecards.py --batch-id msgbatch_xxx
```

### Pre-fetch text only (populate cache without parsing)

```bash
uv run python malayan_interstate_cricket/scorecards.py --fetch-only
```

### Re-run name normalisation without re-parsing

```bash
uv run python malayan_interstate_cricket/scorecards.py --normalize-only
```

---

## Validation

Runs arithmetic cross-checks (batting totals, wicket counts, fall-of-wickets
ordering, bowling wickets vs dismissals, etc.) and reports issues with severity
scores (1 = cosmetic, 5 = structural).

```bash
uv run python malayan_interstate_cricket/validate.py
uv run python malayan_interstate_cricket/validate.py --input my_run.json
uv run python malayan_interstate_cricket/validate.py --json          # machine-readable output
uv run python malayan_interstate_cricket/validate.py --check batting_total,fow_ascending
```

Available checks: `missing_section`, `innings_structure`, `batting_total`,
`batsmen_count`, `dismissed_count`, `dismissal_wickets`, `duplicate_player`,
`fow_ascending`, `fow_count`, `fow_final_value`, `fow_max`, `extras_detail`,
`invalid_dismissal`, `overs_sanity`.

---

## Comparing two parse runs

`compare.py` ingests two JSON files and picks the better parse per page based on
completeness and severity-weighted validation scores.

```bash
# Print per-page verdict
uv run python malayan_interstate_cricket/compare.py original.json candidate.json

# Merge winners into a new file
uv run python malayan_interstate_cricket/compare.py original.json candidate.json --merge output.json

# Score against hand-verified ground truth
uv run python malayan_interstate_cricket/compare.py original.json candidate.json --golden gold_scorecards.json
```

---

## Gold Scorecard Editor

A local web application for reviewing and hand-correcting `gold_scorecards.json`,
the hand-verified ground truth used for comparison and evaluation.

```bash
uv run python malayan_interstate_cricket/gold_editor.py
```

Then open **http://localhost:8765** in your browser.

### Features

- Side-by-side view: the original FlippingBook page (iframe) alongside the
  parsed JSON fields
- Raw text tab shows the cached plain-text source for the page
- Editable fields for all match metadata, innings info, batting, bowling,
  extras, totals, and fall of wickets
- Add or delete innings, batsmen, and bowlers
- Per-page validation issues shown inline (fetched from `validate.py`)
- Verified / Unverified badge on each match; header shows overall progress
- Changes are saved back to `gold_scorecards.json` via a local POST endpoint

Stop the server with `Ctrl-C`, or kill it with:

```bash
pkill -f gold_editor.py
# or
lsof -ti:8765 | xargs kill -9
```

---

## Data format

Each entry in `scorecards.json` / `gold_scorecards.json` follows this schema:

```jsonc
{
  "page": 42,
  "source_url": "https://archive.acscricket.com/…/42/index.html",
  "verified": false,
  "match": {
    "team1": "Selangor",
    "team2": "Perak",
    "venue": "Selangor Club Padang, Kuala Lumpur",
    "date": "April 3 and 4, 1925",
    "result": "Selangor won by 8 wickets"
  },
  "innings": [
    {
      "team": "Perak",
      "innings_number": 1,
      "batting": [
        { "name": "AB Smith", "captain": false, "wicketkeeper": false,
          "dismissal": "c Jones b Brown", "runs": 34 }
      ],
      "extras": { "total": 7, "detail": "b 4, lb 3" },
      "total": { "runs": 112, "wickets": null, "declared": false },
      "fow": [15, 34, 56, 72, 88, 94, 100, 105, 109, 112],
      "bowling": [
        { "name": "CD Jones", "overs": "18", "maidens": 4,
          "runs": 42, "wickets": 5, "noballs": null, "wides": null }
      ]
    }
  ],
  "umpires": ["HC Belfield (Selangor)", "EL Bennett (Perak)"],
  "toss": "Perak won the toss",
  "close_of_play": null,
  "balls_per_over": 6,
  "notes": null
}
```

`total.wickets` is `null` when the team was all out (all 10 wickets fell);
a number means the innings was declared or unfinished.

---

## File reference

| File | Purpose |
|------|---------|
| `scorecards.py` | Main fetch-and-parse pipeline |
| `validate.py` | Arithmetic cross-checks |
| `compare.py` | Compare two parse runs, pick winners |
| `gold_editor.py` | Browser-based manual correction tool |
| `names.py` | Shared name/dismissal helpers |
| `dspy_pipeline.py` | Experimental DSPy-optimized extraction |
| `gold_scorecards.json` | Hand-verified ground truth |
| `pages/` | Plain-text page cache (one file per page) |
| `site/` | Built output for the public website |
