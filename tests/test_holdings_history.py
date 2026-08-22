"""Unit tests for QoQ 13F history diffs."""

from hidden_stock.quirks.holdings.history import classify_action, diff_snapshots


def test_classify_action():
    assert classify_action(0, 100) == "new"
    assert classify_action(100, 0) == "exit"
    assert classify_action(100, 150) == "buy"
    assert classify_action(150, 100) == "sell"
    assert classify_action(100, 100) == "hold"


def test_diff_snapshots_exit_and_sell():
    p1 = (
        "2023-12-31",
        "2024-02-14",
        "acc1",
        [
            {"investee_name": "BILIBILI INC", "investee_ticker": "BILI", "shares_held": 10_000_000, "market_value_usd": 100.0, "_cusip": "090040106"},
            {"investee_name": "PERFECT CORP", "investee_ticker": "PERF", "shares_held": 10_887_904, "market_value_usd": 50.0, "_cusip": "G7006A109"},
            {"investee_name": "XPENG INC", "investee_ticker": "XPEV", "shares_held": 6_650_000, "market_value_usd": 80.0, "_cusip": "98422D105"},
        ],
    )
    p2 = (
        "2024-03-31",
        "2024-05-13",
        "acc2",
        [
            {"investee_name": "PERFECT CORP", "investee_ticker": "PERF", "shares_held": 4_419_823, "market_value_usd": 20.0, "_cusip": "G7006A109"},
            {"investee_name": "XPENG INC", "investee_ticker": "XPEV", "shares_held": 6_650_000, "market_value_usd": 70.0, "_cusip": "98422D105"},
        ],
    )
    hist = diff_snapshots("BABA", [p1, p2])
    by = {(r["period_end"], r["investee_ticker"]): r for r in hist}
    assert by[("2024-03-31", "BILI")]["action"] == "exit"
    assert by[("2024-03-31", "BILI")]["shares_delta"] == -10_000_000
    assert by[("2024-03-31", "PERF")]["action"] == "sell"
    assert by[("2024-03-31", "PERF")]["shares_delta"] == 4_419_823 - 10_887_904
    assert by[("2024-03-31", "XPEV")]["action"] == "hold"
    assert by[("2023-12-31", "BILI")]["action"] == "new"
