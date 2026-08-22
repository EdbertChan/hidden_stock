---
name: equity-holdings-sheets
description: >-
  Refresh BABA (or allowlisted) equity holdings to Postgres, export CSVs, and
  push Google Sheets tabs plus the stacked QoQ chart. Use when the user asks to
  run/export holdings to Sheets, productionize spreadsheet creation, refresh
  Moonshot/Ant note history, or materialize equity_holdings_job.
---

# Equity holdings → Postgres / CSV / Google Sheets

## When to use

- Export or refresh BABA (or other allowlist) equity holdings
- Create/update the Google Spreadsheet with QoQ chart
- Materialize `equity_holdings_job` in Dagster
- Debug why private names (Moonshot, Ant) are missing from QoQ

## Auth (required for Sheets)

Service accounts have **0 Drive quota** and cannot create files.

**Preferred (new sheet each experiment):**

1. GCP → OAuth client (Desktop) JSON
2. `python scripts/google_sheets_oauth_setup.py --client-secrets /path/to/client.json`
3. Set in `.env`:
   - `GOOGLE_SHEETS_OAUTH_CLIENT_SECRETS`
   - `GOOGLE_SHEETS_OAUTH_TOKEN`
   - `GOOGLE_SHEETS_CREATE_NEW=1`

**Reuse one sheet:** share it with the SA email, set `GOOGLE_SHEETS_SPREADSHEET_ID`, run with `--reuse-sheet`.

Also required: `GOOGLE_SHEETS_CREDENTIALS_JSON` (SA JSON path), `SEC_EDGAR_USER_AGENT`, `POSTGRES_*`.

## One-shot CLI

```bash
set -a && source .env && set +a
# New experiment spreadsheet (OAuth)
python scripts/export_equity_holdings_sheets.py --ticker BABA --live --history --new-sheet

# Reuse fixed ID
python scripts/export_equity_holdings_sheets.py --ticker BABA --live --history --reuse-sheet \
  --spreadsheet-id "$GOOGLE_SHEETS_SPREADSHEET_ID"
```

Tabs written: `positions_qoq`, `current_holdings`, `portfolio_by_period`, `chart_data` (+ embedded stacked column chart).

## Dagster production path

```bash
# Allowlist BABA (config on asset materialization)
dagster asset materialize -m hidden_stock.definitions \
  --select equity_holdings,equity_holdings_history,equity_holdings_export
```

Job: `equity_holdings_job` selects all three. History uses `build_holdings_history` (13F QoQ **plus** forward-filled 20-F/10-K notes).

## Data model notes

- **Listed (XPEV, WB, …):** 13F shares + market value, true QoQ
- **Private (Moonshot, Ant):** annual 20-F/10-K note parse; ownership % + carrying USD; forward-filled into each 13F quarter after the note filing date
- Chart excludes known value spikes (`MRDB`) via `CHART_EXCLUDE_TICKERS`

## Agent checklist

1. Confirm `.env` has Postgres + Edgar + Sheets auth
2. Run export or Dagster materialize for the ticker allowlist
3. Verify Moonshot appears on `current_holdings` and `chart_data` / `positions_qoq`
4. Paste the spreadsheet URL back to the user
5. Do **not** commit `.env`, SA JSON, or OAuth tokens
