"""Broker SOTP ingest → holdings overlay (no separate broker table)."""

from __future__ import annotations

from pathlib import Path

from hidden_stock.quirks.holdings.broker_sotp import (
    apply_broker_overlay,
    hkd_mn_to_usd,
    load_catalog,
    parse_cmbigm_strategic_investments,
    rows_from_entry,
    stake_history_wide,
)
from hidden_stock.quirks.holdings.composition import (
    PARENT_LISTED_INVESTEES,
    excluded_from_portfolio_mv,
)
from hidden_stock.quirks.holdings.export import portfolio_by_period_frame
import pandas as pd

FIXTURE = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "broker_sotp"
    / "cmbigm_strategic_investments_snippet.txt"
)

FIXTURE_2023 = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "broker_sotp"
    / "cmbigm_strategic_investments_2023_03_snippet.txt"
)


def test_catalog_series_backfills_multiple_vintages():
    """Example catalog documents multi-vintage series; production catalog is empty."""
    import yaml

    example = (
        Path(__file__).resolve().parents[1]
        / "hidden_stock"
        / "quirks"
        / "holdings"
        / "data"
        / "broker_sotp_catalog.example.yaml"
    )
    raw = yaml.safe_load(example.read_text(encoding="utf-8"))
    series = raw.get("series") or []
    assert series
    assert series[0].get("parent_ticker") == "EXAMPLE"
    assert len(series[0].get("reports") or []) >= 1
    # Shipped production catalog must not embed live broker research URLs.
    assert load_catalog() == []


def test_parse_cmbigm_meituan_stake_1_7():
    text = FIXTURE.read_text(encoding="utf-8")
    rows = parse_cmbigm_strategic_investments(text)
    by_name = {r["investee_name"].lower(): r for r in rows}
    assert "meituan" in by_name
    assert by_name["meituan"]["ownership_pct"] == 1.7
    assert by_name["meituan"]["investee_ticker"] == "3690.HK"
    assert by_name["pdd holdings inc"]["ownership_pct"] == 13.8
    assert by_name["pdd holdings inc"]["investee_ticker"] == "PDD"
    assert not any("listed investments" in r["investee_name"].lower() for r in rows)
    assert len(rows) >= 20
    by_t = {r["investee_ticker"]: r for r in rows}
    assert "CH" not in by_t and "KS" not in by_t
    assert by_t["002602.SZ"]["investee_name"] == "Zhejiang Century Huatong Group"
    assert by_t["259960.KS"]["investee_name"] == "Krafton Inc"


def test_parse_numeric_code_exchange_tickers_not_bare_suffix():
    """002602 CH / 259960 KS must not collapse to ticker=CH/KS."""
    text = (
        "Valuation of strategic investments\n"
        "Name Ticker Stake(%) Mkt Cap (US$mn) Value to Tencent (HK$mn)\n"
        "Zhejiang Century Huatong Group 002602 CH 4.1 12345 6789\n"
        "Krafton Inc 259960 KS 2.5 9876 5432\n"
        "PDD Holdings Inc PDD US 13.8 136262 146672\n"
    )
    rows = parse_cmbigm_strategic_investments(text)
    by_t = {r["investee_ticker"]: r for r in rows}
    assert "CH" not in by_t and "KS" not in by_t and "US" not in by_t
    assert by_t["002602.SZ"]["investee_name"] == "Zhejiang Century Huatong Group"
    assert by_t["259960.KS"]["investee_name"] == "Krafton Inc"
    assert by_t["PDD"]["ownership_pct"] == 13.8


def test_rows_from_entry_stamps_broker_source():
    text = FIXTURE.read_text(encoding="utf-8")
    entry = {
        "id": "fixture_cmbigm",
        "series_id": "cmbigm_tencent_strategic",
        "parent_ticker": "TCEHY",
        "broker": "CMBIGM",
        "as_of": "2026-05-12",
        "url": "https://example.com/fixture.pdf",
        "citation": "CMBIGM fixture",
        "parser": "cmbigm_strategic_investments",
    }
    rows = rows_from_entry(entry, text=text)
    meituan = next(r for r in rows if r["investee_name"] == "Meituan")
    assert meituan["value_source"] == "broker_sotp"
    assert "value_source=broker_sotp" in meituan["note"]
    assert meituan["value_to_parent_hkd_mn"] == 9328.0


def test_stake_history_wide_confirms_meituan_over_vintages():
    e_early = {
        "id": "early",
        "series_id": "cmbigm_tencent_strategic",
        "parent_ticker": "TCEHY",
        "broker": "CMBIGM",
        "as_of": "2023-03-21",
        "url": "https://example.com/early.pdf",
        "citation": "CMBIGM early",
        "parser": "cmbigm_strategic_investments",
    }
    e_late = {
        "id": "late",
        "series_id": "cmbigm_tencent_strategic",
        "parent_ticker": "TCEHY",
        "broker": "CMBIGM",
        "as_of": "2026-05-12",
        "url": "https://example.com/late.pdf",
        "citation": "CMBIGM late",
        "parser": "cmbigm_strategic_investments",
    }
    rows = rows_from_entry(e_early, text=FIXTURE_2023.read_text(encoding="utf-8"))
    rows += rows_from_entry(e_late, text=FIXTURE.read_text(encoding="utf-8"))
    wide = stake_history_wide(rows)
    mei = next(r for r in wide if r["investee_ticker"] == "3690.HK")
    assert mei["n_vintages"] == 2
    assert mei["stake_pct_2023-03-21"] == 3.6
    assert mei["stake_pct_2026-05-12"] == 1.7


def test_hkd_mn_to_usd_uses_positive_fx():
    usd, fx = hkd_mn_to_usd(7800.0, "2024-01-01")
    assert fx > 0
    assert usd > 0
    # ~HK$7.8/USD → ~$1e9 for 7800 mn HKD
    assert 0.5e9 < usd < 2e9


def test_overlay_fills_pdd_dollar_keeps_13g_pct():
    history = [
        {
            "parent_ticker": "TCEHY",
            "period_end": "2025-12-31",
            "investee_ticker": PARENT_LISTED_INVESTEES,
            "investee_name": "listed",
            "market_value_usd": 90e9,
            "ownership_pct": None,
            "shares_held": None,
            "action": "hold",
            "note": "value_source=hk_annual_note22",
        },
        {
            "parent_ticker": "TCEHY",
            "period_end": "2026-05-12",
            "investee_ticker": "PDD",
            "investee_name": "PDD Holdings Inc",
            "ownership_pct": 15.6,
            "shares_held": 783468116.0,
            "market_value_usd": None,
            "action": "hold",
            "note": "source=13g form=SC 13D/A",
            "filing_date": "2026-05-12",
            "accession_no": None,
            "filing_url": None,
            "cusip": None,
            "shares_prev": None,
            "shares_delta": None,
            "value_prev": None,
            "value_delta": None,
            "first_seen_period": "2026-05-12",
            "exited_period": None,
        },
    ]
    broker = rows_from_entry(
        {
            "id": "fixture",
            "series_id": "cmbigm_tencent_strategic",
            "parent_ticker": "TCEHY",
            "broker": "CMBIGM",
            "as_of": "2026-05-12",
            "url": "https://example.com/f.pdf",
            "citation": "CMBIGM fixture",
            "parser": "cmbigm_strategic_investments",
        },
        text=FIXTURE.read_text(encoding="utf-8"),
    )
    out = apply_broker_overlay(history, broker, mode="history", parent_ticker="TCEHY")
    pdd = next(r for r in out if r["investee_ticker"] == "PDD" and r["period_end"] == "2026-05-12")
    assert pdd["ownership_pct"] == 15.6  # 13G wins
    assert pdd["shares_held"] == 783468116.0
    assert pdd["market_value_usd"] is not None and float(pdd["market_value_usd"]) > 1e9
    assert "value_source=broker_sotp" in pdd["note"]
    assert "excluded_from_portfolio_mv" in pdd["note"]
    assert excluded_from_portfolio_mv(pdd)


def test_overlay_adds_missing_sehk_name():
    history = [
        {
            "parent_ticker": "TCEHY",
            "period_end": "2026-05-12",
            "investee_ticker": PARENT_LISTED_INVESTEES,
            "market_value_usd": 90e9,
            "note": "value_source=hk_annual_note22",
            "action": "hold",
            "investee_name": "agg",
            "ownership_pct": None,
            "shares_held": None,
            "filing_date": "2026-05-12",
            "accession_no": None,
            "filing_url": None,
            "cusip": None,
            "shares_prev": None,
            "shares_delta": None,
            "value_prev": None,
            "value_delta": None,
            "first_seen_period": "2026-05-12",
            "exited_period": None,
        }
    ]
    broker = rows_from_entry(
        {
            "id": "fixture",
            "series_id": "cmbigm_tencent_strategic",
            "parent_ticker": "TCEHY",
            "broker": "CMBIGM",
            "as_of": "2026-05-12",
            "url": "https://example.com/f.pdf",
            "citation": "CMBIGM fixture",
            "parser": "cmbigm_strategic_investments",
        },
        text=FIXTURE.read_text(encoding="utf-8"),
    )
    out = apply_broker_overlay(history, broker, mode="history", parent_ticker="TCEHY")
    mei = next(r for r in out if r["investee_ticker"] == "3690.HK")
    assert mei["ownership_pct"] == 1.7
    assert mei["shares_held"] is None
    assert mei["market_value_usd"] is not None
    assert "value_source=broker_sotp" in mei["note"]


def test_overlay_excluded_from_portfolio_sum():
    history = [
        {
            "parent_ticker": "TCEHY",
            "period_end": "2026-05-12",
            "investee_ticker": PARENT_LISTED_INVESTEES,
            "investee_name": "agg",
            "market_value_usd": 100e9,
            "action": "hold",
            "note": "value_source=hk_annual_note22",
            "ownership_pct": None,
            "shares_held": None,
            "filing_date": "2026-05-12",
            "accession_no": None,
            "filing_url": None,
            "cusip": None,
            "shares_prev": None,
            "shares_delta": None,
            "value_prev": None,
            "value_delta": None,
            "first_seen_period": "2026-05-12",
            "exited_period": None,
        }
    ]
    broker = [
        {
            "parent_ticker": "TCEHY",
            "investee_name": "PDD Holdings Inc",
            "investee_ticker": "PDD",
            "ownership_pct": 13.8,
            "value_to_parent_hkd_mn": 146672.0,
            "as_of": "2026-05-12",
            "broker": "CMBIGM",
            "report_id": "x",
            "source_url": "https://example.com",
            "citation": "CMBIGM",
        }
    ]
    out = apply_broker_overlay(history, broker, mode="history", parent_ticker="TCEHY")
    port = portfolio_by_period_frame(pd.DataFrame(out))
    # Only aggregate $ should remain (broker child excluded).
    assert list(port["investee_ticker"].astype(str).str.upper()) == [PARENT_LISTED_INVESTEES]
    assert float(port["market_value_usd"].sum()) == 100e9


def test_write_csvs_has_no_broker_sotp_paths(tmp_path):
    from hidden_stock.quirks.holdings.export import write_csvs

    hold = pd.DataFrame(
        [
            {
                "parent_ticker": "TCEHY",
                "investee_ticker": "PDD",
                "investee_name": "PDD",
                "ownership_pct": 13.8,
                "market_value_usd": 1e9,
                "note": "value_source=broker_sotp",
            }
        ]
    )
    hist = pd.DataFrame(
        columns=[
            "parent_ticker",
            "period_end",
            "investee_ticker",
            "investee_name",
            "cusip",
            "action",
            "shares_held",
            "ownership_pct",
            "shares_prev",
            "shares_delta",
            "market_value_usd",
            "value_prev",
            "value_delta",
            "filing_date",
            "accession_no",
            "filing_url",
            "first_seen_period",
            "exited_period",
            "note",
        ]
    )
    paths = write_csvs("TCEHY", hold, hist, tmp_path)
    assert "broker_sotp" not in paths
    assert "broker_sotp_stake_history" not in paths
    assert "hk_composition" not in paths
    assert "returns_chart" not in paths
    assert "realized_chart" not in paths
    assert "realized_by_ticker_chart" not in paths
    assert "returns_by_period" in paths
