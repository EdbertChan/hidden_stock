"""HK aggregate → 13G composition overlay."""

from __future__ import annotations

from hidden_stock.quirks.holdings.composition import (
    PARENT_LISTED_INVESTEES,
    hk_composition_frame,
    stamp_hk_composition_parents,
)
from hidden_stock.quirks.holdings.performance import _portfolio_mv_by_period


def _row(**kw):
    base = {
        "parent_ticker": "TCEHY",
        "period_end": "2024-12-31",
        "filing_date": "2024-12-31",
        "accession_no": None,
        "filing_url": None,
        "investee_name": None,
        "investee_ticker": None,
        "cusip": None,
        "shares_held": None,
        "ownership_pct": None,
        "market_value_usd": None,
        "shares_prev": None,
        "shares_delta": None,
        "value_prev": None,
        "value_delta": None,
        "action": "hold",
        "first_seen_period": "2024-12-31",
        "exited_period": None,
        "note": "",
    }
    base.update(kw)
    return base


def test_stamp_composition_parent_and_residual_no_duplicate_ticker():
    history = [
        _row(
            investee_ticker=PARENT_LISTED_INVESTEES,
            investee_name="All listed investees",
            market_value_usd=100e9,
            note="source=hk_annual; value_source=hk_annual_note22; ticker=private_note",
        ),
        _row(
            period_end="2024-06-15",
            filing_date="2024-06-15",
            investee_ticker="BILI",
            investee_name="Bilibili Inc.",
            ownership_pct=8.6,
            shares_held=1000,
            market_value_usd=None,
            note="source=13g form=SC 13G",
            first_seen_period="2024-06-15",
        ),
        _row(
            period_end="2024-11-01",
            filing_date="2024-11-01",
            investee_ticker="JD",
            investee_name="JD.com, Inc.",
            ownership_pct=2.3,
            shares_held=2000,
            market_value_usd=None,
            note="source=13g form=SC 13G",
            first_seen_period="2024-11-01",
        ),
    ]
    out = stamp_hk_composition_parents(history)
    # No duplicate period×ticker; residuals live on history (excluded from portfolio MV)
    keys = [(r["period_end"], r["investee_ticker"]) for r in out]
    assert len(keys) == len(set(keys))
    assert any(str(t).endswith("_RESIDUAL") for _, t in keys)

    bili = next(r for r in out if r["investee_ticker"] == "BILI")
    assert bili["composition_parent"] == PARENT_LISTED_INVESTEES
    assert bili["composition_as_of"] == "2024-12-31"
    assert bili["parent_aggregate_market_value_usd"] == 100e9
    assert "composition_parent=PRIV_HK_LISTED_INVESTEES_FV" in bili["note"]
    assert "not_fv_allocation" in bili["note"]
    assert bili["market_value_usd"] is None

    jd = next(r for r in out if r["investee_ticker"] == "JD")
    assert jd["composition_parent"] == PARENT_LISTED_INVESTEES
    assert jd["market_value_usd"] is None

    mv = _portfolio_mv_by_period(out)
    assert mv["2024-12-31"] == 100e9

    residual = next(
        r for r in out if r["investee_ticker"] == f"{PARENT_LISTED_INVESTEES}_RESIDUAL"
    )
    assert residual["market_value_usd"] == 100e9
    assert "excluded_from_portfolio_mv" in residual["note"]

    frame = hk_composition_frame(out)
    child_tickers = {r["child_ticker"] for r in frame}
    assert "BILI" in child_tickers
    assert "JD" in child_tickers


def test_composition_joins_broker_child_mv_not_parent_aggregate():
    """PDD child $ comes from broker row; parent_aggregate stays Note 22 bucket."""
    history = [
        _row(
            investee_ticker=PARENT_LISTED_INVESTEES,
            investee_name="All listed investees",
            market_value_usd=93e9,
            note="source=hk_annual; value_source=hk_annual_note22; ticker=private_note",
        ),
        _row(
            period_end="2024-06-01",
            filing_date="2024-06-01",
            investee_ticker="PDD",
            investee_name="PDD Holdings Inc",
            ownership_pct=15.6,
            shares_held=1000,
            market_value_usd=None,
            note="source=13g form=SC 13D/A",
            first_seen_period="2024-06-01",
        ),
        _row(
            period_end="2024-05-12",
            filing_date="2024-05-12",
            investee_ticker="PDD",
            investee_name="PDD Holdings Inc",
            ownership_pct=13.8,
            shares_held=None,
            market_value_usd=18.7e9,
            note="value_source=broker_sotp; report_id=x; excluded_from_portfolio_mv",
            first_seen_period="2024-05-12",
        ),
    ]
    stamped = stamp_hk_composition_parents(history)
    pdd_hist = next(
        r
        for r in stamped
        if r["investee_ticker"] == "PDD" and r.get("composition_parent")
    )
    assert pdd_hist["composition_parent"] == PARENT_LISTED_INVESTEES
    assert pdd_hist["parent_aggregate_market_value_usd"] == 93e9
    frame = hk_composition_frame(stamped)
    pdd = next(r for r in frame if r["child_ticker"] == "PDD")
    assert pdd["child_market_value_usd"] == 18.7e9
    assert pdd["child_value_source"] == "broker_sotp"
    assert pdd["parent_aggregate_market_value_usd"] == 93e9
    assert abs(float(pdd["child_market_value_usd"]) - float(pdd["parent_aggregate_market_value_usd"])) > 1e9
    # Join keys required whenever child $ is set (NaN composition_parent class).
    for r in frame:
        if r.get("child_market_value_usd") is None:
            continue
        if str(r.get("child_ticker") or "").endswith("_RESIDUAL"):
            continue
        assert r.get("period_end") and str(r["period_end"]).lower() != "nan"
        assert r.get("composition_parent") and str(r["composition_parent"]).lower() != "nan"
        assert r.get("parent_aggregate_market_value_usd") is not None


def test_live_holdings_from_history_is_open_slice():
    from hidden_stock.quirks.holdings.composition import live_holdings_from_history

    history = [
        _row(
            period_end="2024-06-01",
            investee_ticker="PDD",
            ownership_pct=10.0,
            market_value_usd=None,
            note="source=13g",
        ),
        _row(
            period_end="2025-05-12",
            investee_ticker="PDD",
            ownership_pct=13.8,
            market_value_usd=18e9,
            note="value_source=broker_sotp",
        ),
        _row(
            period_end="2025-06-01",
            investee_ticker="BILI",
            action="exit",
            ownership_pct=0.0,
            note="13g_exit=1",
        ),
        _row(
            period_end="2024-01-01",
            investee_ticker="BILI",
            ownership_pct=5.0,
            note="source=13g",
        ),
    ]
    live = live_holdings_from_history(history)
    tickers = {r["investee_ticker"] for r in live}
    assert "PDD" in tickers
    assert "BILI" not in tickers  # exited
    pdd = next(r for r in live if r["investee_ticker"] == "PDD")
    assert pdd["as_of_date"] == "2025-05-12"
    assert pdd["market_value_usd"] == 18e9
    assert pdd["ownership_pct"] == 13.8


def test_private_note_buckets_under_unlisted_investees():
    history = [
        _row(
            investee_ticker="PRIV_HK_UNLISTED_INVESTEES",
            investee_name="Unlisted investees",
            carrying_usd=50e9,
            market_value_usd=None,
            note="source=hk_annual; ticker=private_note",
        ),
        _row(
            period_end="2024-08-01",
            investee_ticker="PRIV_SOME_STARTUP",
            investee_name="Some Startup",
            ownership_pct=10.0,
            note="source=13g; ticker=private_note",
            first_seen_period="2024-08-01",
        ),
    ]
    out = stamp_hk_composition_parents(history)
    priv = next(r for r in out if r["investee_ticker"] == "PRIV_SOME_STARTUP")
    assert "composition_parent=PRIV_HK_UNLISTED_INVESTEES" in priv["note"]
