"""Unresolved investee fixture: store row, unknown market, no invented adj."""

from hidden_stock.quirks.holdings.extract import resolve_investee_ticker
from hidden_stock.quirks.holdings.mtm import apply_gaap_and_adj


def test_unresolved_investee_no_invented_ticker_or_adj():
    ticker = resolve_investee_ticker("Totally Private Unlisted Co LLC", None, {})
    assert ticker is None
    row = apply_gaap_and_adj(
        {
            "parent_ticker": "BABA",
            "investee_name": "Totally Private Unlisted Co LLC",
            "investee_ticker": ticker,
            "ownership_pct": 12,
            "carrying_usd": 250_000_000,
            "measurement_alternative": True,
        }
    )
    assert row["investee_ticker"] is None
    assert row["market_value_usd"] is None
    assert row["market_vs_carrying"] == "unknown"
    assert row["suggested_adj_usd"] is None
    assert row["include_in_book_adj"] is False
