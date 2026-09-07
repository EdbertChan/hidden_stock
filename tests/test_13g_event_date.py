"""Schedule 13D/G rows are dated by the cover *event* date, not the filing date.

Defect: a 13D filed 2024-05-08 for a 2024-04-22 purchase (UBER/SERV) landed in
the 2024-03-31 row because the overlay compared 13G filing_date against the
13F/10-Q filing date (~45 days after quarter end). The period a 13G/D row
belongs to is the first period_end >= its event date.

SERV_13D_HTML is a trimmed mirror of e24231_uberserv-sc13d.htm (accession
0001552781-24-000296).
"""

from __future__ import annotations

from unittest.mock import patch

from hidden_stock.quirks.holdings.sec_13g import (
    collect_13g_period_snapshots,
    exited_tickers_as_of,
    parse_13g_html,
    parse_13g_xml,
    positions_as_of,
    raw_to_position,
)

SERV_13D_HTML = """
<html><body>
SCHEDULE 13D
Serve Robotics Inc. (Name of Issuer)
Common Stock, par value $0.0001 per share (Title of Class of Securities)
81757H105 (CUSIP Number)
April 22, 2024 (Date of Event Which Requires Filing of this Statement)
(7) Sole Voting Power 5,298,833
(11) Aggregate Amount Beneficially Owned by Each Reporting Person 5,298,833
(13) Percent of Class Represented by Amount in Row (11): 14.6%
(14) Type of Reporting Person CO
Item 5. Interest in Securities of the Issuer.
(c) On April 22, 2024, the Reporting Person purchased 1,125,000 shares of Common
Stock in the Issuer's public offering at $4.00 per share.
(d) Not applicable.
</body></html>
"""

SERV_13D_NO_COVER_HTML = SERV_13D_HTML.replace(
    "April 22, 2024 (Date of Event Which Requires Filing of this Statement)", ""
)

SERV_13G_XML = """<?xml version="1.0"?>
<edgarSubmission xmlns="http://www.sec.gov/edgar/schedule13G">
  <coverPageHeader>
    <issuerInfo><issuerName>Serve Robotics Inc.</issuerName><issuerCUSIP>81757H105</issuerCUSIP></issuerInfo>
    <eventDateRequiresFilingThisStatement>12/31/2024</eventDateRequiresFilingThisStatement>
  </coverPageHeader>
  <reportingPersonInfo>
    <aggregateAmountOwned>5,298,833</aggregateAmountOwned>
    <percentOfClass>11.2</percentOfClass>
  </reportingPersonInfo>
</edgarSubmission>
"""

BILI_EXIT_HTML = """
<html><body>
Bilibili Inc. (Name of Issuer)
CUSIP No. G10970112
March 21, 2024 (Date of Event Which Requires Filing of this Statement)
Aggregate Amount Beneficially Owned by Each Reporting Person
0
Percent of Class Represented by Amount in Row (9)
0%
As of March 21, 2024, the Reporting Person no longer owns any Class Z Ordinary Shares.
</body></html>
"""


def _serv_pos(event_date: str, filing_date: str, acc: str = "serv-13d") -> dict:
    return {
        "investee_name": "Serve Robotics Inc.",
        "investee_ticker": "SERV",
        "shares_held": 5_298_833.0,
        "ownership_pct": 14.6,
        "market_value_usd": None,
        "_cusip": "81757H105",
        "cusip": "81757H105",
        "_source": "13g",
        "note": f"source=13g form=SC 13D event_date={event_date}",
        "as_of_date": filing_date,
        "as_of_accession_no": acc,
        "event_date": event_date,
    }


def test_parse_html_cover_event_date_and_item5c():
    parsed = parse_13g_html(SERV_13D_HTML)
    assert parsed["event_date"] == "2024-04-22"
    assert parsed["transaction_dates"] == ["2024-04-22"]
    assert parsed["shares"] == 5_298_833
    no_cover = parse_13g_html(SERV_13D_NO_COVER_HTML)
    assert "event_date" not in no_cover
    assert no_cover["transaction_dates"] == ["2024-04-22"]


def test_parse_xml_event_date_normalised():
    parsed = parse_13g_xml(SERV_13G_XML)
    assert parsed["event_date"] == "2024-12-31"
    assert parsed["shares"] == 5_298_833


def test_raw_to_position_stamps_event_date_and_keeps_filing_date():
    """Cover date wins; Item 5(c) is the fallback; filing_date is the last resort."""
    pos = raw_to_position(
        parse_13g_html(SERV_13D_HTML),
        parent_ticker="UBER",
        form="SC 13D",
        acc="serv-13d",
        filing_date="2024-05-08",
        cik="0001543151",
    )
    assert pos is not None
    assert pos["event_date"] == "2024-04-22"
    assert pos["as_of_date"] == "2024-05-08"
    assert "event_date=2024-04-22" in pos["note"]
    fb = raw_to_position(
        parse_13g_html(SERV_13D_NO_COVER_HTML),
        parent_ticker="UBER",
        form="SC 13D",
        acc="serv-13d",
        filing_date="2024-05-08",
        cik="0001543151",
    )
    assert fb is not None and fb["event_date"] == "2024-04-22"
    plain = raw_to_position(
        {"issuer_name": "Serve Robotics Inc.", "ticker": "SERV", "shares": 10.0, "ownership_pct": 5.0},
        parent_ticker="UBER",
        form="SC 13G",
        acc="x",
        filing_date="2024-05-08",
        cik="0001543151",
    )
    assert plain is not None and plain["event_date"] == "2024-05-08"


def test_positions_as_of_by_event_date():
    snaps = [
        ("2024-04-22", "2024-05-08", "serv-13d", [_serv_pos("2024-04-22", "2024-05-08")]),
    ]
    assert positions_as_of(snaps, "2024-03-31", by="event") == []
    assert positions_as_of(snaps, "2024-06-30", by="event")[0]["investee_ticker"] == "SERV"
    assert positions_as_of(snaps, "2024-05-07", by="filing") == []
    assert positions_as_of(snaps, "2024-05-08", by="filing")[0]["investee_ticker"] == "SERV"


def test_collect_13g_period_snapshots_orders_by_event_date():
    """Running map is built in (event_date, filing_date) order; tuple[0] is the event date."""
    items = [
        ("2024-05-08", "SC 13D", "serv-13d", "e24231_uberserv-sc13d.htm"),
        ("2024-04-10", "SC 13G/A", "bili-exit", "bili.htm"),
    ]
    bodies = {"serv-13d": SERV_13D_HTML, "bili-exit": BILI_EXIT_HTML}
    with (
        patch("hidden_stock.quirks.holdings.sec_13g._list_13g_items", return_value=items),
        patch(
            "hidden_stock.quirks.holdings.sec_13g.fetch_filing_text",
            side_effect=lambda _s, _c, acc, _p: (bodies[acc], "html"),
        ),
        patch("hidden_stock.quirks.holdings.sec_13g.time.sleep", return_value=None),
    ):
        ordered, meta = collect_13g_period_snapshots(
            cik="0001543151", parent_ticker="UBER", user_agent="test", max_filings=10
        )
    assert [(t[0], t[1], t[2]) for t in ordered] == [
        ("2024-03-21", "2024-04-10", "bili-exit"),
        ("2024-04-22", "2024-05-08", "serv-13d"),
    ]
    assert meta["exited_by_date"] == {"2024-03-21": ["BILI"], "2024-04-22": ["BILI"]}
    assert meta["exit_events"]["BILI"] == {
        "accession": "bili-exit",
        "filing_date": "2024-04-10",
        "event_date": "2024-03-21",
    }
    serv = ordered[1][3][0]
    assert serv["investee_ticker"] == "SERV"
    assert serv["event_date"] == "2024-04-22"
    assert serv["as_of_date"] == "2024-05-08"
