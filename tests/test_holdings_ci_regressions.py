"""CI regression contracts for equity holdings fan-out + chart dollars.

These tests are intentionally network-free (mocks only). They lock the bugs we
already shipped once: APRIL false positives, DIDIY invented OTC marks, AUR
13F+13G double-count, self-issuer 13Gs, and strategy silos that skipped sources.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd

from hidden_stock.quirks.holdings.export import chart_data_frame, portfolio_by_period_frame
from hidden_stock.quirks.holdings.history import (
    _merge_period_rows,
    build_holdings_history,
    price_history_rows,
    reconcile_13f_vs_investments,
)
from hidden_stock.quirks.holdings.parse_notes import parse_investment_notes, parse_investments_table
from hidden_stock.quirks.holdings.parents import history_strategy, normalize_parent, uses_hk_aggregates
from hidden_stock.quirks.holdings.runner import merge_raw_holdings
from hidden_stock.quirks.holdings.sec_13g import is_self_issuer, parse_13g_html, raw_to_position


APRIL_TRASH = """
Investment in certain agreements ("e&") April 2023, we entered into a series of agreements.
Investment in Moonshot AI Ltd ("Moonshot"). We acquired approximately 36% equity interest
and invested US $0.8 billion.
"""

DIDI_HTML = """
<html><body>
Didi Global Inc. (Name of Issuer)
23292E108 (CUSIP Number)
(7) Sole Voting Power 143,911,749 (1)
(13) Percent of Class Represented by Amount in Row (11): 11.8% (2)
</body></html>
"""

UBER_INVESTMENTS_TABLE = """
<html><body>
<table>
<tr><td>As of</td><td>December 31, 2025</td><td>June 30, 2026</td></tr>
<tr><td>Non-marketable equity securities:</td></tr>
<tr><td>Didi</td><td>$</td><td>3,011</td><td>$</td><td>1,900</td></tr>
<tr><td>Grab</td><td></td><td>2,674</td><td></td><td>2,020</td></tr>
<tr><td>Aurora</td><td>(1)</td><td>1,252</td><td></td><td>1,763</td></tr>
</table>
</body></html>
"""


def test_ci_no_note_merge_parent_allowlist():
    """Notes are universal — no NOTE_MERGE_PARENTS / us_13f_notes silo."""
    import hidden_stock.quirks.holdings.parents as parents

    assert not hasattr(parents, "NOTE_MERGE_PARENTS")
    assert history_strategy("UBER") == "fanout_13f_13g_notes"
    assert history_strategy("BABA") == "fanout_13f_13g_notes"
    assert history_strategy("TCEHY") == "fanout_13g_hk"
    assert uses_hk_aggregates("TCEHY")
    assert not uses_hk_aggregates("UBER")


def test_ci_april_rejected_moonshot_kept():
    rows = parse_investment_notes(APRIL_TRASH, parent_ticker="UBER", form="10-K")
    names = " ".join(r["investee_name"].lower() for r in rows)
    assert "april" not in names
    assert "moonshot" in names
    moon = next(r for r in rows if "moonshot" in r["investee_name"].lower())
    assert moon["ownership_pct"] == 36.0
    assert moon["carrying_usd"] == 800_000_000


def test_ci_didi_html_and_self_issuer_filter():
    parsed = parse_13g_html(DIDI_HTML)
    pos = raw_to_position(
        parsed,
        parent_ticker="UBER",
        form="SC 13D",
        acc="acc",
        filing_date="2024-05-08",
        cik="0001543151",
    )
    assert pos is not None
    assert pos["investee_ticker"] == "DIDIY"
    assert pos["ownership_pct"] == 11.8
    assert pos["shares_held"] == 143_911_749.0

    assert is_self_issuer(
        {"issuer_name": "Uber Technologies Inc", "ticker": "UBER", "ownership_pct": 5},
        parent_ticker="UBER",
    )
    assert (
        raw_to_position(
            {"issuer_name": "Uber Technologies Inc", "ticker": "UBER", "ownership_pct": 5},
            parent_ticker="UBER",
            form="SC 13G",
            acc="x",
            filing_date="2025-01-01",
            cik="0001543151",
        )
        is None
    )


def test_ci_investments_table_didi_1900m():
    rows = parse_investments_table(
        UBER_INVESTMENTS_TABLE, parent_ticker="UBER", form="10-Q", filing_date="2026-08-06"
    )
    by = {(r["investee_ticker"], r["as_of_date"]): r for r in rows}
    assert by[("DIDIY", "2026-06-30")]["fair_value_disclosed_usd"] == 1_900_000_000.0
    assert by[("DIDIY", "2025-12-31")]["fair_value_disclosed_usd"] == 3_011_000_000.0
    assert by[("GRAB", "2026-06-30")]["fair_value_disclosed_usd"] == 2_020_000_000.0
    assert by[("AUR", "2026-06-30")]["fair_value_disclosed_usd"] == 1_763_000_000.0


def test_ci_merge_13f_wins_dollars_13g_fills_ownership_notes_fill_didi_fv():
    f13 = [
        {
            "investee_name": "Grab",
            "investee_ticker": "GRAB",
            "shares_held": 1e6,
            "market_value_usd": 2e9,
            "_cusip": "G4124C109",
            "_source": "sec_api_13f",
            "note": "source=sec_api_13f",
        },
        {
            "investee_name": "AURORA",
            "investee_ticker": "AUR",
            "shares_held": 100.0,
            "market_value_usd": 1.7e9,
            "_cusip": "051774107",
            "_source": "sec_api_13f",
            "note": "source=sec_api_13f",
        },
    ]
    g13 = [
        {
            "investee_name": "Grab Holdings",
            "investee_ticker": "GRAB",
            "shares_held": 1e6,
            "ownership_pct": 13.5,
            "market_value_usd": None,
            "_cusip": "G4124C109",
            "_source": "13g",
            "note": "source=13g",
        },
        {
            # Different key (no CUSIP) — must NOT double AUR
            "investee_name": "Aurora Innovation, Inc.",
            "investee_ticker": "AUR",
            "shares_held": 10.0,
            "ownership_pct": 10.9,
            "market_value_usd": None,
            "_cusip": None,
            "_source": "13g",
            "note": "source=13g",
        },
        {
            "investee_name": "DiDi Global Inc.",
            "investee_ticker": "DIDIY",
            "shares_held": 143_911_749.0,
            "ownership_pct": 11.8,
            "market_value_usd": None,
            "_cusip": "23292E108",
            "_source": "13g",
            "note": "source=13g",
        },
    ]
    notes = [
        {
            "investee_name": "Didi",
            "investee_ticker": "DIDIY",
            "shares_held": 1.0,
            "market_value_usd": 1_900_000_000.0,
            "ownership_pct": None,
            "as_of_date": "2026-06-30",
            "_source": "10q_investments_table",
            "note": "source=10q_investments_table",
        }
    ]
    live = merge_raw_holdings([f13, g13, notes])
    by = {r["investee_ticker"]: r for r in live}
    assert by["GRAB"]["market_value_usd"] == 2e9
    assert by["GRAB"].get("ownership_pct") == 13.5
    assert by["AUR"]["market_value_usd"] == 1.7e9
    assert by["AUR"].get("ownership_pct") == 10.9
    assert by["DIDIY"]["market_value_usd"] == 1_900_000_000.0
    assert sum(1 for r in live if r.get("investee_ticker") == "AUR") == 1

    merged = _merge_period_rows(f13, g13, notes)
    tickers = [r["investee_ticker"] for r in merged]
    assert tickers.count("AUR") == 1
    assert set(tickers) == {"GRAB", "AUR", "DIDIY"}
    grab = next(r for r in merged if r["investee_ticker"] == "GRAB")
    assert grab["market_value_usd"] == 2e9
    assert grab.get("ownership_pct") == 13.5
    # ownership_pct_provenance_not_preserved (real BABA grade finding, 2026-08-22):
    # this is the REAL production merge path (build_holdings_history calls
    # _merge_period_rows, not merge_raw_holdings) — GRAB is 13F-primary and
    # gets ownership_pct filled from the 13G group via _fill_missing; the note
    # must not claim the % came from 13F alone.
    assert "13g" in str(grab.get("note") or "").lower()
    aur = next(r for r in merged if r["investee_ticker"] == "AUR")
    assert "13g" in str(aur.get("note") or "").lower()
    didi = next(r for r in merged if r["investee_ticker"] == "DIDIY")
    assert didi["market_value_usd"] == 1_900_000_000.0
    assert "investments_table" in str(didi.get("note") or "")
    assert "13g" in str(didi.get("_source") or didi.get("note") or "")


def test_ci_no_eodhd_invent_null_stays_null_table_fv_charts():
    hist = [
        {
            "period_end": "2026-06-30",
            "investee_ticker": "DIDIY",
            "investee_name": "DiDi",
            "shares_held": 143_911_749.0,
            "ownership_pct": 11.8,
            "market_value_usd": None,
            "action": "new",
            "note": "source=13g",
        },
        {
            "period_end": "2026-06-30",
            "investee_ticker": "GRAB",
            "investee_name": "Grab",
            "shares_held": 1e6,
            "market_value_usd": 2.5e9,
            "action": "hold",
            "note": "source=sec_api_13f",
        },
    ]
    with patch(
        "hidden_stock.quirks.holdings.mtm.fetch_eodhd_price",
        return_value={"market_price": 4.5, "price_as_of": "2026-06-30", "price_source": "eodhd"},
    ) as mock_px:
        priced = price_history_rows(hist)
    mock_px.assert_not_called()
    didi = next(r for r in priced if r["investee_ticker"] == "DIDIY")
    assert didi["market_value_usd"] is None

    # Missing $ must not become 0 / must omit from chart
    assert portfolio_by_period_frame(pd.DataFrame(priced)).shape[0] == 1

    # With Investments-table FV, DIDIY charts at $1.9B
    hist2 = [
        {
            "period_end": "2026-06-30",
            "investee_ticker": "DIDIY",
            "investee_name": "DiDi",
            "action": "hold",
            "market_value_usd": 1_900_000_000.0,
            "note": "source=10q_investments_table",
        },
        {
            "period_end": "2026-06-30",
            "investee_ticker": "GRAB",
            "investee_name": "Grab",
            "action": "hold",
            "market_value_usd": 2_020_000_000.0,
            "note": "source=sec_api_13f",
        },
        {
            "period_end": "2026-06-30",
            "investee_ticker": "AUR",
            "investee_name": "Aurora",
            "action": "hold",
            "market_value_usd": 1_763_000_000.0,
            "note": "source=sec_api_13f",
        },
    ]
    port = portfolio_by_period_frame(pd.DataFrame(hist2))
    assert float(port.loc[port.investee_ticker == "DIDIY", "market_value_usd"].iloc[0]) == 1_900_000_000.0
    chart = chart_data_frame(pd.DataFrame(hist2), top_n=7)
    assert "DIDIY" in chart.columns
    assert abs(float(chart.iloc[0]["DIDIY"]) - 1900.0) < 0.1
    assert list(chart.columns)[1] == "GRAB"


def test_ci_reconcile_13f_vs_investments():
    f13 = [
        {"investee_ticker": "GRAB", "market_value_usd": 2_020_000_000.0},
        {"investee_ticker": "AUR", "market_value_usd": 1_763_000_000.0},
    ]
    inv = [
        {"investee_ticker": "GRAB", "fair_value_disclosed_usd": 2_020_000_000.0},
        {"investee_ticker": "AUR", "fair_value_disclosed_usd": 1_763_000_000.0},
        {"investee_ticker": "DIDIY", "fair_value_disclosed_usd": 1_900_000_000.0},
    ]
    assert reconcile_13f_vs_investments(f13, inv) == []

    bad = [
        {"investee_ticker": "GRAB", "market_value_usd": 500_000_000.0},
        {"investee_ticker": "AUR", "market_value_usd": 1_763_000_000.0},
    ]
    mismatches = reconcile_13f_vs_investments(bad, inv)
    assert len(mismatches) == 1
    assert mismatches[0]["ticker"] == "GRAB"


def test_ci_build_holdings_history_fans_out_table_fv_not_eodhd():
    """UBER path overlays 13G + Investments FV; never invents DIDIY via EODHD."""
    f13_periods = [
        (
            "2026-06-30",
            "2026-08-14",
            "a1",
            [
                {
                    "investee_name": "GRAB",
                    "investee_ticker": "GRAB",
                    "shares_held": 1e6,
                    "market_value_usd": 2_020_000_000.0,
                    "_cusip": "G4124C109",
                    "note": "source=sec_api_13f",
                },
                {
                    "investee_name": "AURORA",
                    "investee_ticker": "AUR",
                    "shares_held": 100.0,
                    "market_value_usd": 1_763_000_000.0,
                    "_cusip": "051774107",
                    "note": "source=sec_api_13f",
                },
            ],
        )
    ]
    g13_snaps = [
        (
            "2024-05-08",
            "2024-05-08",
            "didi-acc",
            [
                {
                    "investee_name": "DiDi Global Inc.",
                    "investee_ticker": "DIDIY",
                    "shares_held": 143_911_749.0,
                    "ownership_pct": 11.8,
                    "market_value_usd": None,
                    "_cusip": "23292E108",
                    "note": "source=13g form=SC 13D",
                    "_source": "13g",
                },
                {
                    "investee_name": "Aurora Innovation, Inc.",
                    "investee_ticker": "AUR",
                    "shares_held": 10.0,
                    "ownership_pct": 10.9,
                    "market_value_usd": None,
                    "_cusip": None,
                    "note": "source=13g",
                    "_source": "13g",
                },
            ],
        )
    ]
    note_snaps = [
        (
            "2026-08-06",
            "2026-08-06",
            "10q-acc",
            [
                {
                    "investee_name": "Didi",
                    "investee_ticker": "DIDIY",
                    "shares_held": 1.0,
                    "market_value_usd": 1_900_000_000.0,
                    "as_of_date": "2026-06-30",
                    "note": "source=10q_investments_table",
                    "_source": "10q_investments_table",
                },
                {
                    "investee_name": "Didi",
                    "investee_ticker": "DIDIY",
                    "shares_held": 1.0,
                    "market_value_usd": 3_011_000_000.0,
                    "as_of_date": "2025-12-31",
                    "note": "source=10q_investments_table",
                    "_source": "10q_investments_table",
                },
            ],
        )
    ]
    edgar = MagicMock()
    edgar.get_cik.return_value = "0001543151"
    edgar.user_agent = "ci"

    with (
        patch(
            "hidden_stock.quirks.holdings.history._collect_13f_periods",
            return_value=(f13_periods, {"error": None, "num_periods": 1}),
        ),
        patch(
            "hidden_stock.quirks.holdings.sec_13g.collect_13g_period_snapshots",
            return_value=(g13_snaps, {"num_filings": 1, "num_periods": 1}),
        ),
        patch(
            "hidden_stock.quirks.holdings.history.collect_note_snapshots",
            return_value=(note_snaps, {"num_annual_filings": 1, "num_note_snapshots": 1}),
        ),
        patch(
            "hidden_stock.quirks.holdings.mtm.fetch_eodhd_price",
            return_value={"market_price": 5.0, "price_as_of": "2026-06-30", "price_source": "eodhd"},
        ) as mock_px,
    ):
        hist, meta = build_holdings_history(parent_ticker="UBER", edgar=edgar, max_filings=5)

    mock_px.assert_not_called()
    assert meta["strategy"] == "fanout_13f_13g_notes"
    by = {r["investee_ticker"]: r for r in hist}
    assert by["DIDIY"]["market_value_usd"] == 1_900_000_000.0
    assert by["AUR"]["market_value_usd"] == 1_763_000_000.0
    assert sum(1 for r in hist if r.get("investee_ticker") == "AUR") == 1


def test_ci_enrich_mtm_never_invents_shares_times_price():
    """Categorical: enrich_holding_mtm must not invent OTC stake $."""
    from hidden_stock.quirks.holdings.mtm import enrich_holding_mtm

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
    assert "mcap*ownership_pct" not in str(out.get("price_source") or "")

    # Disclosed FV still maps through
    out2 = enrich_holding_mtm(
        {
            "investee_ticker": "DIDIY",
            "fair_value_disclosed_usd": 1_900_000_000.0,
            "market_value_usd": None,
        }
    )
    assert out2["market_value_usd"] == 1_900_000_000.0


def test_ci_dagster_definitions_load():
    """Equity holdings job stays importable for Dagster deploy/CI."""
    from hidden_stock.definitions import defs
    from hidden_stock.jobs import equity_holdings_job

    assert equity_holdings_job.name == "equity_holdings_job"
    assert defs is not None
    job_names = {j.name for j in (defs.jobs or [])}
    assert "equity_holdings_job" in job_names
    keys = {str(k) for k in defs.resolve_asset_graph().get_all_asset_keys()}
    assert any("equity_holdings" == k.split("/")[-1].strip("\"'") or "equity_holdings" in k for k in keys)
    assert any("equity_holdings_history" in k for k in keys)
    assert any("equity_holdings_export" in k for k in keys)


def test_ci_normalize_parent_stock_agnostic():
    assert normalize_parent("uber") == "UBER"
    assert normalize_parent("alibaba") == "BABA"
    assert normalize_parent("tencent") == "TCEHY"
