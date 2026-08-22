"""SEC filing URL column for positions_qoq verification."""

from hidden_stock.quirks.holdings.edgar_urls import edgar_filing_index_url, stamp_filing_urls
from hidden_stock.quirks.holdings.history import diff_snapshots


def test_edgar_filing_index_url():
    url = edgar_filing_index_url("0001543151", "0001552781-26-000415")
    assert url == (
        "https://www.sec.gov/Archives/edgar/data/1543151/"
        "000155278126000415/0001552781-26-000415-index.htm"
    )
    assert edgar_filing_index_url(None, "0001552781-26-000415") is None
    assert edgar_filing_index_url("0001543151", None) is None
    assert edgar_filing_index_url("", "") is None


def test_diff_snapshots_prefers_as_of_accession_no():
    """Supporting filing = row as_of_accession_no, not period grid accession."""
    periods = [
        (
            "2026-06-30",
            "2026-08-07",
            "0001552781-26-000415",  # period 13F
            [
                {
                    "investee_ticker": "DIDIY",
                    "investee_name": "DiDi",
                    "shares_held": 143_911_749.0,
                    "market_value_usd": 1_900_000_000.0,
                    "as_of_accession_no": "0001543151-26-000032",  # 10-Q
                    "note": "source=10q_investments_table",
                },
                {
                    "investee_ticker": "GRAB",
                    "investee_name": "Grab",
                    "shares_held": 100.0,
                    "market_value_usd": 2e9,
                    "as_of_accession_no": "0001552781-26-000415",
                    "note": "source=sec_api_13f",
                },
            ],
        )
    ]
    hist = diff_snapshots("UBER", periods)
    by = {r["investee_ticker"]: r for r in hist}
    assert by["DIDIY"]["accession_no"] == "0001543151-26-000032"
    assert by["GRAB"]["accession_no"] == "0001552781-26-000415"

    stamped = stamp_filing_urls(hist, cik="0001543151")
    didi = next(r for r in stamped if r["investee_ticker"] == "DIDIY")
    assert didi["filing_url"] is not None
    assert "000154315126000032" in didi["filing_url"]
    assert "0001543151-26-000032-index.htm" in didi["filing_url"]


def test_stamp_filing_urls_missing_accession():
    rows = stamp_filing_urls(
        [{"investee_ticker": "X", "accession_no": None}],
        cik="0001543151",
    )
    assert rows[0]["filing_url"] is None
