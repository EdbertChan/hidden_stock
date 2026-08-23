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

    good = []
    if not issues:
        good.append(f"Mechanical uniqueness + invent checks passed for parent={parent_u}")
        if parent_u == "UBER":
            good.append("UBER June-2026 FV anchors checked")
        else:
            good.append("Uber DIDIY/GRAB/AUR anchors skipped (wrong parent)")

    return {
        "judge": "mechanical",
        "verdict": "fail" if issues else "pass",
        "score": 0 if issues else 100,
        "blocking_issues": issues,
        "minor_issues": [],
        "what_looks_good": good,
        "checks": checks,
        "parent": parent_u,
        "summary": (
            f"Mechanical precheck failed for {parent_u}"
            if issues
            else f"Mechanical precheck passed for {parent_u}"
        ),
    }


def build_packet(*, ticker: str, sheet_url: str | None, out_dir: Path) -> Path:
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
                "from shares×EOD is FAIL. `PRIV_HK_*_RESIDUAL` is excluded from portfolio MV.",
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

    lines.extend(
        [
            "## exit / 13g_exit rows (sample — check before stale-M&A FAIL)",
            "",
            "```csv",
            _exit_rows_snippet(history),
            "```",
            "",
            "## portfolio_by_period.csv",
            "",
            "```csv",
            _read_csv_snippet(portfolio),
            "```",
            "",
            "## equity_holdings_history.csv (excerpt)",
            "",
            "```csv",
            _read_csv_snippet(history, max_chars=24000),
            "```",
            "",
        ]
    )
    # Attach parent-scoped performance CSVs when present
    for label, name in (
        ("returns_by_period", f"{t}_returns_by_period.csv"),
        ("realized_pnl_qoq", f"{t}_realized_pnl_qoq.csv"),
        ("reported_vs_est", f"{t}_reported_vs_est.csv"),
    ):
        path = out_dir / name
        if path.is_file():
            lines.extend(
                [
                    f"## {label}.csv (excerpt)",
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
    if "fail" in verdicts:
        board = "BOARD: FAIL"
    elif "needs_work" in verdicts or len(verdicts) > 1:
        board = "BOARD: NEEDS_WORK"
    else:
        board = "BOARD: PASS"
    lines.insert(3, f"**{board}**")
    lines.insert(4, "")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main() -> int:
    _load_dotenv()
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--ticker", required=True)
    p.add_argument("--sheet-url", default=None)
    p.add_argument("--out-dir", default=str(_ROOT / "exports"))
    p.add_argument(
        "--judges",
        default="fable,codex",
        help="Comma list: fable,codex",
    )
    args = p.parse_args()

    from hidden_stock.quirks.holdings.parents import normalize_parent

    ticker = normalize_parent(args.ticker)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    packet_path = build_packet(ticker=ticker, sheet_url=args.sheet_url, out_dir=out_dir)
    packet_text = packet_path.read_text(encoding="utf-8")
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    tslug = ticker.lower()
    mech = mechanical_precheck(
        out_dir / f"{tslug}_equity_holdings_history.csv",
        out_dir / f"{tslug}_portfolio_by_period.csv",
        parent=ticker,
    )
    (out_dir / f"{tslug}_grade_mechanical.json").write_text(
        json.dumps(mech, indent=2) + "\n", encoding="utf-8"
    )
    results: list[dict] = [mech]

    judges = [j.strip().lower() for j in args.judges.split(",") if j.strip()]
    # mechanical always runs above; strip it from LLM pool
    llm_judges = [j for j in judges if j != "mechanical"]

    def _run(name: str) -> dict:
        if name == "fable":
            return run_fable(packet_text, schema)
        if name == "codex":
            return run_codex(packet_text, SCHEMA_PATH)
        raise ValueError(f"unknown judge {name}")

    with ThreadPoolExecutor(max_workers=max(1, len(llm_judges))) as pool:
        futs = {pool.submit(_run, j): j for j in llm_judges}
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
                        {
                            "id": "exception",
                            "severity": str(e),
                            "evidence": repr(e),
                        }
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
            out_json = out_dir / f"{ticker.lower()}_grade_{name}.json"
            out_json.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
            results.append(result)

    # Stable order
    order = {n: i for i, n in enumerate(judges)}
    results.sort(key=lambda r: order.get(str(r.get("judge")), 99))
    board = write_board(ticker, out_dir, results, args.sheet_url)
    print(board.read_text(encoding="utf-8"))
    print(f"\nWrote {board}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
