"""Tests for sec-api Form 13F mapping."""

from unittest.mock import patch

from hidden_stock.quirks.holdings.history import diff_snapshots
from hidden_stock.quirks.holdings.sec_api_13f import (
    cik_for_query,
    collect_filer_13f_periods,
    map_filing_holdings,
    map_holding_to_row,
)


def _sample_filing(*, period: str, filed: str, acc: str, holdings: list[dict]) -> dict:
    return {
        "formType": "13F-HR",
        "periodOfReport": period,
        "filedAt": f"{filed}T12:00:00-05:00",
        "accessionNo": acc,
        "cik": "1543151",
        "holdings": holdings,
    }


_AMD = {
    "nameOfIssuer": "ADVANCED MICRO DEVICES INC",
    "titleOfClass": "COM",
    "cusip": "007903107",
    "ticker": "AMD",
    "value": 1_565_000,
    "shrsOrPrnAmt": {"sshPrnamt": 18527, "sshPrnamtType": "SH"},
}

_PUT = {
    "nameOfIssuer": "SOME CORP",
    "titleOfClass": "COM",
    "cusip": "111111111",
    "ticker": "SOME",
    "putCall": "PUT",
    "value": 1000,
    "shrsOrPrnAmt": {"sshPrnamt": 100, "sshPrnamtType": "SH"},
}


def test_cik_for_query_strips_zeros():
    assert cik_for_query("0001543151") == "1543151"
    assert cik_for_query(1543151) == "1543151"


def test_map_holding_skips_puts_and_maps_ticker():
    row = map_holding_to_row(
        _AMD,
        parent_ticker="UBER",
        accession_no="acc1",
        filing_date="2024-11-12",
        aliases={},
        cusip_map={},
    )
    assert row is not None
    assert row["investee_ticker"] == "AMD"
    assert row["_source"] == "sec_api_13f"
    assert row["shares_held"] == 18527.0
    assert row["market_value_usd"] == 1_565_000.0

    assert (
        map_holding_to_row(
            _PUT,
            parent_ticker="UBER",
            accession_no="acc1",
            filing_date="2024-11-12",
            aliases={},
            cusip_map={},
        )
        is None
    )


def test_map_filing_and_qoq_diff():
    f1 = _sample_filing(period="2024-06-30", filed="2024-08-14", acc="a1", holdings=[_AMD])
    f2 = _sample_filing(
        period="2024-09-30",
        filed="2024-11-12",
        acc="a2",
        holdings=[
            {
                **_AMD,
                "shrsOrPrnAmt": {"sshPrnamt": 20000, "sshPrnamtType": "SH"},
                "value": 2_000_000,
            }
        ],
    )
    r1 = map_filing_holdings(f1, parent_ticker="UBER", aliases={}, cusip_map={})
    r2 = map_filing_holdings(f2, parent_ticker="UBER", aliases={}, cusip_map={})
    hist = diff_snapshots(
        "UBER",
        [
            ("2024-06-30", "2024-08-14", "a1", r1),
            ("2024-09-30", "2024-11-12", "a2", r2),
        ],
    )
    by = {(r["period_end"], r["investee_ticker"]): r for r in hist}
    assert by[("2024-06-30", "AMD")]["action"] == "new"
    assert by[("2024-09-30", "AMD")]["action"] == "buy"
    assert "sec_api_13f" in str(by[("2024-09-30", "AMD")]["note"])


def test_collect_filer_periods_dedupes_by_period():
    filings = [
        _sample_filing(period="2024-09-30", filed="2024-11-01", acc="old", holdings=[_AMD]),
        _sample_filing(period="2024-09-30", filed="2024-11-12", acc="new", holdings=[_AMD]),
        _sample_filing(period="2024-06-30", filed="2024-08-14", acc="mid", holdings=[_AMD]),
    ]
    with patch(
        "hidden_stock.quirks.holdings.sec_api_13f.fetch_filer_13f_filings",
        return_value=filings,
    ):
        ordered, meta = collect_filer_13f_periods(
            parent_ticker="UBER", cik="0001543151", max_filings=10
        )
    assert meta["num_periods"] == 2
    assert ordered[0][0] == "2024-06-30"
    assert ordered[1][2] == "new"
