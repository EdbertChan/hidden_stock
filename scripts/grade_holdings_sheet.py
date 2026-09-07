#!/usr/bin/env python3
"""Grade equity-holdings sheet/CSV exports with Fable + Codex (independent judges).

  python scripts/grade_holdings_sheet.py --ticker UBER \\
    --sheet-url 'https://docs.google.com/spreadsheets/d/...'

Writes:
  exports/<ticker>_grade_packet.md
  exports/<ticker>_grade_fable.json
  exports/<ticker>_grade_codex.json
  exports/<ticker>_grade_board.md
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

SCHEMA_PATH = (
    _ROOT / ".cursor" / "skills" / "holdings-sheet-swarm-grade" / "grade_schema.json"
)

UBER_10Q_TRUTH = {
    "filing": (
        "https://www.sec.gov/Archives/edgar/data/1543151/"
        "000154315126000032/uber-20260630.htm"
    ),
    "as_of": "2026-06-30",
    "investments_millions": {"DIDIY": 1900, "GRAB": 2020, "AUR": 1763},
    "tolerance": "within 5% or $50M of Investments-table FV / 13F $",
}


def _load_dotenv() -> None:
    env_path = _ROOT / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _read_csv_snippet(path: Path, max_chars: int = 12000) -> str:
    if not path.is_file():
        return f"(missing {path})"
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) > max_chars:
        return text[:max_chars] + f"\n… truncated ({len(text)} chars total)\n"
    return text


def _exit_rows_snippet(history: Path, max_rows: int = 40) -> str:
    """Surface exit / 13g_exit rows so judges do not FAIL 'stale M&A' blindly."""
    if not history.is_file():
        return f"(missing {history})"
    try:
        import pandas as pd

        df = pd.read_csv(history)
        if df.empty:
            return "(no history rows)"
        note = df["note"].astype(str) if "note" in df.columns else pd.Series([""] * len(df))
        action = (
            df["action"].astype(str).str.lower()
            if "action" in df.columns
            else pd.Series([""] * len(df))
        )
        mask = action.eq("exit") | note.str.contains("13g_exit=1", na=False)
        exits = df.loc[mask]
        if exits.empty:
            return "(no action=exit / 13g_exit=1 rows in history)"
        cols = [
            c
            for c in (
                "period_end",
                "investee_ticker",
                "action",
                "shares_held",
                "ownership_pct",
                "market_value_usd",
                "accession_no",
                "filing_date",
                "note",
            )
            if c in exits.columns
        ]
        return exits[cols].head(max_rows).to_csv(index=False)
    except Exception as e:
        return f"(exit sample skipped: {e})"


def mechanical_precheck(
    history_csv: Path,
    portfolio_csv: Path,
    *,
    parent: str,
) -> dict:
    """Pandas checks that must not wait for an LLM judge (SERV-class bugs).

    Parent-scoped: Uber 10-Q DIDIY/GRAB/AUR anchors apply **only** when parent is UBER.
    """
    parent_u = str(parent or "").strip().upper()
    issues: list[dict] = []
    # needs_work-class findings: reported as minor_issues, verdict needs_work.
    soft_issues: list[dict] = []
    checks = {
        "didi_2026_06_30_fv": "n/a" if parent_u != "UBER" else "unknown",
        "grab_aurora_vs_10q": "n/a" if parent_u != "UBER" else "unknown",
        "aur_one_per_period": "unknown",
        "no_otc_invent_marks": "unknown",
        "no_share_invent": "unknown",
        "no_placeholder_tickers": "unknown",
        "chart_ranking_sane": "unknown",
        "parent_scoped": "pass",
    }
    if not history_csv.is_file():
        issues.append(
            {
                "id": "missing_history_csv",
                "severity": f"missing {history_csv}",
                "evidence": str(history_csv),
            }
        )
        return {
            "judge": "mechanical",
            "verdict": "fail",
            "score": 0,
            "blocking_issues": issues,
            "minor_issues": [],
            "what_looks_good": [],
            "checks": checks,
            "summary": "Missing history CSV",
            "parent": parent_u,
        }

    import pandas as pd

    hist = pd.read_csv(history_csv)
    # Align uniqueness with export: stamp + coalesce before grouping
    try:
        from hidden_stock.quirks.holdings.lookback import coalesce_period_ticker

        hist = pd.DataFrame(coalesce_period_ticker(hist.to_dict(orient="records")))
    except Exception:
        pass
    if "investee_ticker" in hist.columns and "period_end" in hist.columns:
        g = (
            hist.assign(
                investee_ticker=hist["investee_ticker"].astype(str).str.upper(),
                period_end=hist["period_end"].astype(str),
            )
            .groupby(["period_end", "investee_ticker"], dropna=False)
            .size()
        )
        # Ignore literal nan/none keys (still-unstamped blanks) — those are missing-ticker, not dups
        dups = g[(g > 1) & (~g.index.get_level_values(1).isin(["NAN", "NONE", "NAT", "", "NAN"]))]
        # Also drop float-NaN group keys (blank tickers before PRIV_ stamp)
        dups = dups[
            [
                not (isinstance(t, float) and t != t)  # NaN != NaN
                and str(t).upper() not in {"NAN", "NONE", "NAT", ""}
                for (_pe, t) in dups.index
            ]
        ]
        if len(dups):
            sample = ", ".join(f"{pe}/{t}×{int(n)}" for (pe, t), n in dups.head(10).items())
            issues.append(
                {
                    "id": "duplicate_ticker_same_period",
                    "severity": "history has >1 row for same period_end × ticker",
                    "evidence": sample,
                }
            )
            checks["aur_one_per_period"] = "fail"
        else:
            checks["aur_one_per_period"] = "pass"

    if (
        parent_u == "UBER"
        and portfolio_csv.is_file()
        and "period_end" in pd.read_csv(portfolio_csv, nrows=0).columns
    ):
        port = pd.read_csv(portfolio_csv)
        june = port[port["period_end"].astype(str) == "2026-06-30"]
        if len(june):
            by = {
                str(r.investee_ticker).upper(): float(r.market_value_usd)
                for r in june.itertuples()
                if hasattr(r, "market_value_usd") and pd.notna(r.market_value_usd)
            }
            truth = UBER_10Q_TRUTH["investments_millions"]
            tol_usd = 50_000_000.0

            def _ok(ticker: str) -> bool:
                if ticker not in by:
                    return False
                expected = truth[ticker] * 1_000_000.0
                return abs(by[ticker] - expected) <= max(tol_usd, 0.05 * expected)

            checks["didi_2026_06_30_fv"] = "pass" if _ok("DIDIY") else "fail"
            checks["grab_aurora_vs_10q"] = (
                "pass" if (_ok("GRAB") and _ok("AUR")) else "fail"
            )
            if checks["didi_2026_06_30_fv"] == "fail":
                issues.append(
                    {
                        "id": "didi_fv_mismatch",
                        "severity": "DIDIY @ 2026-06-30 not ≈ $1.9B Investments FV",
                        "evidence": str(by.get("DIDIY")),
                    }
                )
            checks["chart_ranking_sane"] = "pass"
    elif parent_u != "UBER":
        # Non-Uber: still sanity-check latest portfolio ranking exists if CSV present
        if portfolio_csv.is_file() and len(pd.read_csv(portfolio_csv)):
            checks["chart_ranking_sane"] = "pass"

    if "note" in hist.columns:
        bad = hist[
            hist["note"].astype(str).str.contains(
                r"priced=shares\*eodhd|shares\*price|share×price",
                case=False,
                regex=True,
                na=False,
            )
        ]
        if len(bad):
            checks["no_otc_invent_marks"] = "fail"
            issues.append(
                {
                    "id": "otc_invent_marker",
                    "severity": "history notes still contain OTC invent pricing markers",
                    "evidence": str(
                        bad[["period_end", "investee_ticker", "note"]].head(5).to_dict()
                    ),
                }
            )
        else:
            checks["no_otc_invent_marks"] = "pass"

        # Non-null $ with only beneficial-ownership provenance = invent-or-mislabel
        mv = pd.to_numeric(hist.get("market_value_usd"), errors="coerce")
        notes = hist["note"].astype(str)
        sole_13g = (
            (mv.notna())
            & (mv > 0)
            & notes.str.contains(r"source=13g", case=False, regex=True, na=False)
            & ~notes.str.contains(
                r"investments_table|13f|value_source=|20f_note|10[kq]_note",
                case=False,
                regex=True,
                na=False,
            )
        )
        if sole_13g.any():
            sample = hist.loc[
                sole_13g, ["period_end", "investee_ticker", "market_value_usd", "note"]
            ].head(5)
            issues.append(
                {
                    "id": "beneficial_ownership_used_as_value_source",
                    "severity": (
                        "non-null market_value with only source=13g in note "
                        "(13G is identity/%/shares — $ must cite 13F or Investments table)"
                    ),
                    "evidence": sample.to_dict(orient="records"),
                }
            )
            checks["no_otc_invent_marks"] = "fail"

        # False 13G exit: Item 5 boilerplate stamped 13g_exit while %/shares still >0.
        notes = hist["note"].astype(str)
        if "ownership_pct" in hist.columns:
            pct_n = pd.to_numeric(hist["ownership_pct"], errors="coerce")
            sh_n = (
                pd.to_numeric(hist["shares_held"], errors="coerce")
                if "shares_held" in hist.columns
                else pd.Series(dtype=float)
            )
            false_exit = notes.str.contains(r"13g_exit=1", case=False, regex=True, na=False) & (
                ((pct_n.notna()) & (pct_n > 0)) | ((sh_n.notna()) & (sh_n > 0))
            )
            if false_exit.any():
                sample = hist.loc[
                    false_exit,
                    [c for c in ("period_end", "investee_ticker", "shares_held", "ownership_pct", "note") if c in hist.columns],
                ].head(5)
                issues.append(
                    {
                        "id": "false_13g_exit_positive_stake",
                        "severity": (
                            "13g_exit=1 stamped while shares_held or ownership_pct still >0 "
                            "(Item 5 form-instruction false positive)"
                        ),
                        "evidence": sample.to_dict(orient="records"),
                    }
                )
                checks["no_false_13g_exit"] = "fail"
            else:
                checks["no_false_13g_exit"] = "pass"

        # PRIV_* / private_note must not hide a name that aliases to a public ticker.
        try:
            from hidden_stock.quirks.holdings.identity import resolve_issuer_ticker

            priv_mask = hist["investee_ticker"].astype(str).str.upper().str.startswith(
                "PRIV_"
            ) | hist["note"].astype(str).str.contains(
                r"ticker=private_note", case=False, regex=True, na=False
            )
            # HK annual aggregates are intentional private_note identities.
            priv_mask = priv_mask & ~hist["investee_ticker"].astype(str).str.upper().str.startswith(
                "PRIV_HK_"
            )
            mis = []
            for r in hist.loc[priv_mask].itertuples():
                name = getattr(r, "investee_name", None)
                resolved = resolve_issuer_ticker(name, None)
                if (
                    resolved
                    and not str(resolved).upper().startswith("PRIV_")
                    and str(resolved).upper()
                    != str(getattr(r, "investee_ticker", "") or "").upper()
                ):
                    mis.append(
                        {
                            "period_end": getattr(r, "period_end", None),
                            "investee_ticker": getattr(r, "investee_ticker", None),
                            "investee_name": name,
                            "should_be": resolved,
                        }
                    )
                    if len(mis) >= 5:
                        break
            if mis:
                issues.append(
                    {
                        "id": "public_ticker_misclassified_as_private",
                        "severity": (
                            "PRIV_/private_note used for a name that resolves to a "
                            "public ticker (null-alias escape hatch)"
                        ),
                        "evidence": mis,
                    }
                )
                checks["no_private_escape_hatch"] = "fail"
            else:
                checks["no_private_escape_hatch"] = "pass"
        except Exception as e:
            checks["no_private_escape_hatch"] = f"skip:{e}"

        # Invent on null-$ rows is still FAIL (BILI / Neutron / ANT class).
        presence = hist[
            hist["note"].astype(str).str.contains(
                r"shares_proxy=presence", case=False, regex=True, na=False
            )
        ]
        if len(presence):
            issues.append(
                {
                    "id": "shares_proxy_presence_invent",
                    "severity": "history invents shares_proxy=presence (forbidden continuity hatch)",
                    "evidence": str(
                        presence[["period_end", "investee_ticker", "shares_held", "note"]]
                        .head(5)
                        .to_dict(orient="records")
                    ),
                }
            )
            checks["no_share_invent"] = "fail"
        else:
            checks["no_share_invent"] = "pass"

        # 13G/note-overlay exits never invent a $0 value (test_price_history_rows
        # regression, 2026-08-22): a name that only ever appeared via 13G/note
        # overlay and exits has no filed $ at all — forcing market_value_usd=0.0
        # fabricates a dollar figure the filing never disclosed. Only a real
        # 13F-sourced exit may be forced to 0.0.
        overlay_mask = hist["note"].astype(str).str.contains(
            r"source=13g|20f_note|10k_note|10q_note|ticker=private_note",
            case=False,
            regex=True,
            na=False,
        )
        exit_mask = hist["action"].astype(str).str.lower().eq("exit")
        mv = pd.to_numeric(hist["market_value_usd"], errors="coerce")
        overlay_exit_invented_zero = hist[overlay_mask & exit_mask & mv.eq(0.0)]
        if len(overlay_exit_invented_zero):
            issues.append(
                {
                    "id": "overlay_exit_invented_zero",
                    "severity": (
                        "13G/note-overlay exit row forces market_value_usd=0.0 "
                        "(should be null — no filed $ to zero out)"
                    ),
                    "evidence": str(
                        overlay_exit_invented_zero[
                            ["period_end", "investee_ticker", "market_value_usd", "note"]
                        ]
                        .head(5)
                        .to_dict(orient="records")
                    ),
                }
            )
            checks["no_overlay_exit_invent"] = "fail"
        else:
            checks["no_overlay_exit_invent"] = "pass"

        if "ownership_pct" in hist.columns and "shares_held" in hist.columns:
            sh = pd.to_numeric(hist["shares_held"], errors="coerce")
            pct = pd.to_numeric(hist["ownership_pct"], errors="coerce")
            stuffed = (
                sh.notna()
                & pct.notna()
                & (sh > 0)
                & (pct > 0)
                & ((sh - pct).abs() < 1e-6)
            )
            if stuffed.any():
                sample = hist.loc[
                    stuffed, ["period_end", "investee_ticker", "shares_held", "ownership_pct", "note"]
                ].head(5)
                issues.append(
                    {
                        "id": "ownership_pct_stuffed_into_shares_held",
                        "severity": "shares_held equals ownership_pct (Neutron/EM class invent)",
                        "evidence": sample.to_dict(orient="records"),
                    }
                )
                checks["no_share_invent"] = "fail"

        # Exchange-like first-word fakes (not PRIV_ and not ticker=private_note)
        if "investee_ticker" in hist.columns:
            tcol = hist["investee_ticker"].astype(str).str.upper()
            notes = hist["note"].astype(str)
            fakeish = (
                tcol.isin({"ANT", "CHINA", "MANGO", "MEINIAN", "YTO", "ALIEXPRESS", "MOONSHOT"})
                & ~tcol.str.startswith("PRIV_", na=False)
                & ~notes.str.contains(r"ticker=private_note", case=False, regex=True, na=False)
            )
            if fakeish.any():
                sample = hist.loc[
                    fakeish, ["period_end", "investee_ticker", "investee_name", "note"]
                ].head(5)
                issues.append(
                    {
                        "id": "placeholder_exchange_like_ticker",
                        "severity": "invented exchange-like ticker from note name (use PRIV_<slug>)",
                        "evidence": sample.to_dict(orient="records"),
                    }
                )
                checks["no_placeholder_tickers"] = "fail"
            else:
                checks["no_placeholder_tickers"] = "pass"
    else:
        checks.setdefault("no_share_invent", "unknown")
        checks.setdefault("no_placeholder_tickers", "unknown")

    # Display-stack basis cliffs: broker $ preferred over EOD mark (PDD 2023-03 class).
    chart_csv = history_csv.parent / history_csv.name.replace(
        "_equity_holdings_history.csv", "_holdings_qoq_chart.csv"
    )
    checks.setdefault("no_display_basis_cliff", "unknown")
    if chart_csv.is_file():
        try:
            chart = pd.read_csv(chart_csv)
            cliff_hits: list[str] = []
            if "period_end" in chart.columns and len(chart) >= 2:
                chart = chart.sort_values("period_end", kind="mergesort")
                tickers = [c for c in chart.columns if c != "period_end"]
                for t in tickers:
                    series = pd.to_numeric(chart[t], errors="coerce")
                    for i in range(1, len(series)):
                        a, b = series.iloc[i - 1], series.iloc[i]
                        if pd.isna(a) or pd.isna(b) or a <= 0 or b <= 0:
                            continue
                        # Drop >70% then rebound next period → basis thrash, not a sale.
                        if b < 0.3 * a:
                            nxt = series.iloc[i + 1] if i + 1 < len(series) else None
                            if nxt is not None and pd.notna(nxt) and nxt > 0.7 * a:
                                pe = str(chart["period_end"].iloc[i])
                                cliff_hits.append(f"{t}@{pe}: {a:.0f}→{b:.0f}→{nxt:.0f}")
            if cliff_hits:
                checks["no_display_basis_cliff"] = "fail"
                checks["chart_ranking_sane"] = "fail"
                issues.append(
                    {
                        "id": "display_basis_cliff",
                        "severity": (
                            "holdings_qoq_chart QoQ cliff then rebound "
                            "(prefer mark_at_filing_est over broker market_value)"
                        ),
                        "evidence": cliff_hits[:8],
                    }
                )
            else:
                checks["no_display_basis_cliff"] = "pass"
        except Exception as e:
            checks["no_display_basis_cliff"] = "unknown"
            issues.append(
                {
                    "id": "display_basis_cliff_check_error",
                    "severity": f"could not scan chart CSV: {e}",
                    "evidence": str(chart_csv),
                }
            )
    else:
        checks["no_display_basis_cliff"] = "n/a"

    # Blank investee_ticker on named public rows (Bitauto / 58.com class).
    checks.setdefault("no_blank_public_ticker", "unknown")
    if "investee_ticker" in hist.columns and "investee_name" in hist.columns:
        tcol = hist["investee_ticker"]
        blank = tcol.isna() | (tcol.astype(str).str.strip() == "") | (
            tcol.astype(str).str.upper().isin({"NAN", "NONE", "NAT"})
        )
        notes = hist["note"].astype(str) if "note" in hist.columns else ""
        named = hist["investee_name"].fillna("").astype(str).str.strip() != ""
        priv = notes.str.contains(r"ticker=private_note", case=False, regex=True, na=False)
        bad_blank = blank & named & ~priv
        if bad_blank.any():
            sample = hist.loc[
                bad_blank,
                [c for c in ("period_end", "investee_name", "cusip", "action", "note") if c in hist.columns],
            ].head(8)
            issues.append(
                {
                    "id": "blank_public_ticker",
                    "severity": (
                        "blank investee_ticker on named public investee "
                        "(stamp via CUSIP/alias — Bitauto/58.com class)"
                    ),
                    "evidence": sample.to_dict(orient="records"),
                }
            )
            checks["no_blank_public_ticker"] = "fail"
        else:
            checks["no_blank_public_ticker"] = "pass"

    checks.setdefault("unreconciled_rows", "unknown")
    reconcile_csv = history_csv.parent / f"{parent_u.lower().replace('-', '')}_reconcile.csv"
    if reconcile_csv.is_file():
        try:
            from hidden_stock.quirks.holdings.reconcile import (
                FAILING_STATUSES,
                read_reconcile_csv,
            )

            rec = read_reconcile_csv(reconcile_csv)
            bad_rows = [r for r in rec if r.get("status") in FAILING_STATUSES]
            if bad_rows:
                sample = "; ".join(
                    f"{r.get('status')} {r.get('period_end')}/{r.get('investee_ticker')} "
                    f"searched={r.get('searched')}"
                    for r in bad_rows[:8]
                )
                issues.append(
                    {
                        "id": "unreconciled_rows",
                        "severity": (
                            f"{len(bad_rows)} reconcile row(s) not_found / anchor mismatch "
                            f"in {reconcile_csv.name} (scripts/reconcile_holdings.py)"
                        ),
                        "evidence": sample,
                    }
                )
                checks["unreconciled_rows"] = "fail"
            else:
                checks["unreconciled_rows"] = "pass"
        except Exception as e:
            issues.append(
                {
                    "id": "unreconciled_rows_check_error",
                    "severity": f"could not read {reconcile_csv}",
                    "evidence": f"{type(e).__name__}: {e}",
                }
            )
            checks["unreconciled_rows"] = "fail"
    else:
        checks["unreconciled_rows"] = "not_run"

    # Quarterly grid must be contiguous: a missing calendar quarter between
    # min and max period_end is a dropped period (note column / 13F not on grid).
    checks.setdefault("period_grid_gap", "unknown")
    if "period_end" in hist.columns:
        pes = sorted({str(x)[:10] for x in hist["period_end"].dropna().astype(str) if str(x)[:10]})
        q_ends = {"03-31", "06-30", "09-30", "12-31"}
        if not pes:
            checks["period_grid_gap"] = "n/a"
        elif any(pe[5:] not in q_ends for pe in pes):
            # 13G-date grid (HK aggregate parents): not a quarterly grid.
            checks["period_grid_gap"] = "n/a"
        else:
            def _q(pe: str) -> int:
                return int(pe[:4]) * 4 + (int(pe[5:7]) - 1) // 3

            def _pe(qi: int) -> str:
                y, q = divmod(qi, 4)
                return f"{y}-{('03-31', '06-30', '09-30', '12-31')[q]}"

            have = {_q(pe) for pe in pes}
            gaps = [_pe(qi) for qi in range(min(have), max(have) + 1) if qi not in have]
            if gaps:
                soft_issues.append(
                    {
                        "id": "period_grid_gap",
                        "severity": (
                            "calendar quarter(s) missing between min and max period_end "
                            "(period dropped from grid)"
                        ),
                        "evidence": gaps[:24],
                    }
                )
                checks["period_grid_gap"] = "fail"
            else:
                checks["period_grid_gap"] = "pass"

    # Every sell/exit must have a cost_method=avg row in realized_pnl_qoq
    # (the realized tab once dropped no-cost-lot sales via a silent `continue`).
    checks.setdefault("sell_without_realized_row", "unknown")
    realized_csv = history_csv.parent / history_csv.name.replace(
        "_equity_holdings_history.csv", "_realized_pnl_qoq.csv"
    )
    if {"action", "shares_delta", "period_end", "investee_ticker"} <= set(hist.columns):
        action_l = hist["action"].astype(str).str.lower()
        delta = pd.to_numeric(hist["shares_delta"], errors="coerce")
        sells = hist[action_l.isin({"sell", "exit"}) & delta.notna() & (delta < 0)]
        if not len(sells):
            checks["sell_without_realized_row"] = "pass"
        elif realized_csv.is_file():
            realized = pd.read_csv(realized_csv)
            covered: set[tuple[str, str]] = set()
            if {"period_end", "investee_ticker"} <= set(realized.columns):
                method = (
                    realized["cost_method"].astype(str).str.lower()
                    if "cost_method" in realized.columns
                    else pd.Series(["avg"] * len(realized), index=realized.index)
                )
                for r in realized[method == "avg"].itertuples():
                    covered.add(
                        (str(r.period_end), str(r.investee_ticker).strip().upper())
                    )
            uncovered = [
                f"{r.period_end}/{str(r.investee_ticker).strip().upper()}"
                for r in sells.itertuples()
                if (str(r.period_end), str(r.investee_ticker).strip().upper()) not in covered
            ]
            if uncovered:
                issues.append(
                    {
                        "id": "sell_without_realized_row",
                        "severity": (
                            "positions_qoq sell/exit with no cost_method=avg row in "
                            "realized_pnl_qoq (sale silently dropped from P&L)"
                        ),
                        "evidence": uncovered[:20],
                    }
                )
                checks["sell_without_realized_row"] = "fail"
            else:
                checks["sell_without_realized_row"] = "pass"
        else:
            # Sales exist but no realized CSV to check against: not proven.
            checks["sell_without_realized_row"] = "unknown"

    # Dietz sanity: |return| > 300% in one period is a phantom row / flow
    # mis-book (UBER 2018-12-31 read 1,952% from a narrative comma-number).
    checks.setdefault("dietz_sane", "unknown")
    returns_csv = history_csv.parent / history_csv.name.replace(
        "_equity_holdings_history.csv", "_returns_by_period.csv"
    )
    if returns_csv.is_file():
        rets = pd.read_csv(returns_csv)
        if {"period_end", "dietz_return"} <= set(rets.columns):
            dz = pd.to_numeric(rets["dietz_return"], errors="coerce")
            wild = rets[dz.notna() & (dz.abs() > 3.0)]
            if len(wild):
                soft_issues.append(
                    {
                        "id": "dietz_sane",
                        "severity": "|dietz_return| > 300% in a period (phantom row / flow mis-book)",
                        "evidence": [
                            f"{r.period_end}: {float(r.dietz_return) * 100:.1f}%"
                            for r in wild.itertuples()
                        ][:12],
                    }
                )
                checks["dietz_sane"] = "fail"
            else:
                checks["dietz_sane"] = "pass"
        else:
            checks["dietz_sane"] = "unknown"

    good = []
    if not issues:
        good.append(f"Mechanical uniqueness + invent checks passed for parent={parent_u}")
        if parent_u == "UBER":
            good.append("UBER June-2026 FV anchors checked")
        else:
            good.append("Uber DIDIY/GRAB/AUR anchors skipped (wrong parent)")

    verdict = "fail" if issues else "needs_work" if soft_issues else "pass"
    return {
        "judge": "mechanical",
        "verdict": verdict,
        "score": {"fail": 0, "needs_work": 60, "pass": 100}[verdict],
        "blocking_issues": issues,
        "minor_issues": soft_issues,
        "what_looks_good": good,
        "checks": checks,
        "parent": parent_u,
        "summary": (
            f"Mechanical precheck failed for {parent_u}"
            if issues
            else f"Mechanical precheck needs work for {parent_u}: "
            + ", ".join(i["id"] for i in soft_issues)
            if soft_issues
            else f"Mechanical precheck passed for {parent_u}"
        ),
    }


def build_packet(
    *,
    ticker: str,
    sheet_url: str | None,
    out_dir: Path,
    digest_text: str | None = None,
    attach_csv: bool = False,
) -> Path:
    """Write the judge packet. Judges see the derived digest (every row,
    aggregated) — raw CSV head-slices are attached only with ``attach_csv``."""
    t = ticker.lower()
    parent_u = str(ticker or "").strip().upper()
    portfolio = out_dir / f"{t}_portfolio_by_period.csv"
    history = out_dir / f"{t}_equity_holdings_history.csv"
    packet = out_dir / f"{t}_grade_packet.md"

    lines = [
        f"# Holdings sheet grade packet — {ticker}",
        "",
        f"- **parent (resolved): {parent_u}** — grade THIS equity only; do not apply another parent's anchors",
        f"- sheet_url: {sheet_url or '(none)'}",
        f"- portfolio_csv: `{portfolio}`",
        f"- history_csv: `{history}`",
        "",
        "## Ground truth (do not invent)",
        "",
        "Valuation order: Form 13F market_value_usd → 10-Q/10-K/20-F Investments "
        "table FV ($M×1e6) → else null. Schedule 13G = identity/%/shares only.",
        "",
    ]
    if parent_u == "UBER":
        lines.extend(
            [
                "Uber 10-Q Investments (millions) @ 2026-06-30:",
                f"- Didi/DIDIY: **{UBER_10Q_TRUTH['investments_millions']['DIDIY']}**",
                f"- Grab/GRAB: **{UBER_10Q_TRUTH['investments_millions']['GRAB']}**",
                f"- Aurora/AUR: **{UBER_10Q_TRUTH['investments_millions']['AUR']}**",
                f"- Filing: {UBER_10Q_TRUTH['filing']}",
                f"- Tolerance: {UBER_10Q_TRUTH['tolerance']}",
                "",
            ]
        )
    else:
        lines.extend(
            [
                f"Parent-specific note: this packet is for **{parent_u}**.",
                "Do **not** require Uber DIDIY/GRAB/AUR fair values.",
                "Judge holdings/values that belong to this parent’s filings only.",
                "If this parent is 13G/D-only with no Investments-table FV and no "
                "HKEX annual FV (`value_source=hk_annual_note22`), "
                "**header-only portfolio/returns/chart CSVs are PASS** — null `$` "
                "must be omitted (never invent OTC×shares or empty-row padding).",
                "Still FAIL stale holds after known M&A/take-private close dates "
                "(GLUU, FTCH class) and blank public tickers. Before FAIL on "
                "stale M&A, search history for `action=exit` / `13g_exit=1` on that "
                "ticker — an explicit exit row means the name is not stale.",
                "TCEHY HK annual aggregate FV rows (`PRIV_HK_*`, source=hk_annual) "
                "are allowed `$` when stamped `value_source=hk_annual_note22`.",
                "CAGR: `years` = calendar span from start of the latest unbroken "
                "Dietz segment (first portfolio period or last "
                "`series_break=coverage_basis`) through last cum-growth period; "
                "do not put `years` in the `cum_growth_index` column of the CAGR footer.",
                "Coverage basis switch (associates FV → all-listed investees FV) must "
                "show `series_break=coverage_basis` — that is PASS, not a fake cash FAIL.",
                "HK composition: named 13G rows may stamp `composition_parent=PRIV_HK_*` "
                "+ `not_fv_allocation` with null child `$` — PASS. Inventing child `$` "
                "into `market_value_usd` from shares×EOD is FAIL. "
                "`PRIV_HK_*_RESIDUAL` is excluded from portfolio MV.",
                "Broker SOTP child `$` in `market_value_usd` with "
                "`value_source=broker_sotp` + `excluded_from_portfolio_mv` is **PASS** "
                "(display / composition only — not Dietz / Note 22 SoT). Do **not** "
                "FAIL merely because broker `$` exists outside 13F/Investments hierarchy.",
                "13G EOD estimates: `mark_at_filing_est_usd` / `cost_basis_est_*` with "
                "`value_estimate=eod_at_filing; excluded_from_portfolio_mv` and "
                "**null** `market_value_usd` are **PASS**. Putting that estimate into "
                "`market_value_usd` / portfolio is FAIL. Chart may stack marks + broker "
                "on calendar quarters (`basis=display_estimate_or_broker`) — PASS.",
                "",
            ]
        )
    lines.extend(
        [
            "Also FAIL if **any** ticker appears twice in the same period_end "
            "(13F exit + continuing 13G is still a duplicate — not only AUR overlap).",
            "",
            "shares_held rules: real 13F/parsed counts only. Ownership_% must live in "
            "`ownership_pct` (note may say `qoq_continuity=ownership_pct`) — never "
            "as shares_held. Never invent exchange-like placeholders (ANT/CHINA/first-word). "
            "**PRIV_<slug>** + `ticker=private_note` is the allowed private-note identity "
            "(not a FAIL). Exit rows cite the period grid accession (disappearance filing). "
            "13G/note rows: filing_date must match the cited accession’s as_of (not the 13F grid date).",
            "",
            "## Large QoQ $ moves (verify share counts)",
            "",
        ]
    )
    # Surface big value jumps with share deltas so judges can confirm 13F buys.
    try:
        import pandas as pd

        if history.is_file():
            hdf = pd.read_csv(history)
            if {"period_end", "investee_ticker", "shares_held", "market_value_usd"}.issubset(
                hdf.columns
            ):
                hdf = hdf.sort_values(["investee_ticker", "period_end"])
                jumps = []
                for tkr, g in hdf.groupby(hdf["investee_ticker"].astype(str).str.upper()):
                    if tkr in {"", "NAN", "NONE"}:
                        continue
                    prev = None
                    for r in g.itertuples():
                        if prev is not None and pd.notna(r.market_value_usd) and pd.notna(
                            prev.market_value_usd
                        ):
                            dlt = float(r.market_value_usd) - float(prev.market_value_usd)
                            if abs(dlt) >= 100_000_000:
                                jumps.append(
                                    f"- {tkr} {prev.period_end}→{r.period_end}: "
                                    f"${float(prev.market_value_usd):,.0f}→${float(r.market_value_usd):,.0f} "
                                    f"(Δ${dlt:,.0f}); shares "
                                    f"{prev.shares_held}→{r.shares_held} "
                                    f"action={getattr(r, 'action', '')} "
                                    f"acc={getattr(r, 'accession_no', '')}"
                                )
                        prev = r
                if jumps:
                    lines.extend(jumps[:20])
                else:
                    lines.append("(no ≥$100M QoQ value jumps)")
                lines.append("")
    except Exception as e:
        lines.extend([f"(jump audit skipped: {e})", ""])

    if digest_text is None:
        from hidden_stock.quirks.holdings.judge_digest import write_digest

        digest_text, _data, _json_path = write_digest(out_dir, t)
    lines.extend(
        [
            "## Export digest (complete — every row aggregated; no CSV slice)",
            "",
            "The coverage grid, sell/realized table, per-period returns and "
            "provenance counts below are derived from ALL rows of every export "
            "CSV. Treat a `-` cell, a `realized=MISSING` sell, or a missing "
            "quarter here as real evidence; nothing is hidden past a slice. "
            "`since_last_digest` shows what changed since the previous grade "
            "iteration.",
            "",
            digest_text.rstrip(),
            "",
        ]
    )
    if attach_csv:
        lines.extend(
            [
                "## exit / 13g_exit rows (sample — check before stale-M&A FAIL)",
                "",
                "```csv",
                _exit_rows_snippet(history),
                "```",
                "",
                "## portfolio_by_period.csv (raw head-slice attachment)",
                "",
                "```csv",
                _read_csv_snippet(portfolio),
                "```",
                "",
                "## equity_holdings_history.csv (raw head-slice attachment)",
                "",
                "```csv",
                _read_csv_snippet(history, max_chars=24000),
                "```",
                "",
            ]
        )
        for label, name in (
            ("returns_by_period", f"{t}_returns_by_period.csv"),
            ("realized_pnl_qoq", f"{t}_realized_pnl_qoq.csv"),
            ("reported_vs_est", f"{t}_reported_vs_est.csv"),
        ):
            path = out_dir / name
            if path.is_file():
                lines.extend(
                    [
                        f"## {label}.csv (raw head-slice attachment)",
                        "",
                        "```csv",
                        _read_csv_snippet(path, max_chars=8000),
                        "```",
                        "",
                    ]
                )
    lines.extend(
        [
            "## Your job",
            "",
            f"You are an independent judge for parent **{parent_u}** only.",
            "Grade this export against the rubric for that equity.",
            "Return ONLY JSON matching the provided schema. Be harsh on invented "
            "OTC marks and duplicate period×ticker rows. Do not fail a non-Uber "
            "parent for missing DIDIY/GRAB/AUR.",
            "",
        ]
    )

    # Preserve rest of original "Your job" if we truncated — check original had more
    packet.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return packet


def _judge_prompt(packet_text: str, judge_name: str) -> str:
    from hidden_stock.quirks.holdings.swarm_verify import judge_prompt_suffix

    return (
        f"You are judge={judge_name} grading an equity-holdings spreadsheet export.\n"
        "Apply the rubric in the packet strictly. Output JSON only (no markdown).\n"
        + judge_prompt_suffix()
        + "\n"
        + f"{packet_text}\n"
    )


def run_fable(packet_text: str, schema: dict) -> dict:
    from hidden_stock.quirks.holdings.swarm_verify import run_fable as _run

    return _run(_judge_prompt(packet_text, "fable"), schema, wrap_prompt=False)


def run_codex(packet_text: str, schema_path: Path) -> dict:
    from hidden_stock.quirks.holdings.swarm_verify import run_codex as _run

    return _run(
        _judge_prompt(packet_text, "codex"), schema_path, wrap_prompt=False
    )


def _parse_json_response(text: str, *, judge: str) -> dict:
    from hidden_stock.quirks.holdings.swarm_verify import parse_json_response

    return parse_json_response(text, judge=judge)


def unknown_check_ids(results: list[dict]) -> list[str]:
    """`judge:check` ids whose value is literally "unknown" (never evaluated)."""
    out: list[str] = []
    for r in results:
        judge = str(r.get("judge") or "judge")
        for cid, val in (r.get("checks") or {}).items():
            if str(val).strip().lower() == "unknown":
                out.append(f"{judge}:{cid}")
    return out


def write_board(ticker: str, out_dir: Path, results: list[dict], sheet_url: str | None) -> Path:
    path = out_dir / f"{ticker.lower()}_grade_board.md"
    lines = [
        f"# Holdings grade board — {ticker}",
        "",
        f"Sheet: {sheet_url or '(csv only)'}",
        "",
        "| Judge | Verdict | Score |",
        "|---|---|---|",
    ]
    for r in results:
        lines.append(
            f"| {r.get('judge')} | **{r.get('verdict')}** | {r.get('score')} |"
        )
    lines.append("")
    for r in results:
        lines.append(f"## {r.get('judge')}")
        lines.append("")
        lines.append(r.get("summary") or "")
        lines.append("")
        lines.append("Checks: " + json.dumps(r.get("checks") or {}))
        lines.append("")
        if r.get("blocking_issues"):
            lines.append("### Blocking")
            for issue in r["blocking_issues"]:
                lines.append(
                    f"- **{issue.get('id')}**: {issue.get('severity')} — {issue.get('evidence')}"
                )
            lines.append("")
        if r.get("minor_issues"):
            lines.append("### Minor")
            for issue in r["minor_issues"]:
                lines.append(
                    f"- **{issue.get('id')}**: {issue.get('severity')} — {issue.get('evidence')}"
                )
            lines.append("")
        if r.get("what_looks_good"):
            lines.append("### Good")
            for g in r["what_looks_good"]:
                lines.append(f"- {g}")
            lines.append("")

    verdicts = {r.get("verdict") for r in results}
    unknown_checks = unknown_check_ids(results)
    if "fail" in verdicts:
        board = "BOARD: FAIL"
    elif "needs_work" in verdicts or len(verdicts) > 1 or unknown_checks:
        # An unevaluated check is not evidence of a pass (unknown != PASS).
        board = "BOARD: NEEDS_WORK"
    else:
        board = "BOARD: PASS"
    if unknown_checks:
        lines.append("### Unknown checks (cannot PASS until evaluated)")
        for cid in unknown_checks:
            lines.append(f"- {cid}")
        lines.append("")
    lines.insert(3, f"**{board}**")
    lines.insert(4, "")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


JUDGE_MODES = ("mechanical", "full")
EXIT_FULL_SKIPPED = 3


def _default_llm_runner(name: str, packet_text: str, schema: dict) -> dict:
    if name == "fable":
        return run_fable(packet_text, schema)
    if name == "codex":
        return run_codex(packet_text, SCHEMA_PATH)
    raise ValueError(f"unknown judge {name}")


def run_grade(
    *,
    ticker: str,
    out_dir: Path,
    judges: list[str],
    judge_mode: str = "mechanical",
    force: bool = False,
    attach_csv: bool = False,
    sheet_url: str | None = None,
    llm_runner=None,
    schema: dict | None = None,
) -> tuple[int, dict]:
    """Mechanical precheck + digest always; LLM judges only in ``full`` mode,
    and only once per export content hash unless ``force``.

    Returns (exit_code, info). ``info['skip_reason']`` is the printed reason
    when the LLM judges did not run; exit code ``EXIT_FULL_SKIPPED`` means
    ``full`` was asked for but refused because nothing changed.
    """
    from hidden_stock.quirks.holdings.judge_digest import (
        export_content_hash,
        full_judge_skip_reason,
        record_last_judged,
        write_digest,
    )

    if judge_mode not in JUDGE_MODES:
        raise ValueError(f"judge_mode must be one of {JUDGE_MODES}, got {judge_mode!r}")
    tslug = ticker.lower()
    out_dir.mkdir(parents=True, exist_ok=True)
    mech = mechanical_precheck(
        out_dir / f"{tslug}_equity_holdings_history.csv",
        out_dir / f"{tslug}_portfolio_by_period.csv",
        parent=ticker,
    )
    (out_dir / f"{tslug}_grade_mechanical.json").write_text(
        json.dumps(mech, indent=2) + "\n", encoding="utf-8"
    )
    digest_text, digest_data, digest_json = write_digest(out_dir, tslug, mechanical=mech)
    content_hash = export_content_hash(out_dir, tslug)
    packet_path = build_packet(
        ticker=ticker,
        sheet_url=sheet_url,
        out_dir=out_dir,
        digest_text=digest_text,
        attach_csv=attach_csv,
    )
    results: list[dict] = [mech]
    llm_judges = [j for j in judges if j != "mechanical"]
    info: dict = {
        "content_hash": content_hash,
        "digest_chars": len(digest_text),
        "digest_json": str(digest_json),
        "packet": str(packet_path),
        "llm_judges_run": [],
        "skip_reason": None,
    }

    skip_reason = full_judge_skip_reason(
        out_dir, tslug, judge_mode=judge_mode, force=force, content_hash=content_hash
    )
    if not llm_judges and skip_reason is None:
        skip_reason = "no LLM judges requested (--judges)"
    exit_code = 0
    if skip_reason:
        info["skip_reason"] = skip_reason
        print(f"LLM judges skipped: {skip_reason}", file=sys.stderr)
        if judge_mode == "full" and llm_judges:
            exit_code = EXIT_FULL_SKIPPED
    else:
        packet_text = packet_path.read_text(encoding="utf-8")
        schema = schema or json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        runner = llm_runner or _default_llm_runner
        with ThreadPoolExecutor(max_workers=max(1, len(llm_judges))) as pool:
            futs = {pool.submit(runner, j, packet_text, schema): j for j in llm_judges}
            for fut in as_completed(futs):
                name = futs[fut]
                try:
                    result = fut.result()
                except Exception as e:
                    result = {
                        "judge": name,
                        "verdict": "needs_work",
                        "score": 0,
                        "blocking_issues": [
                            {"id": "exception", "severity": str(e), "evidence": repr(e)}
                        ],
                        "minor_issues": [],
                        "what_looks_good": [],
                        "checks": {
                            "didi_2026_06_30_fv": "unknown",
                            "grab_aurora_vs_10q": "unknown",
                            "aur_one_per_period": "unknown",
                            "no_otc_invent_marks": "unknown",
                            "chart_ranking_sane": "unknown",
                        },
                        "summary": f"{name} raised",
                    }
                out_json = out_dir / f"{tslug}_grade_{name}.json"
                out_json.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
                results.append(result)
                info["llm_judges_run"].append(name)
        info["last_judged"] = str(
            record_last_judged(out_dir, tslug, content_hash=content_hash, judges=llm_judges)
        )

    order = {n: i for i, n in enumerate(judges)}
    results.sort(key=lambda r: order.get(str(r.get("judge")), 99))
    board = write_board(ticker, out_dir, results, sheet_url)
    info["board"] = str(board)
    return exit_code, info


def main() -> int:
    _load_dotenv()
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--ticker", required=True)
    p.add_argument("--sheet-url", default=None)
    p.add_argument("--out-dir", default=str(_ROOT / "exports"))
    p.add_argument(
        "--judges",
        default="fable,codex",
        help="Comma list: fable,codex (only run with --judge-mode full)",
    )
    p.add_argument(
        "--judge-mode",
        choices=JUDGE_MODES,
        default="mechanical",
        help=(
            "mechanical (default): precheck + digest only, no LLM. "
            "full: also run LLM judges — once per export content hash."
        ),
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Re-run LLM judges in full mode even if this export hash was judged before",
    )
    p.add_argument(
        "--attach-csv",
        action="store_true",
        help="Also attach raw CSV head-slices to the packet (digest is always included)",
    )
    args = p.parse_args()

    from hidden_stock.quirks.holdings.parents import normalize_parent

    ticker = normalize_parent(args.ticker)
    judges = [j.strip().lower() for j in args.judges.split(",") if j.strip()]
    code, info = run_grade(
        ticker=ticker,
        out_dir=Path(args.out_dir),
        judges=judges,
        judge_mode=args.judge_mode,
        force=args.force,
        attach_csv=args.attach_csv,
        sheet_url=args.sheet_url,
    )
    board = Path(info["board"])
    print(board.read_text(encoding="utf-8"))
    print(f"\nWrote {board} (digest {info['digest_chars']} chars)", file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
