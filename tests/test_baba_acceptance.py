"""BABA acceptance: Ant Group 33% equity-method; no invented book adj without market."""

from hidden_stock.quirks.holdings.mtm import apply_gaap_and_adj
from hidden_stock.quirks.holdings.rollup import rollup_holdings


def test_baba_ant_group_equity_method_no_blind_mtm():
    ant = apply_gaap_and_adj(
        {
            "parent_ticker": "BABA",
            "investee_name": "Ant Group Co., Ltd.",
            "ownership_pct": 33.0,
            "influence_disclosed": True,
            "carrying_usd": None,
            "market_value_usd": None,
        }
    )
    assert ant["gaap_treatment"] == "equity_method"
    assert ant["include_in_book_adj"] is False
    assert ant["impacts_parent_ni"] is True
    assert ant["market_vs_carrying"] == "unknown"

    # Listed cost-like stake: hand-checkable book adj.
    listed = apply_gaap_and_adj(
        {
            "parent_ticker": "BABA",
            "investee_name": "XPeng",
            "investee_ticker": "XPEV",
            "ownership_pct": 8.0,
            "carrying_usd": 1_000_000_000,
            "market_value_usd": 1_400_000_000,
            "measurement_alternative": True,
        }
    )
    assert listed["suggested_adj_usd"] == 400_000_000
    assert listed["include_in_book_adj"] is True

    roll = rollup_holdings(
        [ant, listed],
        price=80.0,
        book_value_per_share=50.0,
        shares=2_000_000_000,
    )
    assert roll["holdings_count"] == 2
    assert roll["holdings_book_adj_usd"] == 400_000_000
    assert abs(roll["bvps_holdings_adj"] - 50.2) < 1e-9
    assert abs(roll["pb_holdings_adj"] - (80.0 / 50.2)) < 1e-9
