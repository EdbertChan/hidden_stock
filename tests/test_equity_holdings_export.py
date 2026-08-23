"""Unit tests for equity holdings CSV/Sheets frame helpers."""

from __future__ import annotations

import pandas as pd
import pytest

from hidden_stock.quirks.holdings.export import (
    _returns_sheet_frame,
    chart_data_frame,
    display_stack_by_period_frame,
    holdings_qoq_chart_frame,
    portfolio_by_period_frame,
    positions_qoq_frame,
    quarterly_display_stack_frame,
)


def test_returns_sheet_frame_adds_dietz_pct():
    raw = pd.DataFrame(
        [
            {
                "period_end": "2022-12-31",
                "dietz_return": -0.5,
                "cum_dietz_growth": 0.5,
            }
        ]
    )
    out = _returns_sheet_frame(raw)
    assert "dietz_return_pct" in out.columns
    assert out.iloc[0]["dietz_return_pct"] == pytest.approx(-50.0)
    # pct sits immediately after dietz_return
    cols = list(out.columns)
    assert cols.index("dietz_return_pct") == cols.index("dietz_return") + 1


def test_portfolio_by_period_sums_and_drops_exits():
    hist = pd.DataFrame(
        [
            {
                "period_end": "2024-03-31",
                "investee_ticker": "BILI",
                "investee_name": "Bilibili",
                "action": "hold",
                "market_value_usd": 100.0,
            },
            {
                "period_end": "2024-03-31",
                "investee_ticker": "BILI",
                "investee_name": "Bilibili",
                "action": "hold",
                "market_value_usd": 50.0,
            },
            {
                "period_end": "2024-06-30",
                "investee_ticker": "BILI",
                "investee_name": "Bilibili",
                "action": "exit",
                "market_value_usd": 0.0,
            },
            {
                "period_end": "2024-06-30",
                "investee_ticker": "",
                "investee_name": "Moonshot",
                "action": "hold",
                "market_value_usd": 10.0,
            },
        ]
    )
    out = portfolio_by_period_frame(hist)
    assert list(out.columns) == ["period_end", "investee_ticker", "market_value_usd"]
    q1 = out[out["period_end"] == "2024-03-31"]
    assert len(q1) == 1
    assert float(q1.iloc[0]["market_value_usd"]) == 150.0
    assert "BILI" not in set(out.loc[out["period_end"] == "2024-06-30", "investee_ticker"])
    assert "Moonshot" in set(out["investee_ticker"])


def test_positions_qoq_column_order():
    hist = pd.DataFrame(
        [
            {
                "period_end": "2024-03-31",
                "investee_ticker": "XPEV",
                "investee_name": "XPeng",
                "cusip": "ABC",
                "action": "buy",
                "shares_held": 1,
                "shares_prev": 0,
                "shares_delta": 1,
                "market_value_usd": 2,
                "value_prev": 0,
                "value_delta": 2,
                "filing_date": "2024-05-01",
                "accession_no": "x",
                "first_seen_period": "2024-03-31",
                "exited_period": None,
                "note": None,
            }
        ]
    )
    out = positions_qoq_frame(hist)
    assert out.iloc[0]["investee_ticker"] == "XPEV"
    assert "action" in out.columns


def test_chart_data_frame_wide_and_excludes_mrdb():
    hist = pd.DataFrame(
        [
            {
                "period_end": "2024-03-31",
                "investee_ticker": "XPEV",
                "investee_name": "XPeng",
                "action": "hold",
                "market_value_usd": 50_000_000,
            },
            {
                "period_end": "2024-03-31",
                "investee_ticker": "MRDB",
                "investee_name": "Meridian",
                "action": "hold",
                "market_value_usd": 2_000_000_000,
            },
            {
                "period_end": "2024-06-30",
                "investee_ticker": "XPEV",
                "investee_name": "XPeng",
                "action": "hold",
                "market_value_usd": 100_000_000,
            },
            {
                "period_end": "2024-06-30",
                "investee_ticker": "WB",
                "investee_name": "Weibo",
                "action": "hold",
                "market_value_usd": 10_000_000,
            },
        ]
    )
    wide = chart_data_frame(hist)
    assert "period_end" in wide.columns
    assert "XPEV" in wide.columns
    assert "WB" in wide.columns
    assert "MRDB" not in wide.columns
    assert "OTHER" not in wide.columns
    assert float(wide.loc[wide["period_end"] == "2024-06-30", "XPEV"].iloc[0]) == 100.0
    # top_n omits smaller names — still no OTHER
    wide_top = chart_data_frame(hist, top_n=1)
    assert list(c for c in wide_top.columns if c != "period_end") == ["XPEV"]
    assert "OTHER" not in wide_top.columns


def test_display_stack_uses_mark_est_skips_priv_aggregates():
    """TCEHY-class: chart named marks; PRIV Note 22 stays off the stack; portfolio SoT unchanged."""
    hist = pd.DataFrame(
        [
            {
                "period_end": "2024-02-09",
                "investee_ticker": "AS",
                "investee_name": "Amer Sports",
                "action": "new",
                "market_value_usd": None,
                "mark_at_filing_est_usd": 400_000_000.0,
                "note": "value_estimate=eod_at_filing; excluded_from_portfolio_mv",
            },
            {
                "period_end": "2024-02-09",
                "investee_ticker": "PRIV_HK_LISTED_INVESTEES_FV",
                "investee_name": "Listed investees FV",
                "action": "hold",
                "market_value_usd": 80_000_000_000.0,
                "note": "value_source=hk_annual_note22",
            },
            {
                "period_end": "2024-05-16",
                "investee_ticker": "AS",
                "investee_name": "Amer Sports",
                "action": "hold",
                "market_value_usd": None,
                "mark_at_filing_est_usd": 500_000_000.0,
                "note": "value_estimate=eod_at_filing; excluded_from_portfolio_mv",
            },
            {
                "period_end": "2024-05-16",
                "investee_ticker": "PDD",
                "investee_name": "PDD",
                "action": "hold",
                "market_value_usd": 10_000_000_000.0,
                "mark_at_filing_est_usd": None,
                "note": "value_source=broker_sotp; excluded_from_portfolio_mv",
            },
            {
                "period_end": "2024-05-16",
                "investee_ticker": "PRIV_HK_LISTED_INVESTEES_FV",
                "investee_name": "Listed investees FV",
                "action": "hold",
                "market_value_usd": 90_000_000_000.0,
                "note": "value_source=hk_annual_note22",
            },
        ]
    )
    disp = display_stack_by_period_frame(hist)
    assert set(disp["investee_ticker"]) == {"AS", "PDD"}
    assert "PRIV_HK_LISTED_INVESTEES_FV" not in set(disp["investee_ticker"])

    wide = holdings_qoq_chart_frame(hist)
    assert "AS" in wide.columns
    assert "PDD" in wide.columns
    assert "PRIV_HK_LISTED_INVESTEES_FV" not in wide.columns
    assert wide.attrs.get("chart_basis") == "display_estimate_or_broker"
    assert wide.attrs.get("chart_top_n") == 7
    # Filing dates collapse to calendar quarters
    assert "2024-02-09" not in set(wide["period_end"].astype(str))
    assert "2024-03-31" in set(wide["period_end"].astype(str))
    assert "2024-06-30" in set(wide["period_end"].astype(str))
    cols = [c for c in wide.columns if c != "period_end"]
    assert cols[0] == "PDD"
    assert float(wide.loc[wide["period_end"] == "2024-06-30", "AS"].iloc[0]) == 500.0

    port = portfolio_by_period_frame(hist)
    assert set(port["investee_ticker"]) == {"PRIV_HK_LISTED_INVESTEES_FV"}


def test_holdings_qoq_chart_defaults_to_top_7():
    rows = []
    for i, t in enumerate(["A", "B", "C", "D", "E", "F", "G", "H", "I"]):
        rows.append(
            {
                "period_end": "2025-09-30",
                "investee_ticker": t,
                "action": "hold",
                "market_value_usd": None,
                "mark_at_filing_est_usd": float((10 - i) * 1_000_000_000),
            }
        )
    wide = holdings_qoq_chart_frame(pd.DataFrame(rows))
    tickers = [c for c in wide.columns if c != "period_end"]
    assert len(tickers) == 7
    assert tickers == ["A", "B", "C", "D", "E", "F", "G"]
    assert "H" not in tickers and "I" not in tickers


def test_quarterly_stack_collapses_filing_dates_not_selloff():
    """2025-08-12 filing burst must not appear as its own x-axis tick."""
    hist = pd.DataFrame(
        [
            {
                "period_end": "2025-07-08",
                "investee_ticker": "AS",
                "action": "hold",
                "market_value_usd": None,
                "mark_at_filing_est_usd": 100_000_000.0,
            },
            {
                "period_end": "2025-08-12",
                "investee_ticker": "AS",
                "action": "hold",
                "market_value_usd": None,
                "mark_at_filing_est_usd": 110_000_000.0,
            },
            {
                "period_end": "2025-08-12",
                "investee_ticker": "SE",
                "action": "new",
                "market_value_usd": None,
                "mark_at_filing_est_usd": 200_000_000.0,
            },
            {
                "period_end": "2025-09-30",
                "investee_ticker": "AS",
                "action": "hold",
                "market_value_usd": None,
                "mark_at_filing_est_usd": 120_000_000.0,
            },
            {
                "period_end": "2025-09-30",
                "investee_ticker": "SE",
                "action": "hold",
                "market_value_usd": None,
                "mark_at_filing_est_usd": 210_000_000.0,
            },
        ]
    )
    q = quarterly_display_stack_frame(hist)
    periods = set(q["period_end"].astype(str))
    assert "2025-08-12" not in periods
    assert "2025-09-30" in periods
    # Mid-quarter AS mark carries into 2025-09-30 (overwritten by 9/30 filing)
    row = q[(q.period_end == "2025-09-30") & (q.investee_ticker == "AS")].iloc[0]
    assert float(row["display_value_usd"]) == 120_000_000.0
    wide = holdings_qoq_chart_frame(hist)
    assert list(wide["period_end"].astype(str)) == ["2025-09-30"] or "2025-09-30" in set(
        wide["period_end"].astype(str)
    )
    assert "2025-08-12" not in set(wide["period_end"].astype(str))
