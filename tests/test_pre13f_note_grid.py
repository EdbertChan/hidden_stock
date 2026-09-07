"""All-time history for a parent whose 13F filings start late (UBER: 2024-12-31).

Investments-table column dates before the first 13F become period rows; the
parser must not split "2,838 —" into 283 / 8, and must read 10-K
"As of December 31, 2024 2025" headers.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from hidden_stock.quirks.holdings.history import (
    build_holdings_history,
    diff_snapshots,
    note_grid_periods,
)
from hidden_stock.quirks.holdings.parse_notes import find_as_of_dates



def _note(ticker, as_of, usd, acc):
    return {
        "investee_name": ticker,
        "investee_ticker": ticker,
        "shares_held": None,
        "ownership_pct": None,
        "market_value_usd": usd,
        "as_of_date": as_of,
        "as_of_accession_no": acc,
        "_cusip": None,
        "cusip": None,
        "_source": "10q_investments_table",
        "note": f"source=10q_investments_table as_of={as_of} fv_usd={usd:.0f}",
    }


NOTE_SNAPS = [
    ("2021-05-06", "2021-05-06", "q1-21", [_note("DIDIY", "2020-12-31", 6.299e9, "q1-21"), _note("DIDIY", "2021-03-31", 5.876e9, "q1-21"), _note("GRAB", "2021-03-31", 3.591e9, "q1-21")]),
    ("2021-08-05", "2021-08-05", "q2-21", [_note("DIDIY", "2021-06-30", 7.326e9, "q2-21"), _note("GRAB", "2021-06-30", 3.592e9, "q2-21")]),
]
G13_SNAPS = [
    ("2022-02-14", "2022-02-14", "g-22", [
        {"investee_name": "Grab", "investee_ticker": "GRAB", "shares_held": 535_902_982.0, "ownership_pct": None, "market_value_usd": None, "_cusip": "G4124C109", "_source": "13g", "note": "source=13g form=SC 13G"},
    ]),
]


def test_find_as_of_dates_year_pair():
    assert find_as_of_dates("As of December 31, 2024 2025") == ("2024-12-31", "2025-12-31")
    assert find_as_of_dates("As of December 31, 2021 June 30, 2022") == ("2021-12-31", "2022-06-30")


def test_note_grid_periods_precede_first_13f_with_disclosed_dollars_only():
    grid = note_grid_periods(NOTE_SNAPS, before="2024-12-31", lookback_start="2000-01-01", g13_snaps=G13_SNAPS)
    assert [g[0] for g in grid] == ["2020-12-31", "2021-03-31", "2021-06-30"]
    # Stamped with the earliest filing that disclosed the column.
    assert grid[0][1:3] == ("2021-05-06", "q1-21")
    by = {(pe, r["investee_ticker"]): r for pe, _fd, _acc, rows in grid for r in rows}
    assert by[("2021-06-30", "DIDIY")]["market_value_usd"] == 7.326e9
    assert by[("2021-03-31", "GRAB")]["market_value_usd"] == 3.591e9
    assert ("2020-12-31", "GRAB") not in by  # no column value → no invented $
    # 13G filed 2022 must not leak shares into a 2021 period.
    assert by[("2021-06-30", "GRAB")]["shares_held"] is None
    # Window cut still applies.
    assert [g[0] for g in note_grid_periods(NOTE_SNAPS, before="2024-12-31", lookback_start="2021-06-01")] == ["2021-06-30"]


def test_diff_snapshots_keeps_fv_only_appear():
    hist = diff_snapshots("UBER", [("2021-03-31", "2021-05-06", "q1-21", [_note("DIDIY", "2021-03-31", 5.876e9, "q1-21")])])
    assert [(r["investee_ticker"], r["market_value_usd"], r["action"]) for r in hist] == [("DIDIY", 5.876e9, "new")]
    assert hist[0]["first_seen_period"] == "2021-03-31"
    assert hist[0]["shares_held"] is None  # never invented


def test_build_holdings_history_extends_before_first_13f():
    f13 = [("2024-12-31", "2025-02-14", "f-24q4", [
        {"investee_name": "GRAB", "investee_ticker": "GRAB", "shares_held": 535_902_982.0, "market_value_usd": 2.529e9, "_cusip": "G4124C109", "note": "source=sec_api_13f"},
    ])]
    edgar = MagicMock()
    edgar.get_cik.return_value = "0001543151"
    edgar.user_agent = "test"
    with (
        patch("hidden_stock.quirks.holdings.history._collect_13f_periods", return_value=(f13, {"num_filings": 1})),
        patch("hidden_stock.quirks.holdings.sec_13g.collect_13g_period_snapshots", return_value=(G13_SNAPS, {"num_filings": 1, "num_periods": 1, "exited_by_date": {}})),
        patch("hidden_stock.quirks.holdings.history.collect_note_snapshots", return_value=(NOTE_SNAPS, {"num_annual_filings": 2, "num_note_snapshots": 2})),
    ):
        hist, meta = build_holdings_history(parent_ticker="UBER", edgar=edgar, max_filings=5, lookback_years=0)
    periods = sorted({r["period_end"] for r in hist})
    assert periods == ["2020-12-31", "2021-03-31", "2021-06-30", "2024-12-31"]
    assert meta["num_note_grid_periods"] == 3
    didi_21q2 = [r for r in hist if r["period_end"] == "2021-06-30" and r["investee_ticker"] == "DIDIY"][0]
    assert didi_21q2["market_value_usd"] == 7.326e9
    assert "investments_table" in didi_21q2["note"]
    assert didi_21q2["accession_no"] == "q2-21"


def test_build_holdings_history_default_window_does_not_change_when_13f_covers_it():
    f13 = [("2024-12-31", "2025-02-14", "f-24q4", [
        {"investee_name": "GRAB", "investee_ticker": "GRAB", "shares_held": 1.0, "market_value_usd": 2.5e9, "_cusip": "G4124C109", "note": "source=sec_api_13f"},
    ])]
    edgar = MagicMock(); edgar.get_cik.return_value = "0001543151"; edgar.user_agent = "test"
    with (
        patch("hidden_stock.quirks.holdings.history._collect_13f_periods", return_value=(f13, {"num_filings": 1})),
        patch("hidden_stock.quirks.holdings.sec_13g.collect_13g_period_snapshots", return_value=([], {"num_filings": 0, "num_periods": 0, "exited_by_date": {}})),
        patch("hidden_stock.quirks.holdings.history.collect_note_snapshots", return_value=(NOTE_SNAPS, {"num_annual_filings": 2, "num_note_snapshots": 2})),
        patch("hidden_stock.quirks.holdings.inception.collect_prewindow_edge_periods", return_value=([], set(), {})),
    ):
        # Window (2024-09-06) starts after every note column → no note-grid periods.
        hist, meta = build_holdings_history(parent_ticker="UBER", edgar=edgar, max_filings=5, as_of="2026-09-06", lookback_years=2)
    assert meta["num_note_grid_periods"] == 0
    assert sorted({r["period_end"] for r in hist}) == ["2024-12-31"]


def test_list_filings_pages_older_submission_files():
    from hidden_stock.resources.edgar_resource import EdgarResource

    recent = {"form": ["10-Q"], "filingDate": ["2021-05-06"], "accessionNumber": ["r1"], "primaryDocument": ["r1.htm"]}
    older = {"form": ["10-Q", "8-K", "10-Q"], "filingDate": ["2020-05-08", "2020-04-01", "2019-11-05"], "accessionNumber": ["o1", "x", "o2"], "primaryDocument": ["o1.htm", "x.htm", "o2.htm"]}
    top = {"filings": {"recent": recent, "files": [{"name": "CIK1-submissions-001.json"}]}}
    calls: list[str] = []

    def fake_get(url, timeout=15):
        calls.append(url)
        m = MagicMock()
        m.json.return_value = older if url.endswith("submissions-001.json") else top
        return m

    res = EdgarResource(user_agent="t")
    with patch.object(EdgarResource, "_session", return_value=MagicMock(get=fake_get)):
        one = res.list_filings("1", form_types=("10-Q",), limit=1)
        many = res.list_filings("1", form_types=("10-Q",), limit=10)
    assert [f["accession_no"] for f in one] == ["r1"]
    assert [f["accession_no"] for f in many] == ["r1", "o1", "o2"]
    # Older file fetched only when ``recent`` could not satisfy the limit.
    assert sum(u.endswith("submissions-001.json") for u in calls) == 1
