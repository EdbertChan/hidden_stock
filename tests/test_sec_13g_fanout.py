"""Tests for Schedule 13D/G HTML/XML parse, self-issuer filter, fan-out history."""

from unittest.mock import MagicMock, patch

from hidden_stock.quirks.holdings.history import build_holdings_history, diff_snapshots, _merge_period_rows
from hidden_stock.quirks.holdings.parse_notes import parse_investment_notes
from hidden_stock.quirks.holdings.sec_13g import (
    is_self_issuer,
    parse_13g_html,
    positions_as_of,
    raw_to_position,
)


DIDI_HTML = """
<html><body>
SCHEDULE 13D
Didi Global Inc. (Name of Issuer)
Class A ordinary shares, par value US$0.00002 per share (Title of Class of Securities)
23292E108 (CUSIP Number)
(7) Sole Voting Power 143,911,749 (1)
(11) Aggregate Amount Beneficially Owned by Each Reporting Person 143,911,749 (1)
(13) Percent of Class Represented by Amount in Row (11): 11.8% (2)
(14) Type of Reporting Person CO
</body></html>
"""

APRIL_PROSE = """
Investment in certain agreements ("e&") April 2023, we entered into a series of agreements.
Investment in Moonshot AI Ltd ("Moonshot"). We acquired approximately 36% equity interest
and invested US $0.8 billion.
"""


def test_parse_didi_html():
    parsed = parse_13g_html(DIDI_HTML)
    assert "didi" in parsed["issuer_name"].lower()
    assert parsed["ownership_pct"] == 11.8
    assert parsed["cusip"] == "23292E108"
    assert parsed.get("shares") == 143_911_749
    pos = raw_to_position(
        {**parsed, "ticker": None},
        parent_ticker="UBER",
        form="SC 13D",
        acc="acc-didi",
        filing_date="2024-05-08",
        cik="0001543151",
    )
    assert pos is not None
    assert pos["investee_ticker"] == "DIDIY"
    assert pos["ownership_pct"] == 11.8


def test_self_issuer_dropped():
    assert is_self_issuer(
        {"issuer_name": "Uber Technologies Inc", "ticker": "UBER", "ownership_pct": 5.0},
        parent_ticker="UBER",
    )
    assert not is_self_issuer(
        {"issuer_name": "DiDi Global Inc.", "ticker": "DIDIY", "ownership_pct": 11.8},
        parent_ticker="UBER",
    )
    dropped = raw_to_position(
        {"issuer_name": "Uber Technologies Inc", "ticker": "UBER", "ownership_pct": 5.0},
        parent_ticker="UBER",
        form="SC 13G",
        acc="x",
        filing_date="2025-01-01",
        cik="0001543151",
    )
    assert dropped is None


def test_harden_notes_rejects_april_keeps_moonshot():
    rows = parse_investment_notes(APRIL_PROSE, parent_ticker="UBER", form="10-K")
    names = {r["investee_name"].lower() for r in rows}
    assert not any("april" in n for n in names)
    assert any("moonshot" in n for n in names)
    moon = next(r for r in rows if "moonshot" in r["investee_name"].lower())
    assert moon["ownership_pct"] == 36.0
    assert moon["carrying_usd"] == 800_000_000


def test_merge_precedence_13f_wins():
    f13 = [
        {
            "investee_name": "AURORA",
            "investee_ticker": "AUR",
            "shares_held": 100.0,
            "market_value_usd": 50.0,
            "_cusip": "051774107",
            "note": "source=sec_api_13f",
        }
    ]
    g13 = [
        {
            "investee_name": "Aurora Innovation, Inc.",
            "investee_ticker": "AUR",
            "shares_held": 10.0,
            "ownership_pct": 10.9,
            "_cusip": "051774107",
            "note": "source=13g",
        }
    ]
    didi = [
        {
            "investee_name": "DiDi Global Inc.",
            "investee_ticker": "DIDIY",
            "shares_held": 11.8,
            "ownership_pct": 11.8,
            "note": "source=13g",
        }
    ]
    merged = _merge_period_rows(f13, g13, didi)
    by = {r["investee_ticker"]: r for r in merged}
    assert by["AUR"]["market_value_usd"] == 50.0
    assert by["AUR"]["shares_held"] == 100.0
    assert "DIDIY" in by


def test_build_holdings_history_overlays_13g():
    f13_periods = [
        (
            "2024-06-30",
            "2024-08-14",
            "a1",
            [
                {
                    "investee_name": "GRAB",
                    "investee_ticker": "GRAB",
                    "shares_held": 1e6,
                    "market_value_usd": 10.0,
                    "_cusip": "G4124C109",
                    "note": "source=sec_api_13f",
                }
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
                    "shares_held": 11.8,
                    "ownership_pct": 11.8,
                    "market_value_usd": None,
                    "_cusip": "23292E108",
                    "note": "source=13g form=SC 13D",
                    "_source": "13g",
                }
            ],
        )
    ]
    edgar = MagicMock()
    edgar.get_cik.return_value = "0001543151"
    edgar.user_agent = "test"

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
            return_value=([], {"num_annual_filings": 0, "num_note_snapshots": 0}),
        ),
    ):
        hist, meta = build_holdings_history(parent_ticker="UBER", edgar=edgar, max_filings=5)

    assert meta["strategy"] == "fanout_13f_13g_notes"
    tickers = {r["investee_ticker"] for r in hist}
    assert "DIDIY" in tickers
    assert "GRAB" in tickers
    assert all("10k_note" not in str(r.get("note") or "") for r in hist)


def test_positions_as_of_forward_fill():
    snaps = [
        ("2024-01-01", "2024-01-01", "a", [{"investee_ticker": "X", "shares_held": 1, "_cusip": "1"}]),
        ("2024-06-01", "2024-06-01", "b", [{"investee_ticker": "Y", "shares_held": 2, "_cusip": "2"}]),
    ]
    assert positions_as_of(snaps, "2023-12-31") == []
    assert positions_as_of(snaps, "2024-03-01")[0]["investee_ticker"] == "X"
    assert positions_as_of(snaps, "2024-07-01")[0]["investee_ticker"] == "Y"
