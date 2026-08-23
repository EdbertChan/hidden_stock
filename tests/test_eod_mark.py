"""Filing-date EOD×shares estimates (cost basis / mark) — never market_value_usd."""

from __future__ import annotations

from hidden_stock.quirks.holdings.eod_mark import apply_filing_mark_estimates
from hidden_stock.quirks.holdings.export import portfolio_by_period_frame
from hidden_stock.quirks.holdings.validate import assert_estimates_not_in_market_value
import pandas as pd


def _as_row(**kw):
    base = {
        "parent_ticker": "TCEHY",
        "period_end": "2024-02-09",
        "filing_date": "2024-02-09",
        "accession_no": "x",
        "filing_url": None,
        "investee_name": "Amer Sports, Inc.",
        "investee_ticker": "AS",
        "cusip": None,
        "shares_held": 31_588_292.0,
        "ownership_pct": 6.5,
        "market_value_usd": None,
        "shares_prev": None,
        "shares_delta": 31_588_292.0,
        "value_prev": None,
        "value_delta": None,
        "action": "new",
        "first_seen_period": "2024-02-09",
        "exited_period": None,
        "note": "source=13g form=SC 13D",
    }
    base.update(kw)
    return base


def test_as_cost_basis_est_on_new_carried_on_hold():
    history = [
        _as_row(action="new", period_end="2024-02-09", filing_date="2024-02-09"),
        _as_row(
            action="hold",
            period_end="2024-05-16",
            filing_date="2024-05-16",
            shares_prev=31_588_292.0,
            shares_delta=0.0,
            first_seen_period="2024-02-09",
        ),
        _as_row(
            action="hold",
            period_end="2025-11-12",
            filing_date="2025-11-12",
            shares_prev=31_588_292.0,
            shares_delta=0.0,
            first_seen_period="2024-02-09",
        ),
    ]

    def fake_price(ticker: str, as_of: str):
        # Distinct prices by date so mark changes but basis stays at first new.
        prices = {"2024-02-09": 10.0, "2024-05-16": 12.0, "2025-11-12": 20.0}
        return {
            "market_price": prices.get(as_of[:10], 10.0),
            "price_as_of": as_of[:10],
            "price_source": "test_fixture",
        }

    out = apply_filing_mark_estimates(history, fetch_price=fake_price)
    new_r, hold1, hold2 = out
    assert new_r["market_value_usd"] is None
    assert new_r["cost_basis_est_usd"] == 31_588_292.0 * 10.0
    assert new_r["cost_basis_est_price"] == 10.0
    assert new_r["mark_at_filing_est_usd"] == 31_588_292.0 * 10.0
    assert "value_estimate=eod_at_filing" in new_r["note"]
    assert "excluded_from_portfolio_mv" in new_r["note"]
    assert "estimate_role=cost_basis" in new_r["note"]

    assert hold1["cost_basis_est_usd"] == new_r["cost_basis_est_usd"]
    assert hold1["mark_at_filing_est_usd"] == 31_588_292.0 * 12.0
    assert hold1["market_value_usd"] is None
    assert "estimate_role=mark_at_filing" in hold1["note"]

    assert hold2["cost_basis_est_usd"] == new_r["cost_basis_est_usd"]
    assert hold2["mark_at_filing_est_usd"] == 31_588_292.0 * 20.0
    assert hold2["market_value_usd"] is None

    assert_estimates_not_in_market_value(out, context="test")

    # Portfolio MV ignores estimate-only rows (no market_value_usd).
    port = portfolio_by_period_frame(pd.DataFrame(out))
    assert port.empty or "AS" not in set(port["investee_ticker"].astype(str).str.upper())


def test_broker_dollar_skips_mark_but_may_carry_basis():
    history = [
        _as_row(action="new", period_end="2024-02-09"),
        _as_row(
            action="hold",
            period_end="2024-05-16",
            market_value_usd=500e6,
            note="source=13g; value_source=broker_sotp; excluded_from_portfolio_mv",
            shares_prev=31_588_292.0,
            shares_delta=0.0,
        ),
    ]

    def fake_price(ticker: str, as_of: str):
        return {"market_price": 10.0, "price_as_of": as_of[:10], "price_source": "test"}

    out = apply_filing_mark_estimates(history, fetch_price=fake_price)
    assert out[0]["cost_basis_est_usd"] == 31_588_292.0 * 10.0
    assert out[1].get("mark_at_filing_est_usd") is None  # skipped — has market_value
    assert out[1]["cost_basis_est_usd"] == out[0]["cost_basis_est_usd"]
    assert out[1]["market_value_usd"] == 500e6
    assert "excluded_from_portfolio_mv" in str(out[1].get("note") or "")
    assert_estimates_not_in_market_value(out, context="broker_carry")
