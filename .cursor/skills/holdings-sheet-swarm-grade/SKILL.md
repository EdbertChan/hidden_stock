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
3. If `--produce`: run export with **both** flags

```bash
set -a && source .env && set +a
python scripts/export_equity_holdings_sheets.py \
  --ticker <RESOLVED> --live --history --new-sheet
```

4. **Grade** (parallel judges; mechanical always runs inside the script):

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

Ground-truth sources only — no OTC invent. **Always scope to the resolved parent**
(`normalize_parent`): BABA sheets/CSVs/grades must not use Uber anchors, and vice versa.

Principles (see `principle-assert-invariants-not-last-bug`): **never invent**;
**assert the class**; **grade invent even when `$` is null**; **exits are exits**;
**provenance stamps**; **no escape hatches**; **one parser module per form type**.

**Form → parser (unique):** SEC 13F → `parse_13f` / `sec_api_13f`; SC 13G/D →
`sec_13g`; 10-K/10-Q/20-F notes → `parse_notes`; HKEX annual report →
`parse_hk_annual`. Shared ticker/CUSIP helpers live in `identity.py` (not a
form parser). Do not hardcode HK Note 22 numbers in orchestration.

1. **Valuation order:** 13F `$` → 10-Q/10-K/20-F Investments-table FV → **HKEX annual Note 22 / portfolio FV** (`value_source=hk_annual_note22`) → else null.
2. **13G** = identity / % / shares only — never primary `$`. Non-null `$` with only `source=13g` in the note is FAIL — must also cite `investments_table` / `value_source=` / `13f` / `hk_annual_note22`.
3. **UBER only @ 2026-06-30** (Uber 10-Q Investments, millions): Didi **1900**, Grab **2020**, Aurora **1763**. Sheet `$` must match within ~5% or $50M. **Skip this check for every other parent.**
4. **One row per ticker per `period_end` for every ticker** — including 13F **exit** + continuing 13G (SERV-class) and `PRIV_*` private notes. Not “one AUR” only.
5. Chart / portfolio ranking by **latest-period** `$` (not all-time max). Uniqueness is on **history**.
6. Null `$` omitted from chart is OK; inventing OTC×shares is FAIL. **Null `$` does not excuse invent shares/tickers** — still FAIL `shares_proxy=presence`, `%` in `shares_held`, or exchange-like first-word fakes.
7. **`shares_held`:** trust 13F / parsed 13G share counts only. Ownership_% stays in `ownership_pct` (`qoq_continuity=ownership_pct`) — **never** write % into `shares_held`. **Never** invent `shares_proxy=presence`. Private notes: **`PRIV_<slug>`** + `ticker=private_note` is allowed (not a FAIL). Exit rows cite the **period grid** accession. 13G/note `filing_date` = source `as_of` (matches accession).
8. **Open the filing** when a row cites SC 13G/A / cessation / 0% — do not trust the sheet alone (BILI class).
9. **ADS ratio / reverse-split** (exact integer share drop + issuer CUSIP change) is a **restatement**, not a sale — expect `action=ratio_adj` / `restatement_not_disposal`, not disposal P&L.
10. **Never mix share counts with ownership_%** in QoQ action/delta (TME/CANG class).
11. **13G exits:** prefer the **explicit zeroing filing** accession + matching `as_of` when recorded (`exit_events`). Inferred disappearances without an exit event keep the **prior-hold** accession (sibling same-day steal guard) and matching prior `as_of`.
12. **Blank `investee_ticker` on public names** is FAIL unless `PRIV_*` / private_note — stamp via alias/CUSIP/hints.
13. **13G exit requires quantitative confirmation** — positive Aggregate Amount **and** % ⇒ not an exit. Do **not** treat Schedule 13G Item 5 form instructions (“ceased to be the beneficial owner… check the following”) as cessation by themselves (DIDIY/NU class).
14. **M&A / take-private without 13G zeroing** (GLUU←EA, FTCH←Coupang class) must cutoff the running map after known close date — do not carry delisted names for years.
15. **Empty portfolio/returns** when every `$` is null is **PASS**. TCEHY with parsed HK annual FV rows should show those aggregates on portfolio/chart — still never invent OTC×shares for named 13G names.
16. **CAGR years** = calendar span from start of the **latest unbroken** Dietz segment (first portfolio period, or last `series_break=coverage_basis`) through last cum-growth period — not `n_q/4`. CAGR footer: put years in the note row only; leave `cum_growth_index` null on the CAGR footer row.
17. Before FAIL on stale M&A/take-private, confirm there is no `action=exit` / `13g_exit=1` row for that ticker (SOGO-class false positive).
18. **Coverage basis switch** (e.g. `PRIV_HK_LISTED_ASSOCIATES` → `PRIV_HK_LISTED_INVESTEES_FV`): must stamp `series_break=coverage_basis` and reset linked Dietz — **not** treat the $ gap as cash inflow with zero return. PASS when stamped.
19. **Nested HK aggregates:** listed-associates FV is a subset of all-listed-investees FV — never both in portfolio `$` (`nested_in=listed_investees_fv`).
20. **HK composition overlay (TCEHY):** Portfolio `$` SoT = `PRIV_HK_*` Note 22 aggregates (`parent_aggregate_market_value_usd` on **positions_qoq**). Named children = SEC 13G/D. First-class `composition_parent` / `composition_as_of` (+ note mirror) — **PASS**. Inventing child `$` via shares×EOD into `market_value_usd` — **FAIL**. Broker `$` join with `child_value_source=broker_sotp` — **PASS**; never treat parent aggregate (~$90B) as PDD value-to-Tencent (~$10–40B). Residuals = `PRIV_HK_*_RESIDUAL` on QoQ with `excluded_from_portfolio_mv` — **no** separate `hk_composition` tab. `current_holdings` = open-positions slice of QoQ.
21. **13G filing-date estimate:** `cost_basis_est_*` (at `action=new`) + `mark_at_filing_est_usd` (each period) via EOD×shares are **PASS** when stamped `value_estimate=eod_at_filing; excluded_from_portfolio_mv` and `market_value_usd` stays null. Putting that estimate into `market_value_usd` / portfolio — **FAIL**.
22. **Display stack ≠ portfolio SoT:** `holdings_qoq_chart` stacks named marks on **calendar quarter-ends ≤ today** (`basis=display_estimate_or_broker`) sorted by size — **PASS**. Preference: **`mark_at_filing_est_usd` → non-broker `$` → broker `$` last**. Preferring broker `$` over continuous EOD marks (PDD 2023-03 / 2026-06 cliffs) — **FAIL**. Filing-date x-axis thrash, future quarters, treating that stack as Dietz / Note 22 SoT, or starring only `PRIV_HK_*` when named display values exist — **FAIL**.
23. **Holding returns** must exclude `PRIV_HK_*_RESIDUAL` / `excluded_from_portfolio_mv` — never double-count residual at weight 1.0 beside the parent aggregate.
24. **ADS ordinary vs ADS price:** EOD estimates apply known ordinary-per-ADS ratios (`ads_ordinary_ratio=`); ordinary×ADS invent (MOGU $86B class) is **FAIL** when unscaled.

Also see `/pipeline-swarm-validate` for stage-scoped mechanical + Fable/Codex boards (broker / overlay / composition).

Filing: https://www.sec.gov/Archives/edgar/data/1543151/000154315126000032/uber-20260630.htm

## Mechanical must catch (not only LLM)

Prefer encoding these in `scripts/grade_holdings_sheet.py` mechanical precheck so
PASS cannot hide invent on null-`$` rows:

- any note containing `shares_proxy=presence` → FAIL
- `shares_held` equals `ownership_pct` (non-null) → FAIL
- exchange-like first-word fakes without `ticker=private_note` / `PRIV_` → FAIL
- exit row accession equals prior hold accession when a newer grid accession exists → FAIL
- `13g_exit=1` with `ownership_pct>0` or `shares_held>0` → FAIL
- `PRIV_*` / `ticker=private_note` (except `PRIV_HK_*`) whose name resolves to a
  public ticker via aliases/hints → FAIL (`public_ticker_misclassified_as_private`)
- 13G/note-overlay exit row (`note` contains `source=13g`/`20f_note`/`10k_note`/
  `10q_note`/`ticker=private_note`) with `market_value_usd == 0.0` → FAIL
  (`overlay_exit_invented_zero` — only a real 13F-sourced exit may be forced to 0.0)
- `holdings_qoq_chart` QoQ drop >70% then rebound (display basis cliff) → FAIL
  (`display_basis_cliff`)
- blank `investee_ticker` on a named public investee (not `ticker=private_note`) → FAIL
  (`blank_public_ticker`)

## After judgment (required)

When board is FAIL / NEEDS_WORK:

1. `/reflect` why it shipped and why we missed it.
2. Fix Dagster/pipeline so the class of bug cannot recur (assert / identity / coalesce / provenance stamp).
3. Codify the invariant into this skill + `equity-holdings-sheets` (assert language, not last-bug name).
4. Re-export `--live --history`, re-grade; do not declare fixed without a new board PASS.

## Eventual swarm (not fully automated yet)

```text
Dagster materialize (equity_holdings*) → Sheets export (--live --history)
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
