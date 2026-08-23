"""Tencent Holdings (TCEHY): HK annual aggregates + SEC 13G/D (shared sec_13g)."""

from __future__ import annotations

from .identity import ISSUER_TICKER_HINTS, resolve_issuer_ticker
from .sec_13g import (
    collect_13g_period_snapshots,
    fetch_latest_13g_holdings,
)

TENCENT_CIK = "0001293451"

# Investees that left public markets without a timely SC 13G/A zeroing filing.
# After as_of, drop from running snapshots so QoQ emits a single exit.
# Glu Mobile: Electronic Arts completed acquisition 2021-04-29.
MNA_EXIT_CUTOFFS: dict[str, str] = {
    "GLUU": "2021-04-29",  # Electronic Arts completed Glu Mobile acquisition
    "FTCH": "2024-01-30",  # Farfetch business/assets sale (Coupang/Surpique)
}


def apply_mna_exit_cutoffs(
    ordered: list[tuple[str, str, str, list[dict]]],
    cutoffs: dict[str, str] | None = None,
    *,
    exit_events: dict[str, dict[str, str]] | None = None,
) -> tuple[list[tuple[str, str, str, list[dict]]], dict[str, dict[str, str]]]:
    """Strip acquired/delisted tickers after M&A as_of; record period-grid exit provenance."""
    cuts = {k.upper(): str(v)[:10] for k, v in (cutoffs or MNA_EXIT_CUTOFFS).items()}
    events = {k.upper(): dict(v) for k, v in (exit_events or {}).items()}
    if not cuts:
        return ordered, events
    out: list[tuple[str, str, str, list[dict]]] = []
    for period_end, filing_date, acc, rows in ordered:
        pe = str(period_end)[:10]
        kept: list[dict] = []
        for r in rows:
            t = str(r.get("investee_ticker") or "").strip().upper()
            if t in cuts and pe > cuts[t]:
                if t not in events:
                    events[t] = {
                        "accession": acc,
                        "filing_date": pe,
                        "note": f"exit_reason=mna_acquired; mna_as_of={cuts[t]}",
                    }
                continue
            kept.append(r)
        out.append((period_end, filing_date, acc, kept))
    return out, events


# Re-export for tests that import from tencent
__all__ = [
    "TENCENT_CIK",
    "ISSUER_TICKER_HINTS",
    "resolve_issuer_ticker",
    "fetch_tencent_13g_holdings",
    "collect_13g_period_snapshots",
    "build_13g_reporter_history",
    "hk_annual_aggregate_rows",
    "build_tencent_holdings",
    "_13g_raw_to_position",
    "MNA_EXIT_CUTOFFS",
    "apply_mna_exit_cutoffs",
]


def _13g_raw_to_position(parsed: dict, *, form: str, acc: str, filing_date: str, cik: str):
    """Test helper: map parsed dict → QoQ position (Tencent parent)."""
    from .sec_13g import raw_to_position

    return raw_to_position(
        parsed,
        parent_ticker="TCEHY",
        form=form,
        acc=acc,
        filing_date=filing_date,
        cik=cik,
    )


def fetch_tencent_13g_holdings(*, user_agent: str, max_filings: int = 120) -> list[dict]:
    """Latest SEC 13G/D subjects for Tencent.

    Default scan depth is higher than the generic 40: Tencent files often, and a
    shallow newest-first window drops large stakes (e.g. PDD) that still appear
    in history / broker SOTP.
    """
    rows, _meta = fetch_latest_13g_holdings(
        cik=TENCENT_CIK,
        parent_ticker="TCEHY",
        user_agent=user_agent,
        max_filings=max_filings,
    )
    return rows


def hk_annual_aggregate_rows(
    *,
    as_of: str | None = None,
    lookback_years: int = 8,
) -> list[dict]:
    """HKEX annual-report Note 22 / portfolio aggregates via ``parse_hk_annual``.

    ``as_of`` if set keeps only that FY year-end; otherwise all parsed years.
    """
    from .parse_hk_annual import collect_hk_annual_aggregate_rows

    rows, _meta = collect_hk_annual_aggregate_rows(
        stock_code="00700",
        lookback_years=lookback_years,
    )
    if as_of:
        target = str(as_of)[:10]
        rows = [r for r in rows if str(r.get("as_of_date") or "")[:10] == target]
    return rows


def _hk_rows_to_history(parent: str, raw_rows: list[dict]) -> list[dict]:
    """Map HK annual aggregate live rows → QoQ history rows (one FY period each)."""
    out: list[dict] = []
    for raw in raw_rows:
        as_of = str(raw.get("as_of_date") or "")[:10]
        if not as_of:
            continue
        out.append(
            {
                "parent_ticker": parent,
                "period_end": as_of,
                "filing_date": as_of,
                "accession_no": None,
                "filing_url": raw.get("filing_url"),
                "investee_name": raw.get("investee_name"),
                "investee_ticker": raw.get("investee_ticker"),
                "cusip": None,
                "shares_held": None,
                "ownership_pct": None,
                "market_value_usd": raw.get("market_value_usd"),
                "shares_prev": None,
                "shares_delta": None,
                "value_prev": None,
                "value_delta": None,
                "action": "hold",
                "first_seen_period": as_of,
                "exited_period": None,
                "note": raw.get("note"),
            }
        )
    return out


def build_13g_reporter_history(
    *,
    parent_ticker: str = "TCEHY",
    user_agent: str,
    max_filings: int = 80,
    lookback_start: str | None = None,
) -> tuple[list[dict], dict]:
    """QoQ history for Tencent: SC 13G/D + HKEX annual aggregates."""
    from .history import diff_snapshots
    from .lookback import coalesce_period_ticker, stamp_missing_tickers
    from .parents import normalize_parent
    from .parse_hk_annual import collect_hk_annual_aggregate_rows

    parent = normalize_parent(parent_ticker)
    ordered, meta = collect_13g_period_snapshots(
        cik=TENCENT_CIK,
        parent_ticker=parent,
        user_agent=user_agent,
        max_filings=max_filings,
        lookback_start=lookback_start,
    )
    meta["parent_ticker"] = parent
    meta["strategy"] = "fanout_13g_hk"
    meta["lookback_start"] = lookback_start
    if meta.get("error"):
        return [], meta

    ordered, exit_events = apply_mna_exit_cutoffs(
        ordered,
        exit_events=meta.get("exit_events") or {},
    )
    meta["exit_events"] = exit_events
    history = coalesce_period_ticker(
        stamp_missing_tickers(
            diff_snapshots(
                parent,
                ordered,
                exit_events=exit_events,
            )
        )
    )

    # Append HKEX annual aggregate periods (disclosed FV / carrying — not invent).
    lookback_years = 8
    if lookback_start:
        try:
            from datetime import date as _date

            y0 = int(str(lookback_start)[:4])
            lookback_years = max(1, _date.today().year - y0 + 1)
        except Exception:
            lookback_years = 8
    hk_rows, hk_meta = collect_hk_annual_aggregate_rows(
        stock_code="00700",
        lookback_years=lookback_years,
    )
    meta["hk_annual"] = hk_meta
    if lookback_start:
        hk_rows = [
            r
            for r in hk_rows
            if str(r.get("as_of_date") or "")[:10] >= str(lookback_start)[:10]
        ]
    history.extend(_hk_rows_to_history(parent, hk_rows))
    from .composition import stamp_hk_composition_parents

    history = stamp_hk_composition_parents(history)
    try:
        from .broker_sotp import apply_broker_overlay, materialize_broker_sotp

        broker = materialize_broker_sotp([parent])
        history = apply_broker_overlay(
            history, broker, mode="history", parent_ticker=parent
        )
        meta["broker_sotp_rows"] = len(broker)
    except Exception as e:
        meta["broker_sotp_error"] = str(e)
    # Re-stamp so broker-only names (and late overlays) get composition_* columns.
    history = stamp_hk_composition_parents(history)
    from .composition import finalize_composition_columns

    history = finalize_composition_columns(history)
    try:
        from .eod_mark import apply_filing_mark_estimates

        history = apply_filing_mark_estimates(history)
        meta["filing_mark_estimates"] = True
    except Exception as e:
        meta["filing_mark_estimates_error"] = str(e)
    history.sort(
        key=lambda r: (
            str(r.get("period_end") or ""),
            str(r.get("investee_ticker") or ""),
            str(r.get("investee_name") or ""),
        )
    )
    return history, meta


def build_tencent_holdings(*, user_agent: str) -> tuple[list[dict], dict]:
    """Live snapshot = open positions slice of QoQ history (single SoT)."""
    from .composition import live_holdings_from_history
    from .lookback import lookback_start_date

    hist, meta = build_13g_reporter_history(
        parent_ticker="TCEHY",
        user_agent=user_agent,
        max_filings=120,
        lookback_start=lookback_start_date(lookback_years=8),
    )
    meta["source"] = "history_slice+hk_annual_report+sec_13g+broker_sotp"
    rows = live_holdings_from_history(hist)
    meta["num_raw"] = len(hist)
    meta["num_live"] = len(rows)
    if rows:
        dates = [str(r.get("as_of_date") or "")[:10] for r in rows if r.get("as_of_date")]
        meta["filing_date"] = max(dates) if dates else meta.get("filing_date")
    return rows, meta
