"""Categorical: never invent stake $ via EODHD shares×price / mcap×%."""

from unittest.mock import patch

from hidden_stock.quirks.holdings.mtm import enrich_holding_mtm


def test_enrich_holding_mtm_never_calls_eodhd_invent():
    with patch(
        "hidden_stock.quirks.holdings.mtm.fetch_eodhd_price",
        return_value={"market_price": 4.5, "price_as_of": "2026-06-30", "price_source": "eodhd"},
    ) as mock_px:
        out = enrich_holding_mtm(
            {
                "investee_ticker": "DIDIY",
                "shares_held": 143_911_749.0,
                "ownership_pct": 11.8,
                "market_value_usd": None,
                "note": "source=13g",
            }
        )
    mock_px.assert_not_called()
    assert out["market_value_usd"] is None
    assert "shares*price" not in str(out.get("price_source") or "")


def test_enrich_holding_mtm_maps_disclosed_fv():
    out = enrich_holding_mtm(
        {
            "investee_ticker": "DIDIY",
            "fair_value_disclosed_usd": 1_900_000_000.0,
            "market_value_usd": None,
        }
    )
    assert out["market_value_usd"] == 1_900_000_000.0
