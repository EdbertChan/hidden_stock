"""MTM + avg-cost / FIFO disposal estimates (period-end $/share proxy)."""

from __future__ import annotations

import pandas as pd
import pytest

from hidden_stock.quirks.holdings.performance import (
    assert_mtm_identity,
    build_lots_and_realized,
    holding_period_returns,
    linked_dietz_cagr,
    load_reported_investment_income,
    performance_frames,
    period_portfolio_returns,
    realized_by_ticker_chart_frame,
    realized_chart_frame,
    returns_chart_frame,
    returns_chart_with_cagr_footer,
)


def _by_method(realized: list[dict], method: str) -> list[dict]:
    return [e for e in realized if e.get("cost_method") == method]


def test_buy_then_full_exit_realized():
    rows = [
        {
            "period_end": "2024-03-31",
            "investee_ticker": "ABC",
            "investee_name": "Abc Co",
            "action": "new",
            "shares_held": 100.0,
            "shares_prev": 0.0,
            "shares_delta": 100.0,
            "market_value_usd": 1000.0,
            "value_prev": 0.0,
        },
        {
            "period_end": "2024-06-30",
            "investee_ticker": "ABC",
            "investee_name": "Abc Co",
            "action": "exit",
            "shares_held": 0.0,
            "shares_prev": 100.0,
            "shares_delta": -100.0,
            "market_value_usd": 0.0,
            "value_prev": 1500.0,
        },
    ]
    _, realized = build_lots_and_realized(rows)
    avg = _by_method(realized, "avg")
    fifo = _by_method(realized, "fifo")
    assert len(avg) == 1 and len(fifo) == 1
    # Single lot: avg == fifo; exit px = 15; cost = 10 → pnl = 500
    for ev in (avg[0], fifo[0]):
        assert ev["shares_sold"] == 100.0
        assert abs(ev["cost_px"] - 10.0) < 1e-9
        assert abs(ev["exit_px"] - 15.0) < 1e-9
        assert abs(ev["realized_pnl_est"] - 500.0) < 1e-6
        assert ev["lot_opened_period"] == "2024-03-31"
        assert ev["holding_periods"] == 1


def test_partial_sell_fifo_consumes_oldest_lot_first():
    rows = [
        {
            "period_end": "2024-03-31",
            "investee_ticker": "XYZ",
            "action": "new",
            "shares_held": 50.0,
            "shares_delta": 50.0,
            "market_value_usd": 500.0,  # $10
        },
        {
            "period_end": "2024-06-30",
            "investee_ticker": "XYZ",
            "action": "buy",
            "shares_held": 100.0,
            "shares_prev": 50.0,
            "shares_delta": 50.0,
            "market_value_usd": 2000.0,  # $20 on 100
        },
        {
            "period_end": "2024-09-30",
            "investee_ticker": "XYZ",
            "action": "sell",
            "shares_held": 40.0,
            "shares_prev": 100.0,
            "shares_delta": -60.0,
            "market_value_usd": 1200.0,  # $30
        },
    ]
    _, realized = build_lots_and_realized(rows)
    fifo = _by_method(realized, "fifo")
    avg = _by_method(realized, "avg")
    assert len(fifo) == 2
    assert abs(fifo[0]["shares_sold"] - 50.0) < 1e-9
    assert abs(fifo[0]["cost_px"] - 10.0) < 1e-9
    assert abs(fifo[0]["realized_pnl_est"] - 50.0 * 20.0) < 1e-6
    assert abs(fifo[1]["shares_sold"] - 10.0) < 1e-9
    assert abs(fifo[1]["cost_px"] - 20.0) < 1e-9
    assert abs(fifo[1]["realized_pnl_est"] - 10.0 * 10.0) < 1e-6
    # Avg cost of book = (50*10 + 50*20)/100 = 15; sell 60 @ 30 → +900
    assert len(avg) == 1
    assert abs(avg[0]["cost_px"] - 15.0) < 1e-9
    assert abs(avg[0]["realized_pnl_est"] - 60.0 * 15.0) < 1e-6


def test_xpev_style_avg_gain_vs_fifo_loss():
    """Late add-ons at low px: FIFO hits old high-cost lot; avg-cost can show a gain."""
    rows = [
        {
            "period_end": "2021-09-30",
            "investee_ticker": "XPEV",
            "action": "new",
            "shares_held": 6_650_000.0,
            "shares_delta": 6_650_000.0,
            "market_value_usd": 6_650_000.0 * 35.54,
        },
        {
            "period_end": "2024-09-30",
            "investee_ticker": "XPEV",
            "action": "buy",
            "shares_held": 31_309_232.0,
            "shares_prev": 6_650_000.0,
            "shares_delta": 24_659_232.0,
            "market_value_usd": 31_309_232.0 * 12.18,
        },
        {
            "period_end": "2024-12-31",
            "investee_ticker": "XPEV",
            "action": "buy",
            "shares_held": 37_959_232.0,
            "shares_prev": 31_309_232.0,
            "shares_delta": 6_650_000.0,
            "market_value_usd": 37_959_232.0 * 11.82,
        },
        {
            "period_end": "2025-03-31",
            "investee_ticker": "XPEV",
            "action": "sell",
            "shares_held": 34_872_212.0,
            "shares_prev": 37_959_232.0,
            "shares_delta": -3_087_020.0,
            "market_value_usd": 34_872_212.0 * 20.72,
        },
    ]
    _, realized = build_lots_and_realized(rows)
    avg = _by_method(realized, "avg")
    fifo = _by_method(realized, "fifo")
    assert len(avg) == 1
    assert avg[0]["realized_pnl_est"] > 0
    assert abs(avg[0]["realized_pnl_est"] - 13_924_550.0) < 1.0  # ~+$13.9M
    fifo_pnl = sum(e["realized_pnl_est"] for e in fifo)
    assert fifo_pnl < 0
    assert abs(fifo_pnl - (-45_749_636.0)) < 1.0  # ~-$45.7M


def test_hold_only_period_dietz_approx_mv_change_over_start():
    rows = [
        {
            "period_end": "2024-03-31",
            "investee_ticker": "HOLD",
            "action": "new",
            "shares_held": 10.0,
            "shares_delta": 10.0,
            "market_value_usd": 100.0,
        },
        {
            "period_end": "2024-06-30",
            "investee_ticker": "HOLD",
            "action": "hold",
            "shares_held": 10.0,
            "shares_prev": 10.0,
            "shares_delta": 0.0,
            "market_value_usd": 110.0,
            "value_prev": 100.0,
        },
    ]
    _, realized = build_lots_and_realized(rows)
    assert realized == []
    port = period_portfolio_returns(rows, realized)
    assert len(port) == 2
    second = port.iloc[1]
    assert abs(second["realized_pnl_est"]) < 1e-9
    assert abs(second["realized_pnl_avg_est"]) < 1e-9
    assert abs(second["realized_pnl_fifo_est"]) < 1e-9
    assert abs(second["dietz_return"] - 0.10) < 1e-9
    assert abs(second["mtm_pnl"] - 10.0) < 1e-9
    assert_mtm_identity(port)


def test_mtm_identity_assert_on_portfolio_returns():
    rows = [
        {
            "period_end": "2024-03-31",
            "investee_ticker": "A",
            "action": "new",
            "shares_held": 10.0,
            "shares_delta": 10.0,
            "market_value_usd": 100.0,
        },
        {
            "period_end": "2024-06-30",
            "investee_ticker": "A",
            "action": "buy",
            "shares_held": 20.0,
            "shares_prev": 10.0,
            "shares_delta": 10.0,
            "market_value_usd": 240.0,  # $12
            "value_prev": 100.0,
        },
    ]
    port = period_portfolio_returns(rows, [])
    assert_mtm_identity(port)
    # Q2: start 100, end 240, flow = 10*12 = 120 → mtm = 240-100-120 = 20
    assert abs(port.iloc[1]["mtm_pnl"] - 20.0) < 1e-6


def test_missing_price_row_excluded_no_crash():
    rows = [
        {
            "period_end": "2024-03-31",
            "investee_ticker": "SERV",
            "action": "new",
            "shares_held": 100.0,
            "shares_delta": 100.0,
            "market_value_usd": None,
        },
        {
            "period_end": "2024-03-31",
            "investee_ticker": "GOOD",
            "action": "new",
            "shares_held": 10.0,
            "shares_delta": 10.0,
            "market_value_usd": 100.0,
        },
        {
            "period_end": "2024-06-30",
            "investee_ticker": "SERV",
            "action": "exit",
            "shares_held": 0.0,
            "shares_prev": 100.0,
            "shares_delta": -100.0,
            "market_value_usd": 0.0,
            "value_prev": None,
        },
        {
            "period_end": "2024-06-30",
            "investee_ticker": "GOOD",
            "action": "hold",
            "shares_held": 10.0,
            "shares_delta": 0.0,
            "market_value_usd": 120.0,
            "value_prev": 100.0,
        },
    ]
    frames = performance_frames(rows)
    port = frames["returns_by_period"]
    assert abs(port.iloc[0]["portfolio_mv_end"] - 100.0) < 1e-9
    assert abs(port.iloc[1]["portfolio_mv_end"] - 120.0) < 1e-9
    real = frames["realized_pnl_qoq"]
    assert len(real) == 0 or "SERV" not in set(real["investee_ticker"].tolist())


def test_holding_returns_weight_and_contribution():
    rows = [
        {
            "period_end": "2024-03-31",
            "investee_ticker": "A",
            "action": "new",
            "shares_held": 1.0,
            "shares_delta": 1.0,
            "market_value_usd": 50.0,
        },
        {
            "period_end": "2024-03-31",
            "investee_ticker": "B",
            "action": "new",
            "shares_held": 1.0,
            "shares_delta": 1.0,
            "market_value_usd": 50.0,
        },
        {
            "period_end": "2024-06-30",
            "investee_ticker": "A",
            "action": "hold",
            "shares_held": 1.0,
            "shares_delta": 0.0,
            "market_value_usd": 60.0,
            "value_prev": 50.0,
        },
        {
            "period_end": "2024-06-30",
            "investee_ticker": "B",
            "action": "hold",
            "shares_held": 1.0,
            "shares_delta": 0.0,
            "market_value_usd": 40.0,
            "value_prev": 50.0,
        },
    ]
    _, realized = build_lots_and_realized(rows)
    hr = holding_period_returns(rows, realized)
    q2 = hr[hr["period_end"] == "2024-06-30"].set_index("investee_ticker")
    assert abs(q2.loc["A", "weight"] - 0.6) < 1e-9
    assert abs(q2.loc["B", "weight"] - 0.4) < 1e-9
    assert abs(q2.loc["A", "holding_dietz_return"] - 0.2) < 1e-9
    assert abs(q2.loc["A", "contribution"] - 0.6 * 0.2) < 1e-9


def test_performance_frames_empty():
    frames = performance_frames(pd.DataFrame())
    assert list(frames["returns_by_period"].columns)
    assert len(frames["returns_by_period"]) == 0
    assert len(frames["realized_pnl_qoq"]) == 0
    assert len(frames["holding_returns"]) == 0
    assert "reported_vs_est" in frames


def test_reported_yaml_loads_baba_empty_unknown():
    baba = load_reported_investment_income("BABA")
    assert len(baba) >= 3
    assert "FY2026" in set(baba["period_label"])
    fy26 = baba[baba["period_label"] == "FY2026"].iloc[0]
    assert fy26["amount_usd"] == pytest.approx(12_687e6)
    unknown = load_reported_investment_income("ZZZZNOPE")
    assert len(unknown) == 0


def test_reported_vs_est_baba_has_residual_note():
    rows = [
        {
            "period_end": "2025-06-30",
            "investee_ticker": "A",
            "action": "new",
            "shares_held": 1.0,
            "shares_delta": 1.0,
            "market_value_usd": 1e9,
        },
        {
            "period_end": "2025-09-30",
            "investee_ticker": "A",
            "action": "hold",
            "shares_held": 1.0,
            "shares_delta": 0.0,
            "market_value_usd": 1.1e9,
        },
        {
            "period_end": "2025-12-31",
            "investee_ticker": "A",
            "action": "hold",
            "shares_held": 1.0,
            "shares_delta": 0.0,
            "market_value_usd": 1.2e9,
        },
        {
            "period_end": "2026-03-31",
            "investee_ticker": "A",
            "action": "hold",
            "shares_held": 1.0,
            "shares_delta": 0.0,
            "market_value_usd": 1.3e9,
        },
    ]
    frames = performance_frames(rows, parent="BABA")
    recon = frames["reported_vs_est"]
    assert len(recon) >= 1
    fy26 = recon[recon["period_label"] == "FY2026"].iloc[0]
    assert fy26["company_reported_usd"] == pytest.approx(12_687e6)
    assert "reconciliation" in str(fy26["note"]).lower() or "residual" in str(fy26["note"]).lower()
    assert fy26["residual_usd"] is not None
    assert abs(fy26["residual_usd"]) > 1e6


def test_linked_dietz_cagr_four_quarters_plus_10pct():
    """4× +10% Dietz over 1y → CAGR ≈ 46.41%."""
    # Build returns_by_period-shaped frame directly
    rows = [
        {
            "period_end": "2024-03-31",
            "dietz_return": None,
            "cum_dietz_growth": None,
        },
        {
            "period_end": "2024-06-30",
            "dietz_return": 0.10,
            "cum_dietz_growth": 1.1,
        },
        {
            "period_end": "2024-09-30",
            "dietz_return": 0.10,
            "cum_dietz_growth": 1.1**2,
        },
        {
            "period_end": "2024-12-31",
            "dietz_return": 0.10,
            "cum_dietz_growth": 1.1**3,
        },
        {
            "period_end": "2025-03-31",
            "dietz_return": 0.10,
            "cum_dietz_growth": 1.1**4,
        },
    ]
    df = pd.DataFrame(rows)
    info = linked_dietz_cagr(df)
    # Calendar span from first portfolio period (2024-03-31) through last cum.
    assert info["start_period"] == "2024-03-31"
    assert info["end_period"] == "2025-03-31"
    assert info["years"] == pytest.approx(1.0, abs=0.002)
    expected = (1.1**4) ** (1.0 / info["years"]) - 1.0
    assert info["cagr"] == pytest.approx(expected, rel=1e-9)
    assert abs(info["cagr"] - 0.4641) < 2e-3

    chart = returns_chart_frame(df)
    assert list(chart.columns) == ["period_end", "dietz_return_pct", "cum_growth_index"]
    assert chart.iloc[1]["dietz_return_pct"] == pytest.approx(10.0)

    with_footer = returns_chart_with_cagr_footer(df)
    assert any(
        str(x).startswith("CAGR") for x in with_footer["period_end"].tolist()
    )
    cagr_row = with_footer[
        with_footer["period_end"].astype(str).str.startswith("CAGR")
    ].iloc[0]
    assert pd.isna(cagr_row["cum_growth_index"]) or cagr_row["cum_growth_index"] is None
    frames = performance_frames(pd.DataFrame())
    assert "returns_chart" in frames
    assert "realized_chart" in frames
    assert "realized_by_ticker_chart" in frames


def test_realized_chart_frame_scales_to_millions():
    df = pd.DataFrame(
        [
            {
                "period_end": "2022-06-30",
                "realized_pnl_est": -10_000_000.0,
                "cum_realized_pnl_est": -10_000_000.0,
            },
            {
                "period_end": "2022-09-30",
                "realized_pnl_est": 0.0,
                "cum_realized_pnl_est": -10_000_000.0,
            },
        ]
    )
    chart = realized_chart_frame(df)
    assert list(chart.columns) == [
        "period_end",
        "realized_pnl_qoq_m",
        "cum_realized_pnl_m",
    ]
    assert chart.iloc[0]["realized_pnl_qoq_m"] == pytest.approx(-10.0)
    assert chart.iloc[1]["cum_realized_pnl_m"] == pytest.approx(-10.0)


def test_realized_by_ticker_ignores_fifo_and_drops_tiny():
    rows = [
        {
            "investee_ticker": "AAA",
            "realized_pnl_est": 5_000_000.0,
            "cost_method": "avg",
        },
        {
            "investee_ticker": "AAA",
            "realized_pnl_est": 5_000_000.0,
            "cost_method": "fifo",  # ignored
        },
        {
            "investee_ticker": "BBB",
            "realized_pnl_est": -3_000_000.0,
            "cost_method": "avg",
        },
        {
            "investee_ticker": "TINY",
            "realized_pnl_est": 100_000.0,
            "cost_method": "avg",
        },
    ]
    chart = realized_by_ticker_chart_frame(rows)
    tickers = chart["investee_ticker"].tolist()
    assert tickers == ["AAA", "BBB"]
    assert chart.iloc[0]["realized_pnl_m"] == pytest.approx(5.0)
    assert chart.iloc[1]["realized_pnl_m"] == pytest.approx(-3.0)


def test_realized_chart_frames_empty_safe():
    assert list(realized_chart_frame(pd.DataFrame()).columns) == [
        "period_end",
        "realized_pnl_qoq_m",
        "cum_realized_pnl_m",
    ]
    assert list(realized_by_ticker_chart_frame([]).columns) == [
        "investee_ticker",
        "realized_pnl_m",
    ]