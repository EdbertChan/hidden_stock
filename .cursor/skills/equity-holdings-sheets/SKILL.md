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
3. **Run** (prefer CLI for one ticker):

```bash
set -a && source .env && set +a
python scripts/export_equity_holdings_sheets.py \
  --ticker <RESOLVED> --live --history --new-sheet
```

   Or Dagster with explicit allowlist (no default ticker):

```bash
# Materialize with config ticker_allowlist=["TCEHY"] (or BABA, etc.)
dagster asset materialize -m hidden_stock.definitions \
  --select equity_holdings,equity_holdings_history,equity_holdings_export
```

4. **Reply** with spreadsheet URL and sources used (`13f+13g+notes` fan-out; HK parents also use annual aggregates).


## Tabs

`positions_qoq`, `current_holdings`, `portfolio_by_period`, `chart_data` (+ stacked column chart).

## Rules

- Never hardcode BABA; always take ticker from the user request.
- `equity_holdings_history` / `equity_holdings_export` **require** `ticker_allowlist`.
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

Merge collide on **ticker** (and CUSIP) so 13G cannot double-count a 13F name (AUR/GRAB).

## How you check (before saying fixed)

Invariant (not “last bug name”):

```text
history.groupby(period_end, investee_ticker).size() == 1   # every ticker
```

1. Sheet `portfolio_by_period` / `chart_data`: for UBER mid-2026, DIDIY ≈ **1900** ($M), not ~482.
2. History: **one row per ticker per `period_end`** (AUR, SERV, GRAB, … — chart can hide exits/nulls).
3. Open the parent’s latest 10-Q/10-K Investments table; match Grab/Aurora/Didi (or equivalents) to our numbers.
4. `uv run pytest tests/test_holdings_ci_regressions.py tests/test_serv_period_uniqueness.py tests/ -q`.
5. Independent second opinion: `/holdings-sheet-swarm-grade <ticker>` (mechanical + Fable + Codex).

Fan-out shape: ticker → CIK → **13F + 13D/G + notes** → merge to **one row per ticker per period**. Do not invent stake `$` (OTC×shares).
