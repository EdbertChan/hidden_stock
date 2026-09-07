# hidden_stock

Dagster pipeline and CLI tools for **parent-company equity holdings**: SEC 13F /
SC 13G/D, 10-K/10-Q/20-F Investments tables, HKEX annual Note 22 aggregates,
optional broker SOTP overlays, and Google Sheets / CSV export with QoQ charts.

## What this is

- Stock-agnostic orchestration: resolve a parent ticker → fan out filings →
  coalesce history → performance → export → grade / reconcile.
- Valuation SoT is filing-disclosed `$` (13F / investments tables / HK Note 22).
  13G is identity / shares / % only. Broker SOTP and EOD×shares marks are
  display overlays when stamped and excluded from portfolio MV.
- Data flow and gate points: [`docs/pipeline.md`](docs/pipeline.md).

## Quick start

```bash
cp .env.example .env   # fill SEC_EDGAR_USER_AGENT, DB, optional EODHD / Sheets
uv sync
git config core.hooksPath .githooks   # once per clone (see Git hooks)

# 1. Export: refresh live + history, write exports/<slug>_*.csv (+ a new sheet)
uv run python scripts/export_equity_holdings_sheets.py \
  --ticker <PARENT> --live --history --new-sheet
#    --lookback-years 0 = whole book (no calendar cut); --no-sheets = CSV only

# 2. Grade mechanically while iterating (default; no LLM call)
uv run python scripts/grade_holdings_sheet.py --ticker <PARENT>

# 3. Reconcile: re-fetch every cited EDGAR filing, assert the sheet's value
#    string is in it, check data/<parent>_anchors.yaml; writes <slug>_reconcile.csv
uv run python scripts/reconcile_holdings.py --ticker <PARENT>

# 4. Full grade once on the final export (Fable + Codex read the digest;
#    refuses to re-judge an unchanged export unless --force)
uv run python scripts/grade_holdings_sheet.py --ticker <PARENT> \
  --judges fable,codex --judge-mode full
```

Stage-scoped boards (broker / overlay / composition) use the same flags:
`uv run python scripts/swarm_validate_pipeline.py --parent <PARENT> --stages broker,overlay,composition`.

## exports/ (gitignored)

Written by `hidden_stock/quirks/holdings/export.py::write_csvs` for `<slug>` = lowercased parent:

| File | Content |
|---|---|
| `<slug>_equity_holdings.csv` | current open positions |
| `<slug>_equity_holdings_history.csv` | one row per `period_end × investee_ticker` (positions_qoq) |
| `<slug>_portfolio_by_period.csv` | portfolio MV per period (display window) |
| `<slug>_holdings_qoq_chart.csv` | calendar-quarter display stack for the chart |
| `<slug>_returns_by_period.csv` | MV, net flow, MTM, Modified Dietz per period |
| `<slug>_realized_pnl_qoq.csv` | every sell/exit, `cost_method=avg|fifo`, `cost_basis_status`, `cost_basis_note` |
| `<slug>_holding_returns.csv` | per ticker × period weight, Dietz, contribution |
| `<slug>_reported_vs_est.csv` | curated company totals vs summed MTM |

Grade / reconcile side files, same directory: `<slug>_grade_mechanical.json`,
`<slug>_judge_digest.{json,md}`, `<slug>_grade_packet.md`,
`<slug>_grade_{fable,codex}.json`, `<slug>_grade_board.md`,
`<slug>_last_judged.json` (content hash of the last full judge run),
`<slug>_reconcile.csv`.

`cost_basis_status` on realized rows is one of `exact`, `estimated`, `mixed`,
`partial`, `unknown` (`performance.py:109-113`); a sale with no cost lot still
ships with null cost and the reason in `cost_basis_note`.

## Per-parent local YAMLs (gitignored)

Both live in `hidden_stock/quirks/holdings/data/` and are matched by
`**/data/*_cost_basis.yaml` / `**/data/*_anchors.yaml` in `.gitignore`.

| File | Used by | Example |
|---|---|---|
| `<parent>_cost_basis.yaml` | `performance.load_disclosed_cost_basis` — filing-stated lots (13D Item 3/5, 8-K, offering price) for `cost_basis_status=exact` | `data/cost_basis.example.yaml` |
| `<parent>_anchors.yaml` | `reconcile.load_anchors` — hand-typed truths the exported sheet must match | `data/anchors.example.yaml` |

## Exit codes

| Script | 0 | 1 | 2 | 3 |
|---|---|---|---|---|
| `grade_holdings_sheet.py` | board written (verdict is in `<slug>_grade_board.md`, not the exit code) | — | — | `--judge-mode full` asked for but LLM judges skipped: export hash already judged (`--force` to repeat) |
| `reconcile_holdings.py` | every checked row `anchored` / `anchor_match` | any `not_found`, `anchor_mismatch`, `anchor_missing_row` | history CSV missing | — |
| `check_skill_edit_has_code.py` | no rule line added, or a code/test hunk (or logged bypass) accompanies it | rule line added on an agent surface with no code/test hunk | — | — |

## Gates

Each one is a bug that shipped once. Precheck ids appear under `checks` in
`<slug>_grade_mechanical.json`; `fail` in `blocking_issues` makes the board
FAIL, a soft issue makes it NEEDS_WORK.

| Gate / precheck id | Checks | Lives in | Proven by |
|---|---|---|---|
| `assert_unique_period_ticker` | one history row per `period_end × investee_ticker` | `hidden_stock/quirks/holdings/history.py` | `tests/test_serv_period_uniqueness.py::test_assert_unique_period_ticker_raises` |
| `assert_pct_domain` | `ownership_pct` in `(0, 100]`; bad 13G / broker / note rows skipped and logged | `hidden_stock/quirks/holdings/identity.py` | `tests/test_pct_domain.py::test_assert_pct_domain_rejects_out_of_domain` |
| `assert_note_dates_in_period_grid` | every Investments-table column date with disclosed `$` is a `period_end` row | `hidden_stock/quirks/holdings/history.py` | `tests/test_pre13f_note_grid.py::test_build_holdings_history_raises_when_note_column_date_missing_from_grid` |
| `assert_sell_realized_coverage` | every sell/exit has a `cost_method=avg` row in `realized_pnl_qoq` (raised in `performance_frames`) | `hidden_stock/quirks/holdings/performance.py` | `tests/test_performance_returns.py::test_performance_frames_raises_when_sell_has_no_realized_event` |
| `sell_without_realized_row` | same, as a grade precheck on the exported CSVs | `scripts/grade_holdings_sheet.py` | `tests/test_grade_precheck.py::test_sell_without_realized_row_flags_uncovered_sale` |
| `period_grid_gap` (soft) | no calendar quarter missing between min and max `period_end` | `scripts/grade_holdings_sheet.py` | `tests/test_grade_precheck.py::test_period_grid_gap_flags_missing_calendar_quarter` |
| `dietz_sane` (soft) | no period with `abs(dietz_return) > 300%` | `scripts/grade_holdings_sheet.py` | `tests/test_grade_precheck.py::test_dietz_sane_flags_period_over_300pct` |
| `unreconciled_rows` | `<slug>_reconcile.csv` has no failing status; `not_run` when the CSV is absent | `scripts/grade_holdings_sheet.py` | `tests/test_reconcile_holdings.py::test_grade_precheck_flags_unreconciled_rows` |
| unknown ≠ PASS | a board with any `judge:check == unknown` is NEEDS_WORK | `scripts/grade_holdings_sheet.py::write_board`, `hidden_stock/quirks/holdings/swarm_verify.py::merge_board` | `tests/test_grade_precheck.py::test_board_with_unknown_check_is_needs_work_not_pass`, `tests/test_swarm_board.py::test_merge_board_unknown_check_downgrades_pass_to_needs_work` |
| judge once per export | `--judge-mode full` skips (exit 3) when the export content hash was already judged | `hidden_stock/quirks/holdings/judge_digest.py` | `tests/test_judge_digest.py::test_run_grade_full_judges_once_per_export_hash_then_skips_unless_forced` |
| 13G/D event dating | a 13G/D row lands in the first period whose `period_end` is on/after the cover event date | `hidden_stock/quirks/holdings/sec_13g.py`, `history.py` | `tests/test_13g_event_date.py::test_13d_filed_in_45_day_window_lands_in_event_quarter` |
| regex lint | numeric regexes under `hidden_stock/` are digit-anchored (`\d[\d,]*`) | `tests/test_regex_lint.py` | `tests/test_regex_lint.py::test_no_unanchored_numeric_regex_under_hidden_stock` |
| production-shape parsing | parser tests run on raw HTML and on production-flattened text | `tests/helpers.py::shaped` | `tests/test_investments_table_parse.py` |
| skill-edit gate | rule lines on agent surfaces ship with a code or test hunk | `scripts/check_skill_edit_has_code.py` | `tests/test_skill_edit_gate.py::test_doc_only_rule_edit_fails` |

## Git hooks

`.githooks/pre-commit` runs `scripts/check_skill_edit_has_code.py --staged`.
Rule or principle lines added on any agent-rule surface (`SKILL.md`, `*.mdc`,
`.cursor/`, `.claude/`, `.codex/`, `CLAUDE.md`, `AGENTS.md`) need a `.py` change
under `hidden_stock/`, `scripts/` or `tests/` in the same commit. CI
(`.github/workflows/ci.yml`) runs the same script over the PR's `base...head`
range, then `uv run pytest tests/`.

```bash
git config core.hooksPath .githooks
```

Explicit bypass, logged: `--allow-doc-only`, `SKILL_EDIT_ALLOW_DOC_ONLY=1`, or a
`[doc-only]` tag in the commit message.

## Layout

| Path | Role |
|---|---|
| `hidden_stock/assets/equity_holdings.py` | Dagster assets (`equity_holdings`, `equity_holdings_history`, `equity_holdings_export`) |
| `hidden_stock/quirks/holdings/` | Parsers, history, performance, export, judge digest, reconcile |
| `scripts/` | Export / grade / reconcile / swarm-validate / skill-edit-gate CLIs |
| `.cursor/skills/` | Agent skills for holdings sheets + swarm grade |
| `docs/pipeline.md` | Data flow with gate points |

## Privacy / licensing

- Do **not** commit `.env`, OAuth tokens, Google credentials, or paid research PDFs.
- `exports/`, `*.pdf`, `*_cost_basis.yaml`, `*_anchors.yaml` are gitignored.
  Broker catalog in-repo ships **empty**; add your own licensed URLs locally.
- Identity YAML (CUSIP / aliases) is operational config, not a holdings dump.

## License

See repository license / copyright holder terms before redistributing.
