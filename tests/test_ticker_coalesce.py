"""Ticker stamp + period×ticker coalesce (13F+13G alias collisions)."""

from __future__ import annotations

from hidden_stock.quirks.holdings.lookback import coalesce_period_ticker, stamp_missing_tickers


def test_stamp_fills_cusip_and_name_aliases():
    rows = stamp_missing_tickers(
        [
            {"investee_ticker": None, "investee_name": "XPeng Inc.", "cusip": None},
            {"investee_ticker": None, "investee_name": "BEST INC", "cusip": "08653C106"},
            {"investee_ticker": None, "investee_name": "Groupon, Inc.", "cusip": "399473107"},
        ]
    )
    by_name = {r["investee_name"]: r["investee_ticker"] for r in rows}
    assert by_name["XPeng Inc."] == "XPEV"
    assert by_name["BEST INC"] == "BEST"
    assert by_name["Groupon, Inc."] == "GRPN"


def test_coalesce_merges_13f_and_13g_same_ticker():
    rows = coalesce_period_ticker(
        [
            {
                "period_end": "2026-06-30",
                "investee_ticker": "XPEV",
                "investee_name": "XPENG INC",
                "action": "hold",
                "market_value_usd": 444e6,
                "note": "source=sec_api_13f",
            },
            {
                "period_end": "2026-06-30",
                "investee_ticker": None,
                "investee_name": "XPeng Inc.",
                "action": "hold",
                "market_value_usd": None,
                "note": "source=13g form=SC 13D/A",
            },
        ]
    )
    assert len(rows) == 1
    assert rows[0]["investee_ticker"] == "XPEV"
    assert rows[0]["market_value_usd"] == 444e6
    assert "13g" in str(rows[0]["note"]).lower()
