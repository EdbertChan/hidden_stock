"""Calendar lookback window for holdings history."""

from hidden_stock.quirks.holdings.lookback import date_on_or_after, lookback_start_date
from hidden_stock.quirks.holdings.history import diff_snapshots


def test_lookback_start_date_subtracts_years():
    assert lookback_start_date(as_of="2026-06-30", lookback_years=5) == "2021-06-30"
    assert lookback_start_date(as_of="2024-02-29", lookback_years=1) == "2023-02-28"


def test_date_on_or_after():
    assert date_on_or_after("2024-12-31", "2021-06-30")
    assert not date_on_or_after("2020-01-01", "2021-06-30")


def test_diff_snapshots_respects_filtered_periods():
    """Year window is applied by dropping old periods before diff — verify QoQ on remainder."""
    periods = [
        (
            "2019-12-31",
            "2020-02-01",
            "acc-old",
            [{"investee_ticker": "GRAB", "shares_held": 1.0, "market_value_usd": 1e9, "_cusip": "G1"}],
        ),
        (
            "2024-12-31",
            "2025-02-01",
            "acc-new",
            [{"investee_ticker": "GRAB", "shares_held": 2.0, "market_value_usd": 2e9, "_cusip": "G1"}],
        ),
        (
            "2025-06-30",
            "2025-08-01",
            "acc-newer",
            [{"investee_ticker": "GRAB", "shares_held": 2.0, "market_value_usd": 2.1e9, "_cusip": "G1"}],
        ),
    ]
    start = lookback_start_date(as_of="2026-06-30", lookback_years=3)  # 2023-06-30
    filtered = [p for p in periods if date_on_or_after(p[0], start)]
    assert [p[0] for p in filtered] == ["2024-12-31", "2025-06-30"]
    hist = diff_snapshots("UBER", filtered)
    ends = sorted({r["period_end"] for r in hist})
    assert "2019-12-31" not in ends
    assert "2024-12-31" in ends


def test_fetch_filer_query_uses_lookback_window(monkeypatch):
    from hidden_stock.quirks.holdings import sec_api_13f as m

    captured: dict = {}

    def fake_post(body, api_key):
        captured["body"] = body
        return {"data": [], "total": {"value": 0}}

    monkeypatch.setenv("SEC_API_KEY", "test-key")
    monkeypatch.setattr(m, "_post_13f", fake_post)
    monkeypatch.setattr(m, "_CACHE", type("C", (), {"get": lambda *a, **k: None, "set": lambda *a, **k: None})())

    m.fetch_filer_13f_filings(
        "0001543151",
        as_of="2026-06-30",
        max_filings=10,
        lookback_start="2021-06-30",
        use_cache=False,
    )
    q = captured["body"]["query"]
    assert "filedAt:[2021-06-30 TO 2026-06-30]" in q
    assert "2000-01-01" not in q
