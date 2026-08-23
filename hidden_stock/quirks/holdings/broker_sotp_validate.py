"""Spike: mechanical (and optional Fable) validation for broker SOTP ingest.

Not production Dagster assets yet — call from
``scripts/spike_broker_sotp_pipeline_validate.py`` to see what each stage catches.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

Stage = Literal[
    "catalog",
    "parse",
    "fx",
    "overlay_live",
    "overlay_history",
    "composition_ux",
    "fable",
]


@dataclass
class Finding:
    stage: Stage
    severity: Literal["fail", "warn", "info"]
    check_id: str
    message: str
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class StageReport:
    stage: Stage
    ok: bool
    findings: list[Finding] = field(default_factory=list)

    def add(self, finding: Finding) -> None:
        self.findings.append(finding)
        if finding.severity == "fail":
            self.ok = False


def _ticker(r: dict) -> str:
    return str(r.get("investee_ticker") or "").strip().upper()


def validate_catalog(entries: list[dict]) -> StageReport:
    rep = StageReport(stage="catalog", ok=True)
    if not entries:
        rep.add(
            Finding("catalog", "fail", "empty_catalog", "No enabled catalog entries")
        )
        return rep
    parents = {str(e.get("parent_ticker") or "").upper() for e in entries}
    rep.add(
        Finding(
            "catalog",
            "info",
            "catalog_size",
            f"{len(entries)} reports across parents={sorted(parents)}",
            {"n": len(entries), "parents": sorted(parents)},
        )
    )
    for e in entries:
        if not e.get("url"):
            rep.add(
                Finding(
                    "catalog",
                    "fail",
                    "missing_url",
                    f"entry {e.get('id')} missing url",
                    {"id": e.get("id")},
                )
            )
        if not e.get("parser"):
            rep.add(
                Finding(
                    "catalog",
                    "fail",
                    "missing_parser",
                    f"entry {e.get('id')} missing parser",
                    {"id": e.get("id")},
                )
            )
        if not e.get("as_of"):
            rep.add(
                Finding(
                    "catalog",
                    "warn",
                    "missing_as_of",
                    f"entry {e.get('id')} missing as_of",
                    {"id": e.get("id")},
                )
            )
    return rep


def validate_parse_rows(
    rows: list[dict],
    *,
    report_id: str | None = None,
    min_rows: int = 10,
) -> StageReport:
    """Per-report / full-series parse sanity (units, litmus stakes)."""
    rep = StageReport(stage="parse", ok=True)
    scope = report_id or "series"
    if len(rows) < min_rows:
        rep.add(
            Finding(
                "parse",
                "fail",
                "too_few_rows",
                f"{scope}: only {len(rows)} named rows (want ≥{min_rows})",
                {"n": len(rows), "report_id": report_id},
            )
        )
    # Unit heuristic: HK$mn value-to-parent for a mega-cap stake is usually 1e3–1e6.
    for r in rows:
        hkd = r.get("value_to_parent_hkd_mn")
        try:
            hkd_f = float(hkd) if hkd is not None else None
        except (TypeError, ValueError):
            hkd_f = None
        if hkd_f is None:
            continue
        if hkd_f > 5_000_000:
            rep.add(
                Finding(
                    "parse",
                    "fail",
                    "value_unit_likely_wrong",
                    f"{scope}/{_ticker(r)}: value_to_parent_hkd_mn={hkd_f} looks like "
                    "not millions (possible bn or raw HKD)",
                    {"ticker": _ticker(r), "value": hkd_f, "report_id": report_id},
                )
            )
        if 0 < hkd_f < 10:
            rep.add(
                Finding(
                    "parse",
                    "warn",
                    "value_suspiciously_small",
                    f"{scope}/{_ticker(r)}: value_to_parent_hkd_mn={hkd_f} tiny for mn",
                    {"ticker": _ticker(r), "value": hkd_f},
                )
            )
        stake = r.get("ownership_pct")
        try:
            stake_f = float(stake) if stake is not None else None
        except (TypeError, ValueError):
            stake_f = None
        if stake_f is not None and not (0 < stake_f <= 100):
            rep.add(
                Finding(
                    "parse",
                    "fail",
                    "stake_out_of_range",
                    f"{scope}/{_ticker(r)}: ownership_pct={stake_f}",
                    {"ticker": _ticker(r), "ownership_pct": stake_f},
                )
            )
        t = _ticker(r)
        if t in {"US", "HK", "KS", "JP", "CH", "LN", "SS", "SZ"}:
            rep.add(
                Finding(
                    "parse",
                    "fail",
                    "ticker_is_exchange_suffix",
                    f"{scope}: investee_ticker={t!r} is an exchange code — "
                    "numeric code was dropped (002602 CH / 259960 KS class)",
                    {
                        "ticker": t,
                        "name": r.get("investee_name"),
                        "raw": r.get("investee_ticker_raw"),
                        "report_id": report_id,
                    },
                )
            )

    by_t = {_ticker(r): r for r in rows if _ticker(r)}
    # Litmus: Meituan post-distribution ~1.7 (allow 1.5–4.0 for older vintages).
    mei = by_t.get("3690.HK")
    if mei is not None:
        try:
            s = float(mei.get("ownership_pct"))
        except (TypeError, ValueError):
            s = None
        if s is not None and not (1.0 <= s <= 20.0):
            rep.add(
                Finding(
                    "parse",
                    "fail",
                    "meituan_litmus",
                    f"{scope}: Meituan stake {s}% outside 1–20",
                    {"ownership_pct": s, "report_id": report_id},
                )
            )
        elif s is not None and s > 5.0:
            rep.add(
                Finding(
                    "parse",
                    "warn",
                    "meituan_elevated",
                    f"{scope}: Meituan stake {s}% (pre- or mid-distribution vintage?)",
                    {"ownership_pct": s, "as_of": mei.get("as_of")},
                )
            )
    pdd = by_t.get("PDD")
    if pdd is not None:
        try:
            s = float(pdd.get("ownership_pct"))
        except (TypeError, ValueError):
            s = None
        if s is not None and not (8.0 <= s <= 20.0):
            rep.add(
                Finding(
                    "parse",
                    "warn",
                    "pdd_stake_unexpected",
                    f"{scope}: PDD stake {s}% outside 8–20 band",
                    {"ownership_pct": s},
                )
            )
    if "PDD" not in by_t and len(rows) >= min_rows:
        rep.add(
            Finding(
                "parse",
                "warn",
                "pdd_missing",
                f"{scope}: PDD not in parsed table",
                {"report_id": report_id},
            )
        )
    rep.add(
        Finding(
            "parse",
            "info",
            "parse_ok_summary",
            f"{scope}: {len(rows)} rows, tickers={len(by_t)}",
            {"n": len(rows), "sample": sorted(by_t)[:12]},
        )
    )
    return rep


def validate_fx_roundtrip(rows: list[dict], *, tol_pct: float = 2.0) -> StageReport:
    """usd * fx ≈ hkd_mn * 1e6."""
    from .broker_sotp import hkd_mn_to_usd

    rep = StageReport(stage="fx", ok=True)
    checked = 0
    for r in rows[:5]:  # spot first few per call; spike aggregates many
        hkd = r.get("value_to_parent_hkd_mn")
        as_of = r.get("as_of")
        try:
            hkd_f = float(hkd)
        except (TypeError, ValueError):
            continue
        usd, fx = hkd_mn_to_usd(hkd_f, str(as_of) if as_of else None)
        expected = hkd_f * 1_000_000.0
        got = usd * fx
        if expected <= 0:
            continue
        err = abs(got - expected) / expected * 100.0
        checked += 1
        if err > tol_pct:
            rep.add(
                Finding(
                    "fx",
                    "fail",
                    "fx_roundtrip",
                    f"{_ticker(r)} as_of={as_of}: round-trip err {err:.2f}%",
                    {"usd": usd, "fx": fx, "hkd_mn": hkd_f, "err_pct": err},
                )
            )
    if checked == 0:
        rep.add(Finding("fx", "warn", "fx_no_rows", "No rows to FX-check"))
    else:
        rep.add(
            Finding(
                "fx",
                "info",
                "fx_checked",
                f"Checked {checked} FX round-trips (tol {tol_pct}%)",
                {"checked": checked},
            )
        )
    return rep


def validate_overlay_live(hold_rows: list[dict]) -> StageReport:
    """After live overlay: broker-stamped $ present; parent agg not confused with child."""
    rep = StageReport(stage="overlay_live", ok=True)
    brokerish = [
        r
        for r in hold_rows
        if "value_source=broker_sotp" in str(r.get("note") or "")
    ]
    if not brokerish:
        rep.add(
            Finding(
                "overlay_live",
                "fail",
                "no_broker_stamps",
                "No live rows with value_source=broker_sotp",
            )
        )
        return rep
    pdd = [r for r in brokerish if _ticker(r) == "PDD"]
    if pdd:
        mv = pdd[0].get("market_value_usd")
        try:
            mv_f = float(mv) if mv is not None else None
        except (TypeError, ValueError):
            mv_f = None
        # PDD broker mark should be ~$10–40B, not ~$80–100B parent aggregate.
        if mv_f is not None and mv_f > 60e9:
            rep.add(
                Finding(
                    "overlay_live",
                    "fail",
                    "pdd_looks_like_parent_agg",
                    f"PDD market_value_usd={mv_f/1e9:.2f}B looks like Note 22 parent "
                    "aggregate, not broker value-to-Tencent",
                    {"market_value_usd": mv_f},
                )
            )
        elif mv_f is not None and mv_f < 1e9:
            rep.add(
                Finding(
                    "overlay_live",
                    "warn",
                    "pdd_value_small",
                    f"PDD market_value_usd={mv_f/1e9:.2f}B smaller than expected",
                    {"market_value_usd": mv_f},
                )
            )
        else:
            rep.add(
                Finding(
                    "overlay_live",
                    "info",
                    "pdd_ok",
                    f"PDD broker $≈{(mv_f or 0)/1e9:.2f}B with stamp",
                    {
                        "market_value_usd": mv_f,
                        "ownership_pct": pdd[0].get("ownership_pct"),
                    },
                )
            )
        if "excluded_from_portfolio_mv" not in str(pdd[0].get("note") or ""):
            rep.add(
                Finding(
                    "overlay_live",
                    "warn",
                    "pdd_missing_exclude",
                    "PDD broker row missing excluded_from_portfolio_mv "
                    "(risk of double-count with Note 22)",
                )
            )
    else:
        rep.add(
            Finding(
                "overlay_live",
                "warn",
                "pdd_not_on_live",
                "PDD not among broker-stamped live rows",
            )
        )
    rep.add(
        Finding(
            "overlay_live",
            "info",
            "broker_stamp_count",
            f"{len(brokerish)} live rows stamped broker_sotp",
            {"n": len(brokerish)},
        )
    )
    return rep


def validate_overlay_history(hist_rows: list[dict]) -> StageReport:
    rep = StageReport(stage="overlay_history", ok=True)
    brokerish = [
        r
        for r in hist_rows
        if "value_source=broker_sotp" in str(r.get("note") or "")
    ]
    if not brokerish:
        rep.add(
            Finding(
                "overlay_history",
                "fail",
                "no_broker_history",
                "No history rows with value_source=broker_sotp",
            )
        )
        return rep
    as_ofs = sorted({str(r.get("period_end") or "")[:10] for r in brokerish if r.get("period_end")})
    rep.add(
        Finding(
            "overlay_history",
            "info",
            "broker_history_vintages",
            f"{len(brokerish)} broker-stamped history rows; period_ends={as_ofs[:8]}…",
            {"n": len(brokerish), "period_ends": as_ofs},
        )
    )
    # Same ticker should not carry parent aggregate $ on broker-stamped rows.
    for r in brokerish:
        try:
            mv = float(r["market_value_usd"]) if r.get("market_value_usd") is not None else None
        except (TypeError, ValueError):
            mv = None
        if mv is not None and mv > 60e9 and _ticker(r) not in {
            "PRIV_HK_LISTED_INVESTEES_FV",
            "PRIV_HK_LISTED_ASSOCIATES",
        }:
            rep.add(
                Finding(
                    "overlay_history",
                    "fail",
                    "child_mv_looks_like_parent",
                    f"{_ticker(r)} @{r.get('period_end')}: mv={mv/1e9:.1f}B "
                    "looks like parent aggregate",
                    {"ticker": _ticker(r), "mv": mv, "period_end": r.get("period_end")},
                )
            )
            break
    return rep


def validate_composition_ux(comp_rows: list[dict]) -> StageReport:
    """Catch confusion: parent_aggregate ≠ broker value-to-parent; child $ required.

    Accepts either ``hk_composition_frame`` rows (child_*) or QoQ history rows
    with first-class ``composition_parent`` / ``parent_aggregate_market_value_usd``.
    """
    rep = StageReport(stage="composition_ux", ok=True)
    if not comp_rows:
        rep.add(
            Finding(
                "composition_ux",
                "warn",
                "no_composition",
                "No composition/QoQ rows to check",
            )
        )
        return rep

    # Normalize QoQ stamped rows → composition-shaped view when needed.
    sample_keys = set(comp_rows[0].keys()) if comp_rows else set()
    if "child_ticker" not in sample_keys and (
        "composition_parent" in sample_keys
        or any("composition_parent=" in str(r.get("note") or "") for r in comp_rows)
    ):
        from hidden_stock.quirks.holdings.composition import hk_composition_frame

        comp_rows = hk_composition_frame(comp_rows)

    pdd = [
        r
        for r in comp_rows
        if str(r.get("child_ticker") or r.get("investee_ticker") or "").upper()
        == "PDD"
    ]
    if not pdd:
        rep.add(
            Finding(
                "composition_ux",
                "info",
                "no_pdd_comp",
                "No PDD in composition/QoQ overlay",
            )
        )
        return rep
    sample = pdd[-1]
    parent_mv = sample.get("parent_aggregate_market_value_usd")
    if parent_mv is None:
        parent_mv = sample.get("parent_market_value_usd")  # legacy column
    child_mv = sample.get("child_market_value_usd")
    if child_mv is None:
        child_mv = sample.get("market_value_usd")
    try:
        c_f = float(child_mv) if child_mv not in (None, "") else None
    except (TypeError, ValueError):
        c_f = None
    if c_f is not None and c_f != c_f:  # NaN
        c_f = None
    try:
        p_f = float(parent_mv) if parent_mv not in (None, "") else None
    except (TypeError, ValueError):
        p_f = None
    if p_f is not None and p_f != p_f:
        p_f = None

    rep.add(
        Finding(
            "composition_ux",
            "info",
            "pdd_composition_fields",
            "PDD composition: parent_aggregate_market_value_usd is Note 22 bucket; "
            "child_market_value_usd is name-level (broker/filing)",
            {
                "period_end": sample.get("period_end")
                or sample.get("composition_as_of"),
                "parent_aggregate_market_value_usd": p_f,
                "child_market_value_usd": c_f,
                "child_value_source": sample.get("child_value_source"),
                "parent_bn": (p_f / 1e9) if p_f else None,
                "child_bn": (c_f / 1e9) if c_f else None,
            },
        )
    )
    if p_f is not None and p_f > 50e9 and c_f is None:
        rep.add(
            Finding(
                "composition_ux",
                "fail",
                "parent_vs_child_confusion_risk",
                f"PDD shows parent_aggregate≈${p_f/1e9:.1f}B with null "
                "child_market_value_usd — easy to misread as PDD's value",
                {"parent_bn": p_f / 1e9},
            )
        )
    if c_f is not None and p_f is not None and abs(c_f - p_f) / p_f < 0.05:
        rep.add(
            Finding(
                "composition_ux",
                "fail",
                "child_equals_parent",
                "child_market_value_usd ≈ parent_aggregate — child wrongly got aggregate $",
                {"parent": p_f, "child": c_f},
            )
        )
    if c_f is not None and c_f > 60e9:
        rep.add(
            Finding(
                "composition_ux",
                "fail",
                "child_looks_like_parent_agg",
                f"PDD child_market_value_usd={c_f/1e9:.1f}B looks like aggregate",
                {"child": c_f},
            )
        )
    if c_f is not None and 5e9 <= c_f <= 50e9:
        rep.add(
            Finding(
                "composition_ux",
                "info",
                "pdd_child_ok",
                f"PDD child_market_value_usd≈${c_f/1e9:.2f}B "
                f"(source={sample.get('child_value_source')})",
                {"child_bn": c_f / 1e9},
            )
        )
    # Class assert: any named child with $ needs join keys + parent aggregate.
    for r in comp_rows:
        child = str(r.get("child_ticker") or "").upper()
        if not child or child.endswith("_RESIDUAL"):
            continue
        cmv = r.get("child_market_value_usd")
        try:
            cmv_f = float(cmv) if cmv not in (None, "") else None
        except (TypeError, ValueError):
            cmv_f = None
        if cmv_f is None or cmv_f != cmv_f:
            continue
        pe = r.get("period_end")
        parent = r.get("composition_parent")
        pagg = r.get("parent_aggregate_market_value_usd")
        pe_s = str(pe or "").strip()
        parent_s = str(parent or "").strip()
        if (
            not pe_s
            or pe_s.lower() == "nan"
            or not parent_s
            or parent_s.lower() == "nan"
            or pagg in (None, "")
            or (isinstance(pagg, float) and pagg != pagg)
        ):
            rep.add(
                Finding(
                    "composition_ux",
                    "fail",
                    "child_mv_missing_join_keys",
                    f"{child}: child $ set but period_end/composition_parent/"
                    "parent_aggregate missing (NaN composition_parent class)",
                    {
                        "child": child,
                        "period_end": pe,
                        "composition_parent": parent,
                        "parent_aggregate": pagg,
                    },
                )
            )
            break
    return rep


def build_fable_packet(
    *,
    report_id: str,
    citation: str,
    raw_table_snippet: str,
    parsed_rows: list[dict],
) -> str:
    sample = [
        {
            "investee_ticker": r.get("investee_ticker"),
            "investee_name": r.get("investee_name"),
            "ownership_pct": r.get("ownership_pct"),
            "value_to_parent_hkd_mn": r.get("value_to_parent_hkd_mn"),
            "mkt_cap_usd_mn": r.get("mkt_cap_usd_mn"),
        }
        for r in parsed_rows[:8]
    ]
    return (
        "You verify a broker strategic-investments PDF parse (spike, not sheet grade).\n"
        f"report_id={report_id}\ncitation={citation}\n\n"
        "PDF/table text snippet:\n```\n"
        + raw_table_snippet[:3500]
        + "\n```\n\nParsed rows (sample):\n"
        + json.dumps(sample, indent=2)
        + "\n\nChecks:\n"
        "1) Is this the named stake×mcap / value-to-parent table?\n"
        "2) Is value_to_parent in HK$ millions (not billions, not USD)?\n"
        "3) Spot-check PDD and Meituan stake % vs the snippet.\n"
        "4) Would treating value_to_parent as USD or as parent aggregate be wrong?\n"
        "Respond JSON: {verdict: pass|fail|needs_work, score: 0-100, "
        "blocking_issues: [{id, severity, evidence}], summary: str}"
    )


def run_fable_spike(packet: str) -> StageReport:
    """Best-effort Fable call; auth failures are warnings for the spike."""
    import os
    import subprocess
    from pathlib import Path

    rep = StageReport(stage="fable", ok=True)
    root = Path(__file__).resolve().parents[3]
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
        packet,
    ]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=180,
            env=env,
        )
    except FileNotFoundError:
        rep.add(
            Finding(
                "fable",
                "warn",
                "fable_cli_missing",
                "claude CLI not found — skip Fable spike",
            )
        )
        return rep
    except subprocess.TimeoutExpired:
        rep.add(Finding("fable", "warn", "fable_timeout", "Fable spike timed out"))
        return rep

    out = (proc.stdout or "").strip() or (proc.stderr or "").strip()
    if any(x in out.lower() for x in ("401", "authenticate", "api key is invalid")):
        rep.add(
            Finding(
                "fable",
                "warn",
                "fable_auth",
                "Fable auth failed — run claude login; mechanical stages still ran",
                {"snippet": out[:500]},
            )
        )
        return rep
    # Try extract JSON
    data = None
    m = re.search(r"\{[\s\S]*\}", out)
    if m:
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            data = None
    if not data:
        rep.add(
            Finding(
                "fable",
                "warn",
                "fable_unparseable",
                "Fable returned non-JSON",
                {"out": out[:1500]},
            )
        )
        return rep
    verdict = str(data.get("verdict") or "").lower()
    sev: Literal["fail", "warn", "info"] = (
        "fail" if verdict == "fail" else "warn" if verdict == "needs_work" else "info"
    )
    if verdict == "fail":
        rep.ok = False
    rep.add(
        Finding(
            "fable",
            sev,
            "fable_verdict",
            f"Fable verdict={verdict} score={data.get('score')} — {data.get('summary')}",
            {"raw": data},
        )
    )
    for issue in data.get("blocking_issues") or []:
        rep.add(
            Finding(
                "fable",
                "fail" if verdict == "fail" else "warn",
                str(issue.get("id") or "fable_issue"),
                str(issue.get("severity") or issue),
                {"evidence": issue.get("evidence")},
            )
        )
    return rep


def merge_reports(reports: list[StageReport]) -> dict[str, Any]:
    fails = [f for r in reports for f in r.findings if f.severity == "fail"]
    warns = [f for r in reports for f in r.findings if f.severity == "warn"]
    return {
        "ok": all(r.ok for r in reports),
        "n_fail": len(fails),
        "n_warn": len(warns),
        "stages": [
            {
                "stage": r.stage,
                "ok": r.ok,
                "findings": [asdict(f) for f in r.findings],
            }
            for r in reports
        ],
    }
