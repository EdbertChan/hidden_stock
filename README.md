# hidden_stock

Dagster pipeline and CLI tools for **parent-company equity holdings**: SEC 13F /
SC 13G/D, HKEX annual Note 22 aggregates, optional broker SOTP overlays, and
Google Sheets / CSV export with QoQ charts.

## What this is

- Stock-agnostic orchestration: resolve a parent ticker → fan out filings →
  coalesce history → export.
- Valuation SoT is filing-disclosed `$` (13F / investments tables / HK Note 22).
  13G is identity / shares / % only. Broker SOTP and EOD×shares marks are
  display overlays when stamped and excluded from portfolio MV.

## Quick start

```bash
cp .env.example .env   # fill SEC_EDGAR_USER_AGENT, DB, optional EODHD / Sheets
uv sync
# Optional: copy and fill a licensed broker catalog
# cp hidden_stock/quirks/holdings/data/broker_sotp_catalog.example.yaml \
#    hidden_stock/quirks/holdings/data/broker_sotp_catalog.yaml

uv run python scripts/export_equity_holdings_sheets.py \
  --ticker <PARENT> --live --history --new-sheet
```

Grade an export (mechanical + optional LLM judges):

```bash
uv run python scripts/grade_holdings_sheet.py --ticker <PARENT> --judges fable,codex
```

## Layout

| Path | Role |
|---|---|
| `hidden_stock/assets/equity_holdings.py` | Dagster assets |
| `hidden_stock/quirks/holdings/` | Parsers, history, export, overlays |
| `scripts/` | Export / grade / swarm validate CLIs |
| `.cursor/skills/` | Agent skills for holdings sheets + swarm grade |

## Privacy / licensing

- Do **not** commit `.env`, OAuth tokens, Google credentials, or paid research PDFs.
- `exports/` and `*.pdf` are gitignored. Broker catalog in-repo ships **empty**;
  add your own licensed URLs locally.
- Identity YAML (CUSIP / aliases) is operational config, not a holdings dump.

## License

See repository license / copyright holder terms before redistributing.
