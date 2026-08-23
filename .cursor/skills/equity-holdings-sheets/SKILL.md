---
name: equity-holdings-sheets
description: >-
  Track or export quarter-over-quarter equity holdings for any parent company
  or ticker (BABA, Tencent/TCEHY, BRK-B, Uber/UBER, …): resolve alias → refresh
  Postgres → CSV + Google Sheets with stacked QoQ chart. Invoke explicitly as
  /equity-holdings-sheets <company|ticker> (e.g. /equity-holdings-sheets uber or
  /equity-holdings-sheets tencent).
disable-model-invocation: true
---

# Equity holdings → Postgres / CSV / Google Sheets (stock-agnostic)

## Trigger examples

Invoke explicitly:

- `/equity-holdings-sheets tencent`
- `/equity-holdings-sheets uber`
- `/equity-holdings-sheets BABA`

## Agent workflow (required)

1. **Resolve parent** with `normalize_parent` / aliases:
   - `tencent` / `0700` / `TCTZF` → `TCEHY`
   - `alibaba` → `BABA`
   - `berkshire` / `brk.b` → `BRK-B`
   - else uppercase ticker (`XPEV`-style parents use their own ticker)
2. **Auth** — service accounts have 0 Drive quota:
   - New experiment sheet: OAuth (`GOOGLE_SHEETS_OAUTH_*`) + `--new-sheet`
   - Reuse: share sheet with SA, set `GOOGLE_SHEETS_SPREADSHEET_ID`, `--reuse-sheet`
3. **Run** — always refresh **live + history** together (history-only leaves stale invent `$` on `current_holdings`). Default `--lookback-years 5` is the **chart/portfolio window**; edge-held names sparsely walk earlier 13Fs for true `first_seen` / lot cost (never treat the window cut as inception). `--lookback-years 0` = no calendar cut (full book from 2000-01-01, still capped by `max_filings`).

```bash
set -a && source .env && set +a
python scripts/export_equity_holdings_sheets.py \
  --ticker <RESOLVED> --live --history --new-sheet \
  --lookback-years 5
```

   Or Dagster — **one** shared resource block (not per-asset Config):

```yaml
# Run config
resources:
  equity_holdings_settings:
    ticker_allowlist: ["UBER"]   # or TCEHY / BABA
    history_lookback_years: 5    # primary depth
    history_max_filings: 80      # safety ceiling inside the window
```

```bash
dagster asset materialize -m hidden_stock.definitions \
  --select equity_holdings,equity_holdings_history,equity_holdings_export
```

Depth is **calendar years** (`history_lookback_years`), not filing count. `history_max_filings` only caps how many filings are pulled inside that window.

4. **Reply** with spreadsheet URL and sources used (`13f+13g+notes` fan-out; HK parents also use annual aggregates).
5. **Grade** with `/holdings-sheet-swarm-grade <ticker> --sheet-url <URL>` before calling it done.

## Tabs

`positions_qoq`, `current_holdings`, `portfolio_by_period`, `holdings_qoq_chart` (+ stacked column chart; **one column per ticker — never `OTHER`**), plus estimated performance tabs:

- `holdings_qoq_chart`: **calendar quarter-ends**, **top 7** names by latest size (broker `$` / `mark_at_filing_est_usd`; `basis=display_estimate_or_broker; not_portfolio_sot`). Rest omitted (no OTHER). Not every 13G filing date. Own tab with chart at top. Dietz / `portfolio_by_period` stay on portfolio SoT. `fanout_13g_hk` default lookback **8** years.
- `returns_by_period` — portfolio MV, net external flow, **MTM** (primary), avg-cost + FIFO disposal estimates, Modified Dietz; **Dietz combo chart embeds here** (`dietz_return_pct` + `cum_dietz_growth`; estimated linked Dietz / CAGR subtitle)
- `realized_pnl_qoq` — disposal events with `cost_method=avg|fifo` (avg is primary; FIFO sensitivity). No separate realized chart tabs (empty/zero for 13G-null-`$` parents).
- `holding_returns` — per ticker × period weight, Dietz, contribution, MTM, avg/FIFO realized
- `reported_vs_est` — BABA curated Interest and investment income vs summed calendar MTM (reconciliation; residual expected)

Thin `chart_data` / `returns_chart` / `realized_chart` / `realized_by_ticker_chart` tabs are **removed**.
## Estimated P&L / returns (not tax or GAAP)

Form 13F / Investments FV give **period-end shares + market value**, not purchase price or tax-lot cost.

- **MTM** (primary): `end_mv − start_mv − net_external_flow` — closest proxy to revaluation / mark moves.
- **Avg-cost disposal** (primary disposal): open lots on buys at period px; on sell use weighted-average cost (ASC 321-style wording).
- **FIFO disposal** (sensitivity only): same lots, oldest-first — do **not** present as “their P&L.”
- Null-`$` names excluded from portfolio MV / Dietz. Labels say **estimated** / **period-end price proxy**.
- Company totals (BABA YAML) are a **reconciliation bar**, not an equality gate (Ant, private marks, equity-method, FX, interest).

## Principles (do not regress)

Canonical copy: `.cursor/skills/principle-assert-invariants-not-last-bug/SKILL.md`.

1. **Never invent** — no fake shares, fake tickers, or fake `$` for QoQ continuity.
2. **Assert the class** — after a miss, refuse-to-ship the *class* for unseen tickers.
3. **Grade invent even when `$` is null** — identity/shares bugs are still FAILs.
4. **Exits are exits** — 0%/cessation pops 13G, suppresses note overlays, blocks live re-add.
5. **Provenance stamps travel with the value** — `$` and `%` cite their real source.
6. **No silent escape hatches** — do not add “continuity” invent paths that look rubric-OK.
7. **Lookback ≠ inception** — windowed chart; sparse walkback for edge-held lots.
8. **ADS ratio ≠ disposal** — integer share consol + issuer CUSIP change ⇒ `ratio_adj`, scale lots, no realized.

## Rules

- Never hardcode BABA; always take ticker from the user request.
- **One parent per export/grade:** sheet title, CSV prefixes (`exports/<slug>_…`), allowlist, and swarm-grade anchors must all match the resolved ticker. Never grade BABA with Uber DIDIY/GRAB/AUR truth (or the reverse).
- Blank tickers must be stamped from CUSIP/name aliases and coalesced so `period_end × ticker` is unique (13G XPeng → XPEV). Private notes use **`PRIV_<slug>`** + `ticker=private_note` — never first-word fakes (`ANT`/`CHINA`).
- Narrative 20-F “amount invested” / equity-method carrying is **not** chart `$` — only 13F or Investments-table FV.
- History/export require `equity_holdings_settings.ticker_allowlist` (shared resource).
- Do not commit `.env`, SA JSON, or OAuth tokens.
- CI: `uv run pytest tests/` (see `.github/workflows/ci.yml`); holdings regressions live in `tests/test_holdings_ci_regressions.py`.

## Valuation (do not invent stake $)

For each investee × period, in order:

1. Form **13F** `market_value_usd` if present (listed book).
2. Else **10-Q / 10-K / 20-F Investments table** (or notes) disclosed FV/carrying — `$M × 1e6`.
3. Else leave `market_value_usd` **null** (omit from chart).
4. Schedule **13D/G** → ownership % / shares / name only — **never** primary dollars.

**Never** invent stake dollars via OTC/EODHD `shares × price` or `mcap × ownership_%`. That path shipped DiDi at ~$482M when Uber’s 10-Q already disclosed **$1,900M**.

Null chart cells are OK until a filing discloses FV. Do **not** “fix” a missing DIDIY bar by inventing a mark.

When Investments/notes fill `$` onto a 13G identity row, the **note must cite** `investments_table` / `value_source=` — leaving only `source=13g` is a FAIL (Codex: beneficial ownership used as value source).

Merge collide on **ticker** (and CUSIP) so 13G cannot double-count a 13F name (AUR/GRAB).

Chart series order by **latest** `period_end` `$`, not all-time max.

## `shares_held` trust (do not treat as always real)

| Source | Trust `shares_held`? | Notes |
|---|---|---|
| Form **13F** | Yes (as of filing) | Filer-reported share amount. |
| **13D/G** with parsed share count | Yes (as of that filing) | May lag the 13F/`period_end` grid; ADR vs ordinary class can differ. |
| **13D/G** or notes with **% only** | **No** | Live + history: `shares_held` **null**; keep `ownership_pct`. Stamp `qoq_continuity=ownership_pct`. **Never** write `%` into `shares_held`. **Never** invent `shares_proxy=presence`. |
| **13D/G** cessation (0% / Aggregate Amount 0 / Item 5) | Exit → **0** | Pop 13G running map; `13g_exit=1`; suppress stale note overlays; block live re-add from older 13Gs (BILI class). |
| Private 20-F names | n/a | Identity = `PRIV_<slug>` + `ticker=private_note` (allowed). |
| Investments table | Usually null | Table gives FV/$M, not share counts. |

- Never invent `shares_held=1` / `shares_proxy=presence` when parse yields no qty — **drop** or mark **exit**.
- Exit rows cite the period-grid accession (disappearance filing), not the prior hold’s accession.
- 13G/note overlay `filing_date` = source `as_of` (matches accession), not the 13F grid date.
- **Dagster:** export scrub + assert before Postgres/Sheets write (`validate.scrub_live_pct_as_shares` / `assert_live_shares_held_sane`). History uniqueness asserted in `equity_holdings_history`. Prefer also asserting: no `shares_proxy=presence`, no `%` copied into `shares_held`, no exchange-like first-word tickers.
- **Tickers:** always `normalize_ticker` before `.strip()` / uniqueness. Pandas `nan` is truthy — `(nan or "").strip()` crashes.

## How you check (before saying fixed)

```text
history.groupby(period_end, investee_ticker).size() == 1
# non-null $ ⇒ note cites 13f OR investments_table OR value_source=
# shares_held ∈ {parsed count, 0, null} — never % / presence
# no ANT/CHINA first-word fakes; PRIV_* OK for private notes
# open cited SC 13G/A when claim is ownership / exit
```

1. Sheet `portfolio_by_period` / `chart_data`: for UBER mid-2026, DIDIY ≈ **1900** ($M), not ~482.
2. History: **one row per ticker per `period_end`** (including `PRIV_*`).
3. Open the parent’s latest 10-Q/10-K Investments table; match disclosed FV.
4. 13G+$ rows cite Investments provenance, not only `source=13g`.
5. `uv run pytest tests/test_holdings_ci_regressions.py tests/test_13g_exit_bili.py tests/ -q`.
6. `/holdings-sheet-swarm-grade <ticker>` (mechanical + Fable + Codex) — invent on null-`$` rows is still FAIL.

Fan-out: ticker → CIK → **13F + 13D/G + notes** → one row per ticker per period. Assert the class of bug, not the last ticker name.
