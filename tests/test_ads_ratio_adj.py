"""ADS ratio / reverse-split restatements are not disposals."""

from __future__ import annotations

from hidden_stock.quirks.holdings.history import diff_snapshots, reclassify_ads_ratio_adjustments
from hidden_stock.quirks.holdings.performance import build_lots_and_realized


def test_exact_ratio_plus_cusip_change_is_ratio_adj_not_sell():
    """BEST-class: 10M→2M with issuer CUSIP change next period."""
    periods = [
        (
            "2022-03-31",
            "2022-05-16",
            "acc-a",
            [
                {
                    "investee_name": "BEST INC",
                    "investee_ticker": "BEST",
                    "shares_held": 10_000_000.0,
                    "market_value_usd": 6_450_000.0,
                    "_cusip": "08653C106",
                    "note": "source=sec_api_13f",
                }
            ],
        ),
        (
            "2022-06-30",
            "2022-08-15",
            "acc-b",
            [
                {
                    "investee_name": "BEST INC",
                    "investee_ticker": "BEST",
                    "shares_held": 2_000_000.0,
                    "market_value_usd": 2_360_000.0,
                    "_cusip": "08653C106",
                    "note": "source=sec_api_13f",
                }
            ],
        ),
        (
            "2022-09-30",
            "2022-11-14",
            "acc-c",
            [
                {
                    "investee_name": "BEST INC",
                    "investee_ticker": "BEST",
                    "shares_held": 2_000_000.0,
                    "market_value_usd": 1_380_000.0,
                    "_cusip": "08653C502",
                    "note": "source=sec_api_13f",
                }
            ],
        ),
    ]
    hist = diff_snapshots("BABA", periods)
    by = {(r["period_end"], r["investee_ticker"]): r for r in hist}
    mid = by[("2022-06-30", "BEST")]
    assert mid["action"] == "ratio_adj"
    assert "ads_ratio=5:1" in str(mid.get("note") or "")
    assert "restatement_not_disposal" in str(mid.get("note") or "")


def test_same_period_cusip_change_ratio_adj():
    rows = [
        {
            "period_end": "2023-03-31",
            "investee_ticker": "BEST",
            "action": "hold",
            "shares_held": 2_000_000.0,
            "shares_prev": 2_000_000.0,
            "cusip": "08653C502",
        },
        {
            "period_end": "2023-06-30",
            "investee_ticker": "BEST",
            "action": "sell",
            "shares_held": 500_000.0,
            "shares_prev": 2_000_000.0,
            "shares_delta": -1_500_000.0,
            "cusip": "08653C601",
            "note": "source=sec_api_13f",
        },
    ]
    out = reclassify_ads_ratio_adjustments(rows)
    assert out[1]["action"] == "ratio_adj"
    assert "ads_ratio=4:1" in out[1]["note"]


def test_plain_sell_without_cusip_change_stays_sell():
    """80% disposal with unchanged CUSIP is still a sell (not ratio)."""
    periods = [
        (
            "2024-03-31",
            "2024-05-15",
            "a1",
            [
                {
                    "investee_name": "FOO INC",
                    "investee_ticker": "FOO",
                    "shares_held": 1_000_000.0,
                    "market_value_usd": 10_000_000.0,
                    "_cusip": "123456109",
                }
            ],
        ),
        (
            "2024-06-30",
            "2024-08-14",
            "a2",
            [
                {
                    "investee_name": "FOO INC",
                    "investee_ticker": "FOO",
                    "shares_held": 200_000.0,
                    "market_value_usd": 2_000_000.0,
                    "_cusip": "123456109",
                }
            ],
        ),
        (
            "2024-09-30",
            "2024-11-14",
            "a3",
            [
                {
                    "investee_name": "FOO INC",
                    "investee_ticker": "FOO",
                    "shares_held": 200_000.0,
                    "market_value_usd": 2_100_000.0,
                    "_cusip": "123456109",
                }
            ],
        ),
    ]
    hist = diff_snapshots("BABA", periods)
    mid = next(r for r in hist if r["period_end"] == "2024-06-30")
    assert mid["action"] == "sell"


def test_ratio_adj_scales_lots_no_realized():
    rows = [
        {
            "period_end": "2022-03-31",
            "investee_ticker": "BEST",
            "investee_name": "BEST INC",
            "action": "new",
            "shares_held": 10_000_000.0,
            "shares_delta": 10_000_000.0,
            "market_value_usd": 119_700_000.0,  # $11.97
        },
        {
            "period_end": "2022-06-30",
            "investee_ticker": "BEST",
            "investee_name": "BEST INC",
            "action": "ratio_adj",
            "shares_held": 2_000_000.0,
            "shares_prev": 10_000_000.0,
            "shares_delta": -8_000_000.0,
            "market_value_usd": 2_360_000.0,
            "note": "ads_ratio=5:1; restatement_not_disposal",
        },
        {
            "period_end": "2025-03-31",
            "investee_ticker": "BEST",
            "investee_name": "BEST INC",
            "action": "exit",
            "shares_held": 0.0,
            "shares_prev": 2_000_000.0,
            "shares_delta": -2_000_000.0,
            "market_value_usd": 0.0,
            "value_prev": 5_300_000.0,  # exit px $2.65
        },
    ]
    _, realized = build_lots_and_realized(rows)
    avg = [e for e in realized if e.get("cost_method") == "avg"]
    assert len(avg) == 1
    # Per-share cost scales with consolidation (total cost unchanged): 11.97×5.
    assert abs(avg[0]["cost_px"] - 59.85) < 1e-6
    assert abs(avg[0]["shares_sold"] - 2_000_000.0) < 1e-6
    assert not any(e["period_end"] == "2022-06-30" for e in realized)
