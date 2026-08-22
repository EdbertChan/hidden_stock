"""Inception backfill: lookback wall is not position inception."""

from hidden_stock.quirks.holdings.history import diff_snapshots
from hidden_stock.quirks.holdings.inception import (
    edge_held_tickers,
    filter_history_to_window,
    stamp_truncated_notes,
    truncated_edge_tickers,
)
from hidden_stock.quirks.holdings.performance import build_lots_and_realized


def _row(ticker, shares, value, *, name=None, cusip="090040106", note="source=sec_api_13f"):
    return {
        "investee_name": name or ticker,
        "investee_ticker": ticker,
        "shares_held": shares,
        "market_value_usd": value,
        "_cusip": cusip,
        "cusip": cusip,
        "note": note,
        "_source": "13f",
    }


def test_edge_held_tickers():
    rows = [
        _row("BILI", 10_000_000, 100.0),
        _row("XPEV", 0.0, 0.0),
        {"investee_ticker": "WB", "shares_held": None, "note": "x"},
    ]
    assert edge_held_tickers(rows) == {"BILI"}


def test_inception_backfill_first_seen_and_hold():
    """Pre-window BILI → in-window first action is hold, first_seen is 2019."""
    pre = [
        (
            "2019-12-31",
            "2020-02-14",
            "acc-2019",
            [_row("BILI", 10_000_000, 400_000_000)],
        ),
        (
            "2020-12-31",
            "2021-02-14",
            "acc-2020",
            [_row("BILI", 10_000_000, 500_000_000)],
        ),
    ]
    window = [
        (
            "2021-09-30",
            "2021-11-15",
            "acc-2021",
            [_row("BILI", 10_000_000, 661_700_000)],
        ),
        (
            "2024-03-31",
            "2024-05-13",
            "acc-exit",
            [],  # exited
        ),
    ]
    hist = diff_snapshots("BABA", pre + window)
    bili = [r for r in hist if r.get("investee_ticker") == "BILI"]
    assert bili
    assert bili[0]["period_end"] == "2019-12-31"
    assert bili[0]["action"] == "new"
    assert bili[0]["first_seen_period"] == "2019-12-31"
    win = next(r for r in bili if r["period_end"] == "2021-09-30")
    assert win["action"] == "hold"
    assert win["first_seen_period"] == "2019-12-31"

    _, realized = build_lots_and_realized(hist)
    avg = [e for e in realized if e.get("cost_method") == "avg" and e.get("investee_ticker") == "BILI"]
    assert avg
    assert avg[0]["lot_opened_period"] == "2019-12-31"
    # Cost px from 2019 open (400M/10M = 40), not 2021 (66.17)
    assert abs(float(avg[0]["cost_px"]) - 40.0) < 1e-6


def test_truncated_no_priced_buy_lot():
    """Edge-held with no pre-window proof → no invent priced lot at window edge."""
    window = [
        (
            "2021-09-30",
            "2021-11-15",
            "acc-2021",
            [_row("BILI", 10_000_000, 661_700_000)],
        ),
        (
            "2024-03-31",
            "2024-05-13",
            "acc-exit",
            [],
        ),
    ]
    hist = diff_snapshots("BABA", window)
    hist = stamp_truncated_notes(hist, {"BILI"})
    assert any("lookback_truncated=1" in str(r.get("note")) for r in hist if r.get("investee_ticker") == "BILI")
    _, realized = build_lots_and_realized(hist)
    avg = [
        e
        for e in realized
        if e.get("cost_method") == "avg"
        and e.get("investee_ticker") == "BILI"
        and e.get("realized_pnl_est") is not None
    ]
    assert avg == []
    unknown = [
        e
        for e in realized
        if e.get("investee_ticker") == "BILI" and "unknown_truncated" in str(e.get("note") or "")
    ]
    assert unknown


def test_truncated_edge_tickers_helper():
    pre = [
        (
            "2019-12-31",
            "2020-02-14",
            "a",
            [_row("BILI", 10_000_000, 1.0)],  # XPEV not yet
        ),
        (
            "2020-06-30",
            "2020-08-14",
            "b",
            [_row("BILI", 10_000_000, 1.0), _row("XPEV", 1_000_000, 1.0)],
        ),
        (
            "2020-12-31",
            "2021-02-14",
            "c",
            [_row("BILI", 10_000_000, 1.0)],  # XPEV exited before window
        ),
    ]
    # BILI still in oldest → truncated; XPEV absent from oldest → inception found
    trunc = truncated_edge_tickers(edge={"BILI", "XPEV"}, pre_periods=pre)
    assert "BILI" in trunc
    assert "XPEV" not in trunc


def test_filter_history_to_window():
    rows = [
        {"period_end": "2019-12-31", "investee_ticker": "BILI"},
        {"period_end": "2021-09-30", "investee_ticker": "BILI"},
    ]
    out = filter_history_to_window(rows, "2021-01-01")
    assert [r["period_end"] for r in out] == ["2021-09-30"]
