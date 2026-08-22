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
import subprocess
import sys
import tempfile
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


def mechanical_precheck(history_csv: Path, portfolio_csv: Path) -> dict:
    """Pandas checks that must not wait for an LLM judge (SERV-class bugs)."""
    issues: list[dict] = []
    checks = {
        "didi_2026_06_30_fv": "unknown",
        "grab_aurora_vs_10q": "unknown",
        "aur_one_per_period": "unknown",
        "no_otc_invent_marks": "unknown",
        "chart_ranking_sane": "unknown",
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
        }

    import pandas as pd

    hist = pd.read_csv(history_csv)
    if "investee_ticker" in hist.columns and "period_end" in hist.columns:
        g = (
            hist.assign(
                investee_ticker=hist["investee_ticker"].astype(str).str.upper(),
                period_end=hist["period_end"].astype(str),
            )
            .groupby(["period_end", "investee_ticker"], dropna=False)
            .size()
        )
        dups = g[g > 1]
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

    if portfolio_csv.is_file() and "period_end" in pd.read_csv(portfolio_csv, nrows=0).columns:
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
            checks["grab_aurora_vs_10q"] = "pass" if (_ok("GRAB") and _ok("AUR")) else "fail"
            if checks["didi_2026_06_30_fv"] == "fail":
                issues.append(
                    {
                        "id": "didi_fv_mismatch",
                        "severity": "DIDIY @ 2026-06-30 not ≈ $1.9B Investments FV",
                        "evidence": str(by.get("DIDIY")),
                    }
                )
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

    return {
        "judge": "mechanical",
        "verdict": "fail" if issues else "pass",
        "score": 0 if issues else 100,
        "blocking_issues": issues,
        "minor_issues": [],
        "what_looks_good": []
        if issues
        else ["Mechanical uniqueness + UBER June-2026 FV checks passed"],
        "checks": checks,
        "summary": (
            "Mechanical precheck failed"
            if issues
            else "Mechanical precheck passed (uniqueness + ground-truth anchors)"
        ),
    }


def build_packet(*, ticker: str, sheet_url: str | None, out_dir: Path) -> Path:
    t = ticker.lower()
    portfolio = out_dir / f"{t}_portfolio_by_period.csv"
    history = out_dir / f"{t}_equity_holdings_history.csv"
    packet = out_dir / f"{t}_grade_packet.md"

    lines = [
        f"# Holdings sheet grade packet — {ticker}",
        "",
        f"- sheet_url: {sheet_url or '(none)'}",
        f"- portfolio_csv: `{portfolio}`",
        f"- history_csv: `{history}`",
        "",
        "## Ground truth (do not invent)",
        "",
        "Valuation order: Form 13F market_value_usd → 10-Q/10-K/20-F Investments "
        "table FV ($M×1e6) → else null. Schedule 13G = identity/%/shares only.",
        "",
        "Uber 10-Q Investments (millions) @ 2026-06-30:",
        f"- Didi/DIDIY: **{UBER_10Q_TRUTH['investments_millions']['DIDIY']}**",
        f"- Grab/GRAB: **{UBER_10Q_TRUTH['investments_millions']['GRAB']}**",
        f"- Aurora/AUR: **{UBER_10Q_TRUTH['investments_millions']['AUR']}**",
        f"- Filing: {UBER_10Q_TRUTH['filing']}",
        f"- Tolerance: {UBER_10Q_TRUTH['tolerance']}",
        "",
        "Also FAIL if **any** ticker appears twice in the same period_end "
        "(13F exit + continuing 13G is still a duplicate — not only AUR overlap).",
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
        _read_csv_snippet(history, max_chars=16000),
        "```",
        "",
        "## Your job",
        "",
        "You are an independent judge. Grade this export against the rubric.",
        "Return ONLY JSON matching the provided schema. Be harsh on invented "
        "OTC marks and double-counts. Cite concrete CSV rows as evidence.",
    ]
    packet.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return packet


def _judge_prompt(packet_text: str, judge_name: str) -> str:
    return (
        f"You are judge={judge_name} grading an equity-holdings spreadsheet export.\n"
        "Apply the rubric in the packet strictly. Output JSON only (no markdown).\n\n"
        f"{packet_text}\n"
    )


def run_fable(packet_text: str, schema: dict) -> dict:
    prompt = _judge_prompt(packet_text, "fable")
    # Prefer Claude Code login; a stale ANTHROPIC_API_KEY causes 401s.
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    cmd = [
        "claude",
        "-p",
        "--model",
        "claude-fable-5",
        "--tools",
        "",
        "--output-format",
        "text",
        prompt
        + "\n\nRespond with a single JSON object matching this schema:\n"
        + json.dumps(schema),
    ]
    proc = subprocess.run(
        cmd,
        cwd=str(_ROOT),
        capture_output=True,
        text=True,
        timeout=600,
        env=env,
    )
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    combined = out or err
    auth_fail = any(
        x in combined.lower()
        for x in ("401", "api key is invalid", "authenticate", "oauth session expired")
    )
    if auth_fail:
        return {
            "judge": "fable",
            "verdict": "needs_work",
            "score": 0,
            "blocking_issues": [
                {
                    "id": "fable_auth",
                    "severity": "Claude Fable auth failed — run `claude login`",
                    "evidence": combined[:2000],
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
            "summary": "Fable judge unavailable (auth)",
        }
    if proc.returncode != 0 and not out:
        return {
            "judge": "fable",
            "verdict": "needs_work",
            "score": 0,
            "blocking_issues": [
                {
                    "id": "fable_exec_failed",
                    "severity": f"claude fable exited {proc.returncode}",
                    "evidence": err[:2000] or out[:2000],
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
            "summary": "Fable judge failed to run",
        }
    return _parse_json_response(out, judge="fable")


def run_codex(packet_text: str, schema_path: Path) -> dict:
    prompt = _judge_prompt(packet_text, "codex")
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as tf:
        tf.write(prompt)
        prompt_path = tf.name
    try:
        cmd = [
            "codex",
            "exec",
            "--skip-git-repo-check",
            "-s",
            "read-only",
            "--output-schema",
            str(schema_path),
            "-o",
            str(_ROOT / "exports" / "_codex_last_message.txt"),
            prompt,
        ]
        proc = subprocess.run(
            cmd,
            cwd=str(_ROOT),
            capture_output=True,
            text=True,
            timeout=600,
        )
        last = _ROOT / "exports" / "_codex_last_message.txt"
        out = last.read_text(encoding="utf-8") if last.is_file() else (proc.stdout or "")
        if proc.returncode != 0 and not out.strip():
            return {
                "judge": "codex",
                "verdict": "needs_work",
                "score": 0,
                "blocking_issues": [
                    {
                        "id": "codex_exec_failed",
                        "severity": f"codex exited {proc.returncode}",
                        "evidence": (proc.stderr or proc.stdout or "")[:2000],
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
                "summary": "Codex judge failed to run",
            }
        return _parse_json_response(out, judge="codex")
    finally:
        try:
            os.unlink(prompt_path)
        except OSError:
            pass


def _parse_json_response(text: str, *, judge: str) -> dict:
    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:].lstrip()
    # Find outermost JSON object
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end < 0:
        return {
            "judge": judge,
            "verdict": "needs_work",
            "score": 0,
            "blocking_issues": [
                {
                    "id": "unparseable",
                    "severity": "Judge did not return JSON",
                    "evidence": text[:2000],
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
            "summary": "Unparseable judge output",
        }
    data = json.loads(raw[start : end + 1])
    data["judge"] = judge
    return data


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
