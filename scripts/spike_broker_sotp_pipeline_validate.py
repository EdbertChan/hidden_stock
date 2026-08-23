#!/usr/bin/env python3
"""Spike: run broker SOTP validators at pipeline-like stages and print what we catch.

Simulates Dagster points without requiring a full materialize:

  catalog → parse (per PDF) → FX → overlay(live CSV) → overlay(history CSV)
  → composition UX → optional Fable on one report

Usage:
  uv run python scripts/spike_broker_sotp_pipeline_validate.py
  uv run python scripts/spike_broker_sotp_pipeline_validate.py --fable
  uv run python scripts/spike_broker_sotp_pipeline_validate.py --parent TCEHY
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--parent", default="TCEHY")
    p.add_argument("--fable", action="store_true", help="Also run one Fable verify spike")
    p.add_argument(
        "--exports-dir",
        default=str(_ROOT / "exports"),
        help="Existing export CSVs (live/history/composition)",
    )
    p.add_argument(
        "--out",
        default=str(_ROOT / "exports" / "broker_sotp_validate_spike.json"),
    )
    args = p.parse_args()
    parent = args.parent.upper()

    from hidden_stock.quirks.holdings.broker_sotp import (
        catalog_for_parent,
        extract_pdf_text,
        download_pdf,
        materialize_broker_sotp,
        rows_from_entry,
        default_cache_dir,
    )
    from hidden_stock.quirks.holdings.broker_sotp_validate import (
        build_fable_packet,
        merge_reports,
        run_fable_spike,
        validate_catalog,
        validate_composition_ux,
        validate_fx_roundtrip,
        validate_overlay_history,
        validate_overlay_live,
        validate_parse_rows,
    )

    reports = []
    entries = catalog_for_parent(parent)
    reports.append(validate_catalog(entries))

    # Parse each report separately (Dagster would fail a bad PDF before merge).
    for e in entries:
        try:
            rows = rows_from_entry(e, cache_dir=default_cache_dir())
            reports.append(validate_parse_rows(rows, report_id=str(e.get("id")), min_rows=8))
        except Exception as exc:
            from hidden_stock.quirks.holdings.broker_sotp_validate import Finding, StageReport

            r = StageReport(stage="parse", ok=False)
            r.add(
                Finding(
                    "parse",
                    "fail",
                    "parse_exception",
                    f"{e.get('id')}: {exc}",
                    {"id": e.get("id")},
                )
            )
            reports.append(r)

    all_rows = materialize_broker_sotp([parent])
    reports.append(validate_parse_rows(all_rows, report_id="full_series", min_rows=15))
    reports.append(validate_fx_roundtrip(all_rows))

    import pandas as pd

    exp = Path(args.exports_dir)
    live_path = exp / f"{parent.lower()}_equity_holdings.csv"
    hist_path = exp / f"{parent.lower()}_equity_holdings_history.csv"

    if live_path.is_file():
        live = pd.read_csv(live_path).to_dict(orient="records")
        reports.append(validate_overlay_live(live))
    else:
        from hidden_stock.quirks.holdings.broker_sotp_validate import Finding, StageReport

        r = StageReport(stage="overlay_live", ok=True)
        r.add(
            Finding(
                "overlay_live",
                "warn",
                "no_live_csv",
                f"Missing {live_path} — skip overlay_live (re-export first)",
            )
        )
        reports.append(r)

    if hist_path.is_file():
        hist = pd.read_csv(hist_path).to_dict(orient="records")
        reports.append(validate_overlay_history(hist))
        reports.append(validate_composition_ux(hist))
    else:
        from hidden_stock.quirks.holdings.broker_sotp_validate import Finding, StageReport

        r = StageReport(stage="overlay_history", ok=True)
        r.add(
            Finding(
                "overlay_history",
                "warn",
                "no_hist_csv",
                f"Missing {hist_path} — skip overlay_history",
            )
        )
        reports.append(r)
        r2 = StageReport(stage="composition_ux", ok=True)
        r2.add(
            Finding(
                "composition_ux",
                "warn",
                "no_hist_csv",
                f"Missing {hist_path} — composition lives on QoQ",
            )
        )
        reports.append(r2)

    if args.fable and entries:
        # Prefer latest report with a local cached PDF.
        e = entries[-1]
        try:
            path = download_pdf(str(e["url"]), default_cache_dir())
            text = extract_pdf_text(path)
            # Keep window around strategic investments table.
            low = text.lower()
            idx = low.find("strategic investment")
            snippet = text[max(0, idx) : max(0, idx) + 4000] if idx >= 0 else text[:4000]
            parsed = rows_from_entry(e, text=snippet if len(snippet) > 500 else text)
            packet = build_fable_packet(
                report_id=str(e.get("id")),
                citation=str(e.get("citation") or ""),
                raw_table_snippet=snippet,
                parsed_rows=parsed or rows_from_entry(e),
            )
            reports.append(run_fable_spike(packet))
        except Exception as exc:
            from hidden_stock.quirks.holdings.broker_sotp_validate import Finding, StageReport

            r = StageReport(stage="fable", ok=True)
            r.add(
                Finding(
                    "fable",
                    "warn",
                    "fable_setup_failed",
                    str(exc),
                )
            )
            reports.append(r)

    summary = merge_reports(reports)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    # Human board
    print(f"\n=== broker SOTP pipeline validate spike ({parent}) ===")
    print(f"ok={summary['ok']}  fails={summary['n_fail']}  warns={summary['n_warn']}")
    print(f"wrote {out}\n")
    for stage in summary["stages"]:
        flag = "PASS" if stage["ok"] else "FAIL"
        print(f"## [{flag}] {stage['stage']}")
        for f in stage["findings"]:
            if f["severity"] == "info" and stage["ok"]:
                continue
            print(f"  - {f['severity'].upper()} {f['check_id']}: {f['message']}")
        # always show one info line
        infos = [f for f in stage["findings"] if f["severity"] == "info"]
        if infos:
            print(f"  · {infos[0]['message']}")
        print()
    return 0 if summary["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
