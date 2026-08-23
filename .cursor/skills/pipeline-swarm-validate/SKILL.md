---
name: pipeline-swarm-validate
description: >-
  Validate holdings pipeline stages (broker PDF parse, overlay merge,
  composition export, …) with mechanical FAIL-hard checks plus optional
  Fable+Codex swarm. Invoke as /pipeline-swarm-validate <parent>
  [--stages broker,overlay,composition] [--judges fable,codex].
  Use after broker/composition changes or when a sheet grade FAIL points at
  an earlier pipeline stage. Board FAIL triggers thrash-reflect-automate.
disable-model-invocation: true
---

# Pipeline swarm validate (mechanical + Fable + Codex)

Offline/CLI gate for **pipeline stages**, not only the final sheet.

## Trigger

- `/pipeline-swarm-validate TCEHY`
- `/pipeline-swarm-validate TCEHY --stages broker,overlay,composition`
- `/pipeline-swarm-validate TCEHY --judges fable,codex`

## Stages (v1)

| Alias | Stage id | What it checks |
|---|---|---|
| `broker` | `broker_pdf_parse` | Strategic-investments window; HK$mn units; PDD/Meituan spot |
| `overlay` | `overlay_merge` | Broker `$` on live/history; PDD ~$10–40B; provenance stamps |
| `composition` | `composition_export` | QoQ `composition_*` cols: child ≠ parent aggregate; no separate hk_composition tab |
| stubs | `catalog_parse`, `hk_annual_parse`, `sec_13g_parse`, `sheet_export` | Info-only until wired |

## Workflow

```bash
set -a && source .env && set +a
uv run python scripts/swarm_validate_pipeline.py \
  --parent TCEHY \
  --stages broker,overlay,composition \
  --judges fable,codex
```

Writes:

- `exports/<ticker>_swarm_<stage>_mechanical.json`
- `exports/<ticker>_swarm_<stage>_{fable,codex}.json`
- `exports/<ticker>_swarm_<stage>_board.md` + `.json`
- `exports/<ticker>_swarm_summary.json`

Exit code **1** if any stage board is not PASS (CI-friendly). Does **not** yet
hard-fail Dagster Celery workers (auth blips).

## Rubric (all stages)

1. **Units:** broker `value_to_parent` = **HK$ millions** before FX to USD.
2. **PDF window:** parsed-backed strategic-investments excerpt — not cover fluff.
3. **Parent ≠ child $:** Note 22 aggregate (`parent_aggregate_market_value_usd`)
   is not PDD’s value-to-Tencent (`child_market_value_usd` / broker stamp).
   Whenever child `$` is set, `period_end` + `composition_parent` + parent
   aggregate must be non-null (never CSV `nan` as a parent).
4. **Provenance:** `value_source=broker_sotp` + `report_id=` / `cite=` on overlays.
5. **Tickers:** never emit bare exchange suffixes (`CH`/`KS`/`US`/…) as
   `investee_ticker` — keep numeric code (`002602.SZ`, `259960.KS`).
6. **No invent:** do not invent child `$` via shares×EOD into `market_value_usd`
   / portfolio. Separate `cost_basis_est_*` / `mark_at_filing_est_usd` with
   `value_estimate=eod_at_filing; excluded_from_portfolio_mv` are **OK** —
   estimate ≠ disclosed FV / Note 22 SoT.

## Judge FAIL schema (required)

Every FAIL / NEEDS_WORK must include:

- `recommended_fixes` — 1–3 concrete file/function actions
- `root_cause_class` — e.g. `unit_mixup`, `parent_child_column_confusion`,
  `wrong_pdf_window`, `shallow_13g_scan`
- `avoid_next_time` — mechanical assert that would have blocked ship

## After BOARD FAIL / NEEDS_WORK

Same as sheet swarm — follow `thrash-reflect-automate`:

1. Short `/reflect` from this board + session.
2. Fix the **class** (`principle-assert-invariants-not-last-bug`).
3. Codify: mechanical check and/or this skill rubric line from `avoid_next_time`.
4. Short `/automate-me` (CI/script gate) — do not open Automations editor unless asked.
5. Re-run the failing stage until board PASS.

Pipeline-swarm FAIL counts the same as holdings-sheet-swarm FAIL for thrash.
