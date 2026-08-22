"""Unit tests for equity holdings GAAP + rollup + MTM adj rules."""

from __future__ import annotations

from hidden_stock.quirks.holdings.gaap import assign_gaap_treatment, ownership_band
from hidden_stock.quirks.holdings.mtm import apply_gaap_and_adj
from hidden_stock.quirks.holdings.rollup import rollup_holdings


def test_ownership_bands():
    assert ownership_band(10) == "lt_20"
    assert ownership_band(20) == "ge_20_lt_50"
    assert ownership_band(49.9) == "ge_20_lt_50"
    assert ownership_band(50) == "ge_50"
    assert ownership_band(None) == "unknown"


def test_gaap_lt20_default_fv_ni():
    g = assign_gaap_treatment(ownership_pct=15)
    assert g["gaap_treatment"] == "fv_ni"
    assert g["impacts_parent_ni"] is True
    assert g["include_in_book_adj"] is False
    assert g["ni_mechanism"] == "fv_changes"


def test_gaap_cost_alternative_book_adj():
    g = assign_gaap_treatment(ownership_pct=10, measurement_alternative=True)
    assert g["gaap_treatment"] == "measurement_alternative_cost"
    assert g["include_in_book_adj"] is True
    assert g["impacts_parent_ni"] is False


def test_gaap_equity_method_band():
    g = assign_gaap_treatment(ownership_pct=25)
    assert g["gaap_treatment"] == "equity_method"
    assert g["include_in_book_adj"] is False
    assert g["impacts_parent_ni"] is True


def test_gaap_consolidated_band():
    g = assign_gaap_treatment(ownership_pct=80)
    assert g["gaap_treatment"] == "consolidated"
    assert g["include_in_book_adj"] is False


def test_cost_like_mtm_above_carrying():
    row = apply_gaap_and_adj(
        {
            "parent_ticker": "TEST",
            "investee_name": "PDD Holdings",
            "investee_ticker": "PDD",
            "ownership_pct": 8,
            "carrying_usd": 50_000_000,
            "market_value_usd": 120_000_000,
            "measurement_alternative": True,
        }
    )
    assert row["gaap_treatment"] == "measurement_alternative_cost"
    assert row["include_in_book_adj"] is True
    assert row["suggested_adj_usd"] == 70_000_000
    assert row["market_vs_carrying"] == "above"
    assert row["lookthrough_mtm_usd"] == 70_000_000


def test_equity_method_lookthrough_not_in_book_adj():
    row = apply_gaap_and_adj(
        {
            "parent_ticker": "TEST",
            "investee_name": "Affiliate",
            "ownership_pct": 30,
            "carrying_usd": 100_000_000,
            "market_value_usd": 180_000_000,
            "influence_disclosed": True,
        }
    )
    assert row["gaap_treatment"] == "equity_method"
    assert row["include_in_book_adj"] is False
    assert row["lookthrough_mtm_usd"] == 80_000_000
    assert row["market_vs_carrying"] == "above"
    assert row["suggested_adj_usd"] == 0.0


def test_consolidated_excluded():
    row = apply_gaap_and_adj(
        {
            "parent_ticker": "TEST",
            "investee_name": "Sub Co",
            "ownership_pct": 100,
            "carrying_usd": 10,
            "market_value_usd": 99,
            "consolidated_disclosed": True,
        }
    )
    assert row["gaap_treatment"] == "consolidated"
    assert row["include_in_book_adj"] is False


def test_unresolved_market_unknown():
    row = apply_gaap_and_adj(
        {
            "parent_ticker": "TEST",
            "investee_name": "Private Co",
            "ownership_pct": 5,
            "carrying_usd": 10_000_000,
            "measurement_alternative": True,
        }
    )
    assert row["market_vs_carrying"] == "unknown"
    assert row["include_in_book_adj"] is False
    assert row["suggested_adj_usd"] is None


def test_rollup_sums_only_book_adj_rows():
    holdings = [
        apply_gaap_and_adj(
            {
                "investee_name": "Cost Stake",
                "ownership_pct": 5,
                "carrying_usd": 50,
                "market_value_usd": 80,
                "measurement_alternative": True,
            }
        ),
        apply_gaap_and_adj(
            {
                "investee_name": "EM Stake",
                "ownership_pct": 25,
                "carrying_usd": 100,
                "market_value_usd": 150,
                "influence_disclosed": True,
            }
        ),
        apply_gaap_and_adj(
            {
                "investee_name": "Unknown Stake",
                "ownership_pct": 3,
                "carrying_usd": 20,
                "measurement_alternative": True,
            }
        ),
    ]
    roll = rollup_holdings(holdings, price=10.0, book_value_per_share=5.0, shares=100.0)
    assert roll["holdings_count"] == 3
    assert roll["holdings_book_adj_usd"] == 30.0  # only cost stake 80-50
    assert roll["holdings_above_count"] == 2
    assert roll["bvps_holdings_adj"] == 5.0 + 30.0 / 100.0
    assert abs(roll["pb_holdings_adj"] - (10.0 / (5.0 + 0.3))) < 1e-9
