"""Estimated QoQ MTM and disposal P&L from holdings history.

Form 13F / Investments FV give period-end shares + market value, not tax lot cost.

Products (do not conflate):
- **MTM** (primary): end_mv − start_mv − net_external_flow — closest proxy to revaluation.
- **Avg-cost disposal** (primary disposal method; ASC 321-style average cost wording).
- **FIFO disposal** (sensitivity only).

Neither disposal method is company GAAP / tax realized P&L.
"""

from __future__ import annotations

from collections import defaultdict, deque
from pathlib import Path
from typing import Any

import pandas as pd

from .lookback import normalize_ticker

NOTE_EST = (
    "estimated; period-end price proxy (not tax/GAAP); "
    "mtm primary; avg-cost disposal primary; fifo=sensitivity"
)
NOTE_RECON = (
    "reconciliation only — residual expected (Ant, private marks, equity-method, FX, "
    "interest income); fiscal Mar-31 vs calendar 13F; not an equality claim"
)

_DATA_DIR = Path(__file__).resolve().parent / "data"


def _holding_periods(open_pe: str, close_pe: str) -> int | None:
    """Approximate number of calendar quarters between lot open and exit period."""
    try:
        o = pd.Timestamp(str(open_pe)[:10])
        c = pd.Timestamp(str(close_pe)[:10])
    except (TypeError, ValueError):
        return None
    if pd.isna(o) or pd.isna(c):
        return None
    months = (c.year - o.year) * 12 + (c.month - o.month)
    return max(0, int(round(months / 3.0)))


def _f(v) -> float | None:
    if v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if x != x:  # NaN
        return None
    return x


def period_price(row: dict) -> float | None:
    """$/share from period-end MV / shares when both present and shares > 0."""
    mv = _f(row.get("market_value_usd"))
    sh = _f(row.get("shares_held"))
    if mv is None or sh is None or sh <= 0:
        if str(row.get("action") or "") == "exit":
            vp = _f(row.get("value_prev"))
            sp = _f(row.get("shares_prev"))
            if vp is not None and sp is not None and sp > 0:
                return vp / sp
        return None
    return mv / sh


def _ticker_key(row: dict) -> str | None:
    t = normalize_ticker(row.get("investee_ticker"))
    if t:
        return t
    name = str(row.get("investee_name") or "").strip()
    return name.upper() if name else None


def _share_delta(row: dict) -> float:
    delta = _f(row.get("shares_delta"))
    if delta is not None:
        return float(delta)
    sh = _f(row.get("shares_held")) or 0.0
    sp = _f(row.get("shares_prev")) or 0.0
    if str(row.get("action") or "") == "exit":
        return -sp
    return sh - sp


def _avg_cost_px(lots: deque) -> float | None:
    tot_sh = sum(float(lot["shares"]) for lot in lots)
    if tot_sh <= 1e-12:
        return None
    tot_cost = sum(float(lot["shares"]) * float(lot["cost_px"]) for lot in lots)
    return tot_cost / tot_sh


def _oldest_open(lots: deque) -> str | None:
    if not lots:
        return None
    return str(lots[0].get("open_period") or "") or None


def build_lots_and_realized(
    history_rows: list[dict],
) -> tuple[list[dict], list[dict]]:
    """Open lots on buys; on sells emit avg-cost (primary) + FIFO (sensitivity) events.

    Returns (unused_open_lots, realized_events) where each event has cost_method=avg|fifo.
    """
    rows = sorted(
        history_rows,
        key=lambda r: (
            str(r.get("period_end") or ""),
            0 if str(r.get("action")) in {"new", "buy"} else 1,
            _ticker_key(r) or "",
        ),
    )
    lots: dict[str, deque] = defaultdict(deque)
    realized: list[dict] = []

    for row in rows:
        t = _ticker_key(row)
        if not t:
            continue
        delta = _share_delta(row)
        px = period_price(row)
        pe = str(row.get("period_end") or "")
        action = str(row.get("action") or "")

        # ADS ratio / reverse-split: rescale open lots; never book a disposal.
        if action == "ratio_adj":
            prev_sh = _f(row.get("shares_prev"))
            cur_sh = _f(row.get("shares_held"))
            if (
                prev_sh is not None
                and cur_sh is not None
                and cur_sh > 0
                and prev_sh > cur_sh
                and lots[t]
            ):
                factor = float(prev_sh) / float(cur_sh)
                if factor >= 2.0 - 1e-9:
                    for lot in lots[t]:
                        lot["shares"] = float(lot["shares"]) / factor
                        lot["cost_px"] = float(lot["cost_px"]) * factor
            continue

        if delta > 0 and px is not None:
            note = str(row.get("note") or "")
            # Lookback wall is not inception — do not invent a priced buy lot.
            if "lookback_truncated=1" in note or "cost_basis=unknown_truncated" in note:
                continue
            lots[t].append(
                {
                    "shares": float(delta),
                    "cost_px": float(px),
                    "open_period": pe,
                }
            )
            continue

        if delta >= 0 or px is None:
            continue

        need = -float(delta)
        exit_px = float(px)
        if need <= 1e-12:
            continue
        if not lots[t]:
            # Exit with no known lots (truncated open) — do not invent cost.
            if "lookback_truncated=1" in str(row.get("note") or ""):
                realized.append(
                    {
                        "period_end": pe,
                        "investee_ticker": t,
                        "investee_name": row.get("investee_name"),
                        "shares_sold": need,
                        "cost_px": None,
                        "exit_px": exit_px,
                        "realized_pnl_est": None,
                        "cost_method": "avg",
                        "lot_opened_period": None,
                        "holding_periods": None,
                        "filing_url": row.get("filing_url"),
                        "accession_no": row.get("accession_no"),
                        "note": f"{NOTE_EST}; cost_basis=unknown_truncated",
                    }
                )
            continue

        avg_px = _avg_cost_px(lots[t])
        oldest = _oldest_open(lots[t])
        if avg_px is not None:
            realized.append(
                {
                    "period_end": pe,
                    "investee_ticker": t,
                    "investee_name": row.get("investee_name"),
                    "shares_sold": need,
                    "cost_px": avg_px,
                    "exit_px": exit_px,
                    "realized_pnl_est": need * (exit_px - avg_px),
                    "cost_method": "avg",
                    "lot_opened_period": oldest,
                    "holding_periods": _holding_periods(oldest or pe, pe),
                    "filing_url": row.get("filing_url"),
                    "accession_no": row.get("accession_no"),
                    "note": NOTE_EST,
                }
            )

        # FIFO sensitivity: consume oldest lots (mutates ledger)
        remaining = need
        while remaining > 1e-12 and lots[t]:
            lot = lots[t][0]
            take = min(lot["shares"], remaining)
            realized.append(
                {
                    "period_end": pe,
                    "investee_ticker": t,
                    "investee_name": row.get("investee_name"),
                    "shares_sold": take,
                    "cost_px": lot["cost_px"],
                    "exit_px": exit_px,
                    "realized_pnl_est": take * (exit_px - lot["cost_px"]),
                    "cost_method": "fifo",
                    "lot_opened_period": lot["open_period"],
                    "holding_periods": _holding_periods(lot["open_period"], pe),
                    "filing_url": row.get("filing_url"),
                    "accession_no": row.get("accession_no"),
                    "note": NOTE_EST,
                }
            )
            lot["shares"] -= take
            remaining -= take
            if lot["shares"] <= 1e-12:
                lots[t].popleft()

    return [], realized


def _dietz_return(start_mv: float, end_mv: float, net_flow: float) -> float | None:
    """Modified Dietz: (end - start - flow) / (start + flow/2)."""
    denom = start_mv + net_flow / 2.0
    if abs(denom) < 1e-6:
        return None
    return (end_mv - start_mv - net_flow) / denom


def _portfolio_mv_by_period(history_rows: list[dict]) -> dict[str, float]:
    by: dict[str, float] = defaultdict(float)
    for r in history_rows:
        if str(r.get("action") or "") == "exit":
            continue
        mv = _f(r.get("market_value_usd"))
        if mv is None:
            continue
        pe = str(r.get("period_end") or "")
        if pe:
            by[pe] += mv
    return dict(by)


def _flows_by_period_ticker(history_rows: list[dict]) -> dict[tuple[str, str], float]:
    """Net external flow at period prices: +buy_value, −sell_value."""
    flows: dict[tuple[str, str], float] = defaultdict(float)
    for r in history_rows:
        if str(r.get("action") or "") == "ratio_adj":
            continue  # share restatement, not cash/share flow
        t = _ticker_key(r)
        pe = str(r.get("period_end") or "")
        if not t or not pe:
            continue
        delta = _share_delta(r)
        px = period_price(r)
        if px is None or abs(delta) < 1e-12:
            continue
        flows[(pe, t)] += float(delta) * float(px)
    return dict(flows)


def _sum_realized_by_period(
    realized_events: list[dict], method: str
) -> dict[str, float]:
    out: dict[str, float] = defaultdict(float)
    for e in realized_events:
        if str(e.get("cost_method") or "") != method:
            continue
        out[str(e.get("period_end"))] += float(e.get("realized_pnl_est") or 0.0)
    return dict(out)


def assert_mtm_identity(
    returns_df: pd.DataFrame,
    *,
    tol: float = 1.0,
    context: str = "mtm_identity",
) -> None:
    """Assert mtm_pnl == end − start − flow (after first period)."""
    if returns_df is None or len(returns_df) == 0:
        return
    for i, row in returns_df.iterrows():
        start = row.get("portfolio_mv_start")
        if start is None or (isinstance(start, float) and start != start):
            continue
        end = float(row["portfolio_mv_end"])
        flow = float(row["net_external_flow"])
        mtm = float(row["mtm_pnl"])
        expected = end - float(start) - flow
        if abs(mtm - expected) > tol:
            raise AssertionError(
                f"{context}: period {row.get('period_end')} mtm={mtm} "
                f"!= end-start-flow={expected} (tol={tol})"
            )


def period_portfolio_returns(
    history_rows: list[dict],
    realized_events: list[dict],
) -> pd.DataFrame:
    """One row per period_end: MV, flows, MTM, avg+fifo realized, Dietz."""
    mv = _portfolio_mv_by_period(history_rows)
    periods = sorted(mv.keys())
    avg_by = _sum_realized_by_period(realized_events, "avg")
    fifo_by = _sum_realized_by_period(realized_events, "fifo")

    flows_tt = _flows_by_period_ticker(history_rows)
    flow_by_pe: dict[str, float] = defaultdict(float)
    for (pe, _t), f in flows_tt.items():
        flow_by_pe[pe] += f

    rows_out: list[dict] = []
    cum_avg = 0.0
    cum_fifo = 0.0
    cum_growth = 1.0
    prev_pe: str | None = None
    for pe in periods:
        end_mv = mv[pe]
        start_mv = mv[prev_pe] if prev_pe is not None else 0.0
        net_flow = float(flow_by_pe.get(pe, 0.0))
        if prev_pe is None:
            mtm = 0.0
            dietz = None
            net_flow_display = end_mv
        else:
            mtm = end_mv - start_mv - net_flow
            dietz = _dietz_return(start_mv, end_mv, net_flow)
            net_flow_display = net_flow
        avg_r = float(avg_by.get(pe, 0.0))
        fifo_r = float(fifo_by.get(pe, 0.0))
        cum_avg += avg_r
        cum_fifo += fifo_r
        if dietz is not None:
            cum_growth *= 1.0 + dietz
        rows_out.append(
            {
                "period_end": pe,
                "portfolio_mv_start": start_mv if prev_pe else None,
                "portfolio_mv_end": end_mv,
                "net_external_flow": net_flow_display,
                "mtm_pnl": mtm,
                "realized_pnl_avg_est": avg_r,
                "realized_pnl_fifo_est": fifo_r,
                # Alias: primary disposal = avg-cost (do not treat as company P&L)
                "realized_pnl_est": avg_r,
                "dietz_return": dietz,
                "cum_realized_pnl_avg_est": cum_avg,
                "cum_realized_pnl_fifo_est": cum_fifo,
                "cum_realized_pnl_est": cum_avg,
                "cum_dietz_growth": cum_growth if prev_pe else None,
                "note": NOTE_EST,
            }
        )
        prev_pe = pe

    cols = [
        "period_end",
        "portfolio_mv_start",
        "portfolio_mv_end",
        "net_external_flow",
        "mtm_pnl",
        "realized_pnl_avg_est",
        "realized_pnl_fifo_est",
        "realized_pnl_est",
        "dietz_return",
        "cum_realized_pnl_avg_est",
        "cum_realized_pnl_fifo_est",
        "cum_realized_pnl_est",
        "cum_dietz_growth",
        "note",
    ]
    df = pd.DataFrame(rows_out, columns=cols)
    assert_mtm_identity(df)
    return df


def holding_period_returns(
    history_rows: list[dict],
    realized_events: list[dict],
) -> pd.DataFrame:
    """Per ticker × period: weight, Dietz, contribution, MTM, avg+fifo realized."""
    mv_port = _portfolio_mv_by_period(history_rows)
    flows_tt = _flows_by_period_ticker(history_rows)

    mv_tt: dict[tuple[str, str], float] = {}
    meta_tt: dict[tuple[str, str], dict] = {}
    for r in history_rows:
        if str(r.get("action") or "") == "exit":
            continue
        t = _ticker_key(r)
        pe = str(r.get("period_end") or "")
        mv = _f(r.get("market_value_usd"))
        if not t or not pe or mv is None:
            continue
        mv_tt[(pe, t)] = mv_tt.get((pe, t), 0.0) + mv
        meta_tt[(pe, t)] = r

    avg_tt: dict[tuple[str, str], float] = defaultdict(float)
    fifo_tt: dict[tuple[str, str], float] = defaultdict(float)
    for e in realized_events:
        pe = str(e.get("period_end") or "")
        t = normalize_ticker(e.get("investee_ticker")) or str(e.get("investee_ticker") or "")
        key = (pe, t)
        pnl = float(e.get("realized_pnl_est") or 0.0)
        if str(e.get("cost_method") or "") == "fifo":
            fifo_tt[key] += pnl
        else:
            avg_tt[key] += pnl

    periods = sorted(mv_port.keys())
    prev_mv_t: dict[str, float] = {}
    out: list[dict] = []
    for pe in periods:
        port_end = mv_port.get(pe) or 0.0
        tickers = sorted({t for (p, t) in mv_tt if p == pe} | set(prev_mv_t.keys()))
        for t in tickers:
            end_mv = mv_tt.get((pe, t), 0.0)
            start_mv = float(prev_mv_t.get(t, 0.0))
            net_flow = float(flows_tt.get((pe, t), 0.0))
            if start_mv <= 0 and end_mv <= 0 and abs(net_flow) < 1e-6:
                continue
            if start_mv <= 0:
                mtm = 0.0
                dietz = None
            else:
                mtm = end_mv - start_mv - net_flow
                dietz = _dietz_return(start_mv, end_mv, net_flow)
            weight = (end_mv / port_end) if port_end > 0 else None
            contrib = (weight * dietz) if (weight is not None and dietz is not None) else None
            src = meta_tt.get((pe, t), {})
            avg_r = float(avg_tt.get((pe, t), 0.0))
            out.append(
                {
                    "period_end": pe,
                    "investee_ticker": t,
                    "investee_name": src.get("investee_name"),
                    "market_value_usd": end_mv if end_mv > 0 else None,
                    "weight": weight,
                    "holding_dietz_return": dietz,
                    "contribution": contrib,
                    "mtm_pnl": mtm,
                    "realized_pnl_avg_est": avg_r,
                    "realized_pnl_fifo_est": float(fifo_tt.get((pe, t), 0.0)),
                    "realized_pnl_est": avg_r,
                    "filing_url": src.get("filing_url"),
                    "note": NOTE_EST,
                }
            )
        prev_mv_t = {t: mv_tt[(pe, t)] for (p, t) in mv_tt if p == pe}

    cols = [
        "period_end",
        "investee_ticker",
        "investee_name",
        "market_value_usd",
        "weight",
        "holding_dietz_return",
        "contribution",
        "mtm_pnl",
        "realized_pnl_avg_est",
        "realized_pnl_fifo_est",
        "realized_pnl_est",
        "filing_url",
        "note",
    ]
    if not out:
        return pd.DataFrame(columns=cols)
    return (
        pd.DataFrame(out, columns=cols)
        .sort_values(["period_end", "investee_ticker"], kind="mergesort")
        .reset_index(drop=True)
    )


def realized_events_frame(realized_events: list[dict]) -> pd.DataFrame:
    cols = [
        "period_end",
        "investee_ticker",
        "investee_name",
        "shares_sold",
        "cost_px",
        "exit_px",
        "realized_pnl_est",
        "cost_method",
        "holding_periods",
        "lot_opened_period",
        "filing_url",
        "accession_no",
        "note",
    ]
    if not realized_events:
        return pd.DataFrame(columns=cols)
    df = pd.DataFrame(realized_events)
    for c in cols:
        if c not in df.columns:
            df[c] = None
    return df[cols].sort_values(
        ["period_end", "investee_ticker", "cost_method", "lot_opened_period"],
        kind="mergesort",
    ).reset_index(drop=True)


def load_reported_investment_income(parent: str) -> pd.DataFrame:
    """Load curated Interest and investment income for a parent (BABA v1)."""
    cols = [
        "parent",
        "period_label",
        "fiscal_year_end",
        "calendar_start",
        "calendar_end",
        "amount_rmb_million",
        "amount_usd",
        "line_item",
        "source_url",
        "note",
    ]
    key = normalize_ticker(parent) or str(parent or "").strip().upper()
    path = _DATA_DIR / f"{key.lower()}_investment_income.yaml"
    if not path.is_file():
        return pd.DataFrame(columns=cols)
    try:
        import yaml
    except ImportError:
        # Minimal YAML subset via pandas if PyYAML missing — prefer yaml
        raise RuntimeError("PyYAML required to load reported investment income") from None

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    rows: list[dict] = []
    line = raw.get("line_item") or "Interest and investment income, net"
    for p in raw.get("periods") or []:
        usd_m = p.get("amount_usd_million")
        rows.append(
            {
                "parent": key,
                "period_label": p.get("period_label"),
                "fiscal_year_end": p.get("fiscal_year_end"),
                "calendar_start": p.get("calendar_start"),
                "calendar_end": p.get("calendar_end"),
                "amount_rmb_million": p.get("amount_rmb_million"),
                "amount_usd": (float(usd_m) * 1e6) if usd_m is not None else None,
                "line_item": line,
                "source_url": p.get("source_url") or raw.get("source_url"),
                "note": p.get("note") or NOTE_RECON,
            }
        )
    return pd.DataFrame(rows, columns=cols)


def reported_vs_est_frame(
    history_rows: list[dict],
    parent: str,
) -> pd.DataFrame:
    """Reconcile company-reported investment income vs summed calendar MTM.

    Residual is expected to be large; this is not an equality gate.
    """
    cols = [
        "period_label",
        "fiscal_year_end",
        "company_reported_usd",
        "company_reported_rmb_million",
        "our_mtm_sum_usd",
        "residual_usd",
        "note",
    ]
    reported = load_reported_investment_income(parent)
    if len(reported) == 0:
        return pd.DataFrame(columns=cols)

    returns = period_portfolio_returns(history_rows, build_lots_and_realized(history_rows)[1])
    out: list[dict] = []
    for _, rep in reported.iterrows():
        start = str(rep.get("calendar_start") or "")
        end = str(rep.get("calendar_end") or "")
        mask = (returns["period_end"].astype(str) >= start) & (
            returns["period_end"].astype(str) <= end
        )
        our_mtm = float(returns.loc[mask, "mtm_pnl"].sum()) if len(returns) else 0.0
        company = rep.get("amount_usd")
        company_f = float(company) if company is not None and company == company else None
        residual = (our_mtm - company_f) if company_f is not None else None
        out.append(
            {
                "period_label": rep.get("period_label"),
                "fiscal_year_end": rep.get("fiscal_year_end"),
                "company_reported_usd": company_f,
                "company_reported_rmb_million": rep.get("amount_rmb_million"),
                "our_mtm_sum_usd": our_mtm,
                "residual_usd": residual,
                "note": NOTE_RECON,
            }
        )
    return pd.DataFrame(out, columns=cols)


def returns_chart_frame(returns_by_period: pd.DataFrame) -> pd.DataFrame:
    """Narrow matrix for combo chart: QoQ Dietz % + cumulative growth index."""
    cols = ["period_end", "dietz_return_pct", "cum_growth_index"]
    if returns_by_period is None or len(returns_by_period) == 0:
        return pd.DataFrame(columns=cols)
    out = pd.DataFrame(
        {
            "period_end": returns_by_period["period_end"].astype(str),
            "dietz_return_pct": returns_by_period["dietz_return"].apply(
                lambda x: float(x) * 100.0 if x is not None and x == x else None
            ),
            "cum_growth_index": returns_by_period["cum_dietz_growth"],
        }
    )
    return out[cols]


def linked_dietz_cagr(returns_by_period: pd.DataFrame) -> dict[str, Any]:
    """CAGR from linked Dietz: cum_end^(1/years) − 1.

    ``years`` = (# of finite QoQ Dietz returns) / 4 — each return is one quarter.
    """
    empty = {
        "cagr": None,
        "years": None,
        "cum_end": None,
        "start_period": None,
        "end_period": None,
    }
    if returns_by_period is None or len(returns_by_period) == 0:
        return empty
    df = returns_by_period.copy()
    df = df[df["dietz_return"].notna()].sort_values("period_end", kind="mergesort")
    if len(df) < 1:
        return empty
    with_cum = df[df["cum_dietz_growth"].notna()]
    if len(with_cum) < 1:
        return empty
    start_pe = str(with_cum.iloc[0]["period_end"])
    end_pe = str(with_cum.iloc[-1]["period_end"])
    cum_end = float(with_cum.iloc[-1]["cum_dietz_growth"])
    n_q = len(with_cum)
    years = n_q / 4.0
    if years <= 0 or cum_end <= 0:
        cagr = None
    else:
        cagr = cum_end ** (1.0 / years) - 1.0
    return {
        "cagr": cagr,
        "years": years,
        "cum_end": cum_end,
        "start_period": start_pe,
        "end_period": end_pe,
    }


def returns_chart_with_cagr_footer(returns_by_period: pd.DataFrame) -> pd.DataFrame:
    """Chart matrix plus blank separator + CAGR footer row for Sheets/CSV."""
    chart = returns_chart_frame(returns_by_period)
    info = linked_dietz_cagr(returns_by_period)
    if len(chart) == 0:
        return chart
    blank = {
        "period_end": "",
        "dietz_return_pct": None,
        "cum_growth_index": None,
    }
    cagr = info.get("cagr")
    years = info.get("years")
    if cagr is None:
        footer = {
            "period_end": "CAGR (estimated Dietz)",
            "dietz_return_pct": None,
            "cum_growth_index": None,
        }
        note_row = {
            "period_end": "note=estimated; Modified Dietz; not tax/GAAP",
            "dietz_return_pct": None,
            "cum_growth_index": None,
        }
    else:
        footer = {
            "period_end": "CAGR (estimated Dietz)",
            "dietz_return_pct": float(cagr) * 100.0,
            "cum_growth_index": years,
        }
        note_row = {
            "period_end": (
                f"years={years:.2f}; {info.get('start_period')}→{info.get('end_period')}; "
                "estimated; Modified Dietz; not tax/GAAP"
            ),
            "dietz_return_pct": None,
            "cum_growth_index": None,
        }
    return pd.concat(
        [chart, pd.DataFrame([blank, footer, note_row])],
        ignore_index=True,
    )


def realized_chart_frame(returns_by_period: pd.DataFrame) -> pd.DataFrame:
    """Combo-chart matrix: QoQ avg-cost realized ($M) + cumulative ($M)."""
    cols = ["period_end", "realized_pnl_qoq_m", "cum_realized_pnl_m"]
    if returns_by_period is None or len(returns_by_period) == 0:
        return pd.DataFrame(columns=cols)
    qoq = returns_by_period.get("realized_pnl_est")
    if qoq is None:
        qoq = returns_by_period.get("realized_pnl_avg_est")
    cum = returns_by_period.get("cum_realized_pnl_est")
    if cum is None:
        cum = returns_by_period.get("cum_realized_pnl_avg_est")
    out = pd.DataFrame(
        {
            "period_end": returns_by_period["period_end"].astype(str),
            "realized_pnl_qoq_m": pd.to_numeric(qoq, errors="coerce") / 1e6,
            "cum_realized_pnl_m": pd.to_numeric(cum, errors="coerce") / 1e6,
        }
    )
    return out[cols]


def realized_by_ticker_chart_frame(
    realized_events: pd.DataFrame | list[dict],
    *,
    max_tickers: int = 15,
    min_abs_usd: float = 1_000_000.0,
) -> pd.DataFrame:
    """Column-chart matrix: ticker vs total estimated avg-cost realized ($M).

    FIFO rows ignored. Prefer no OTHER bucket; drop tiny tails under ``min_abs_usd``
    when more than ``max_tickers`` remain after the floor filter.
    """
    cols = ["investee_ticker", "realized_pnl_m"]
    if isinstance(realized_events, pd.DataFrame):
        df = realized_events.copy()
    else:
        df = pd.DataFrame(list(realized_events or []))
    if df.empty or "realized_pnl_est" not in df.columns:
        return pd.DataFrame(columns=cols)
    if "cost_method" in df.columns:
        df = df[df["cost_method"].astype(str) == "avg"]
    if df.empty:
        return pd.DataFrame(columns=cols)
    tcol = "investee_ticker" if "investee_ticker" in df.columns else None
    if not tcol:
        return pd.DataFrame(columns=cols)
    df = df.copy()
    df["investee_ticker"] = df[tcol].astype(str).str.strip().str.upper()
    df = df[df["investee_ticker"].ne("") & df["investee_ticker"].ne("NAN")]
    df["realized_pnl_est"] = pd.to_numeric(df["realized_pnl_est"], errors="coerce")
    df = df[df["realized_pnl_est"].notna()]
    if df.empty:
        return pd.DataFrame(columns=cols)
    grouped = (
        df.groupby("investee_ticker", as_index=False)["realized_pnl_est"]
        .sum()
        .rename(columns={"realized_pnl_est": "realized_pnl_usd"})
    )
    grouped["abs_usd"] = grouped["realized_pnl_usd"].abs()
    grouped = grouped[grouped["abs_usd"] >= float(min_abs_usd)]
    grouped = grouped.sort_values("abs_usd", ascending=False, kind="mergesort")
    if len(grouped) > max_tickers:
        grouped = grouped.head(max_tickers)
    out = pd.DataFrame(
        {
            "investee_ticker": grouped["investee_ticker"].astype(str),
            "realized_pnl_m": grouped["realized_pnl_usd"] / 1e6,
        }
    )
    return out.reset_index(drop=True)[cols]


def performance_frames(
    hist: pd.DataFrame | list[dict],
    *,
    parent: str | None = None,
) -> dict[str, pd.DataFrame]:
    """Build all performance tabs from history DataFrame or row list."""
    if isinstance(hist, pd.DataFrame):
        rows = hist.to_dict(orient="records") if len(hist) else []
    else:
        rows = list(hist or [])
    _, realized = build_lots_and_realized(rows)
    returns = period_portfolio_returns(rows, realized)
    realized_df = realized_events_frame(realized)
    frames = {
        "returns_by_period": returns,
        "realized_pnl_qoq": realized_df,
        "holding_returns": holding_period_returns(rows, realized),
        "returns_chart": returns_chart_with_cagr_footer(returns),
        "realized_chart": realized_chart_frame(returns),
        "realized_by_ticker_chart": realized_by_ticker_chart_frame(realized_df),
    }
    if parent:
        frames["reported_vs_est"] = reported_vs_est_frame(rows, parent)
    else:
        frames["reported_vs_est"] = pd.DataFrame(
            columns=[
                "period_label",
                "fiscal_year_end",
                "company_reported_usd",
                "company_reported_rmb_million",
                "our_mtm_sum_usd",
                "residual_usd",
                "note",
            ]
        )
    return frames