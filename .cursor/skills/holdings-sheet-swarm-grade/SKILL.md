---
name: holdings-sheet-swarm-grade
description: >-
  Grade an equity-holdings Google Sheet / CSV export with independent judges
  (Claude Fable + Codex). Invoke as /holdings-sheet-swarm-grade <ticker>
  [--sheet-url URL] [--produce]. Eventual swarm: Dagster produce → Fable+Codex
  grade → report judgment. Use when a holdings sheet looks wrong, after export,
  or when asking another model to check Uber/BABA/Tencent sheet quality.
disable-model-invocation: true
---

# Holdings sheet swarm grade (Fable + Codex)

Independent second opinions on a holdings spreadsheet. The building agent
must **not** self-grade as the only check.

## Trigger

- `/holdings-sheet-swarm-grade uber`
- `/holdings-sheet-swarm-grade UBER --sheet-url https://docs.google.com/...`
- `/holdings-sheet-swarm-grade uber --produce` (refresh export, then grade)

## Workflow (v1 — run now)

1. **Resolve parent** via `normalize_parent` (same as equity-holdings-sheets).
2. **Inputs** (prefer local CSV; sheet URL is for humans):
   - `exports/<ticker>_portfolio_by_period.csv`
   - `exports/<ticker>_equity_holdings_history.csv`
   - optional sheet URL
3. If `--produce`: run export first

```bash
set -a && source .env && set +a
python scripts/export_equity_holdings_sheets.py \
  --ticker <RESOLVED> --history --new-sheet
```

4. **Grade** (parallel judges):

```bash
python scripts/grade_holdings_sheet.py \
  --ticker <RESOLVED> \
  --sheet-url '<URL>' \
  --judges fable,codex
```

5. **Reply** with the judgment board: each judge’s verdict/score, blocking
   issues, agreement/disagreement, and one recommended next fix. Do not
   paraphrase away a FAIL.

## Rubric (judges must apply)

Ground-truth sources only — no OTC invent:

1. **Valuation order:** 13F `$` → 10-Q/10-K/20-F Investments-table FV → else null.
2. **13G** = identity / % / shares only — never primary `$`.
3. **UBER @ 2026-06-30** (Uber 10-Q Investments, millions): Didi **1900**, Grab **2020**, Aurora **1763**. Sheet `$` must match within ~5% or $50M.
4. **One row per ticker per `period_end` for every ticker** — including 13F **exit** + continuing 13G (SERV-class). Not “one AUR” only.
5. Chart / portfolio ranking by true max `$` — chart can hide exits/nulls; uniqueness is on **history**.
6. Null `$` omitted from chart is OK; inventing OTC×shares is FAIL.

Filing: https://www.sec.gov/Archives/edgar/data/1543151/000154315126000032/uber-20260630.htm

## After judgment (required)

When board is FAIL / NEEDS_WORK:

1. `/reflect` why it shipped and why we missed it.
2. Fix Dagster/pipeline so the class of bug cannot recur (assert / identity / coalesce).
3. Codify the invariant into this skill + `equity-holdings-sheets` (assert language, not last-bug name).
4. Re-grade; do not declare fixed without a new board.

## Eventual swarm (not fully automated yet)

```text
Dagster materialize (equity_holdings*) → Sheets export
        ↓
   mechanical precheck + Fable + Codex   (parallel judges)
        ↓
   Orchestrator (this agent) merges → tell user PASS/FAIL + fixes
   On FAIL → /reflect → pipeline assert → skill invariant → re-grade
```

Wire Dagster job + auto-grade later; v1 is the grade script + this skill.

## Auth

- **Codex:** `codex login` (worked in first UBER run).
- **Fable:** `claude login` with Fable access (`claude-fable-5`). Unset stale
  `ANTHROPIC_API_KEY` if Claude returns 401 / OAuth expired.

## Rules

- Never let the model that built the sheet be the sole judge.
- Prefer CSV evidence over “looks fine on the sheet.”
- If judges disagree on a blocking check, report BOTH and treat as needs_work.
- Do not commit `.env` / OAuth tokens.
