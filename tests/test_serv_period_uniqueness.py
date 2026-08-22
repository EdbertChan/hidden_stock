"""Regression: 13F drop + continuing 13G must not emit exit+new same ticker."""

from hidden_stock.quirks.holdings.history import (
    assert_unique_period_ticker,
    coalesce_history_by_period_ticker,
    diff_snapshots,
)


def test_13f_drop_13g_continue_same_ticker_one_row():
    """SERV-shaped bug: CUSIP 13F gone, ticker-only 13G remains → one row, not exit+new."""
    p0 = (
        "2026-03-31",
        "2026-05-01",
        "a0",
        [
            {
                "investee_name": "SERVE ROBOTICS",
                "investee_ticker": "SERV",
                "shares_held": 2_070_629.0,
                "market_value_usd": 17_476_109.0,
                "_cusip": "81758H106",
                "note": "source=sec_api_13f",
            }
        ],
    )
    p1 = (
        "2026-06-30",
        "2026-08-01",
        "a1",
        [
            # No 13F SERV — only 13G continuation (same share count)
            {
                "investee_name": "Serve Robotics Inc.",
                "investee_ticker": "SERV",
                "shares_held": 2_070_629.0,
                "market_value_usd": None,
                "_cusip": None,
                "note": "source=13g form=SCHEDULE 13D/A",
                "_source": "13g",
            }
        ],
    )
    hist = diff_snapshots("UBER", [p0, p1])
    june = [r for r in hist if r["period_end"] == "2026-06-30" and r["investee_ticker"] == "SERV"]
    assert len(june) == 1, june
    assert june[0]["action"] != "exit"
    assert june[0]["shares_held"] == 2_070_629.0
    assert_unique_period_ticker(hist)


def test_coalesce_drops_exit_when_survivor_exists():
    rows = [
        {
            "period_end": "2026-06-30",
            "investee_ticker": "SERV",
            "action": "exit",
            "shares_held": 0.0,
            "market_value_usd": 0.0,
            "note": "13f exit",
        },
        {
            "period_end": "2026-06-30",
            "investee_ticker": "SERV",
            "action": "new",
            "shares_held": 2_070_629.0,
            "market_value_usd": None,
            "note": "13g",
        },
    ]
    out = coalesce_history_by_period_ticker(rows)
    assert len(out) == 1
    assert out[0]["action"] == "new"
    assert out[0]["shares_held"] == 2_070_629.0


def test_assert_unique_period_ticker_raises():
    rows = [
        {"period_end": "2026-06-30", "investee_ticker": "SERV", "action": "exit"},
        {"period_end": "2026-06-30", "investee_ticker": "SERV", "action": "new"},
    ]
    try:
        assert_unique_period_ticker(rows, context="test")
        assert False, "expected ValueError"
    except ValueError as e:
        assert "SERV" in str(e)
