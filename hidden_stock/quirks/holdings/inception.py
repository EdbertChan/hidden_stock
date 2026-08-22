"""Sparse inception backfill for lookback-truncated 13F holdings.

Edge-held names at the window cut are walked earlier until shares hit 0 (true
inception) or filings run out (lookback_truncated). Chart/portfolio stay windowed;
lots/first_seen use the extended history.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from .lookback import normalize_ticker


def edge_held_tickers(oldest_period_rows: list[dict]) -> set[str]:
    """Tickers with positive shares in the oldest in-window 13F period."""
    out: set[str] = set()
    for r in oldest_period_rows or []:
        t = normalize_ticker(r.get("investee_ticker"))
        if not t:
            continue
        try:
            sh = float(r.get("shares_held")) if r.get("shares_held") is not None else 0.0
        except (TypeError, ValueError):
            sh = 0.0
        if sh > 0:
            out.add(t)
    return out


def _day_before(iso: str) -> str:
    d = date.fromisoformat(str(iso)[:10])
    return (d - timedelta(days=1)).isoformat()


def _filter_rows_to_tickers(rows: list[dict], tickers: set[str]) -> list[dict]:
    if not tickers:
        return []
    out: list[dict] = []
    for r in rows or []:
        t = normalize_ticker(r.get("investee_ticker"))
        if t and t in tickers:
            out.append(dict(r))
    return out


def truncated_edge_tickers(
    *,
    edge: set[str],
    pre_periods: list[tuple[str, str, str, list[dict]]],
) -> set[str]:
    """Edge tickers still present in the oldest pre-window period (or no pre data)."""
    if not edge:
        return set()
    if not pre_periods:
        return set(edge)
    oldest_pe, _fd, _acc, rows = pre_periods[0]
    present = {
        normalize_ticker(r.get("investee_ticker"))
        for r in rows
        if normalize_ticker(r.get("investee_ticker"))
        and float(r.get("shares_held") or 0) > 0
    }
    # Truncated if never absent across pre periods (still in oldest snapshot).
    truncated: set[str] = set()
    for t in edge:
        if t in present:
            truncated.add(t)
            continue
        # Also truncated if ticker never appears in any pre period (can't prove prior zero
        # before window — actually if absent from all pre, first window is true new).
        # Only mark truncated when oldest pre still holds them.
    return truncated


def collect_prewindow_edge_periods(
    *,
    parent_ticker: str,
    edgar,
    window_start: str,
    edge: set[str],
    as_of: str | None = None,
    max_filings: int = 80,
) -> tuple[list[tuple[str, str, str, list[dict]]], set[str], dict[str, Any]]:
    """Oldest→newest pre-window 13F periods filtered to ``edge`` tickers.

    Returns (periods, truncated_tickers, meta).
    """
    from .history import _collect_13f_periods

    meta: dict[str, Any] = {
        "num_prewindow_periods": 0,
        "edge_tickers": sorted(edge),
        "truncated_tickers": [],
        "error": None,
    }
    if not edge or not window_start:
        return [], set(), meta

    # Filings with period/filed before the display window.
    pre_as_of = _day_before(window_start)
    ordered, api_meta = _collect_13f_periods(
        parent_ticker=parent_ticker,
        edgar=edgar,
        as_of=pre_as_of,
        max_filings=max_filings,
        lookback_start="2000-01-01",
    )
    if api_meta.get("error"):
        meta["error"] = api_meta["error"]

    pre: list[tuple[str, str, str, list[dict]]] = []
    for period_end, filing_date, accession, rows in ordered:
        if str(period_end)[:10] >= str(window_start)[:10]:
            continue
        filtered = _filter_rows_to_tickers(rows, edge)
        # Keep period even if empty for some edges — needed so diff can see exits to 0.
        # But empty periods for all edges add noise; keep if any edge row OR we need gaps.
        pre.append((period_end, filing_date, accession, filtered))

    # Drop leading empty periods (no edge tickers yet).
    while pre and not pre[0][3]:
        pre.pop(0)

    # Trim trailing empties before window.
    while pre and not pre[-1][3]:
        pre.pop()

    # Per-ticker: drop periods before each ticker's first appearance is handled by diff.
    # Truncation: still held in oldest remaining pre period.
    truncated = truncated_edge_tickers(edge=edge, pre_periods=pre)
    # True new in window: edge ticker never appears in any pre period → not truncated.
    appeared: set[str] = set()
    for _pe, _fd, _acc, rows in pre:
        for r in rows:
            t = normalize_ticker(r.get("investee_ticker"))
            if t:
                appeared.add(t)
    truncated = {t for t in truncated if t in appeared}
    # Edge tickers that never appear in pre are true window news — remove from truncated
    # (truncated_edge_tickers already requires present in oldest; if no pre, all edge
    # are truncated because we can't prove inception).
    if not pre:
        truncated = set(edge)

    meta["num_prewindow_periods"] = len(pre)
    meta["truncated_tickers"] = sorted(truncated)
    return pre, truncated, meta


def stamp_truncated_notes(rows: list[dict], truncated: set[str]) -> list[dict]:
    """Stamp lookback_truncated / cost_basis on window-edge opens for truncated names."""
    if not truncated:
        return rows
    out: list[dict] = []
    for r in rows:
        row = dict(r)
        t = normalize_ticker(row.get("investee_ticker"))
        if t and t in truncated:
            note = str(row.get("note") or "")
            if "lookback_truncated=1" not in note:
                note = f"{note}; lookback_truncated=1".strip("; ")
            if str(row.get("action") or "") in {"new", "buy"} and "cost_basis=unknown_truncated" not in note:
                note = f"{note}; cost_basis=unknown_truncated".strip("; ")
            row["note"] = note
        out.append(row)
    return out


def filter_history_to_window(history: list[dict], window_start: str | None) -> list[dict]:
    """Keep rows with period_end >= window_start (chart/portfolio)."""
    if not window_start:
        return history
    start = str(window_start)[:10]
    return [r for r in history if str(r.get("period_end") or "")[:10] >= start]
