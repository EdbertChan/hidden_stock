"""BILI SC 13G/A cessation must not invent shares_proxy=presence=1."""

from hidden_stock.quirks.holdings.history import diff_snapshots
from hidden_stock.quirks.holdings.sec_13g import parse_13g_html, raw_to_position


# Trimmed mirror of tm249567d1_sc13ga.htm cover + Item 4 exit language
# (accession 0001104659-24-038466): 0% / Aggregate Amount 0 / no longer owns.
BILI_EXIT_HTML = """
<html><body>
Bilibili Inc. (Name of Issuer)
CUSIP No. G10970112; 090040106
Aggregate Amount Beneficially Owned by Each Reporting Person
0
10 Check Box if the Aggregate Amount in Row (9) Excludes Certain Shares
Percent of Class Represented by Amount in Row (9)
0%
12 Type of Reporting Person (See Instructions) CO
Item 4. Ownership.
(a) Amount beneficially owned:
As of March 21, 2024, Taobao China Holding Limited no longer owns any
Class Z Ordinary Shares or ADSs of the Issuer. As such, as of the date hereof,
the Reporting Persons are no longer beneficial owners of more than 5% of the
Class Z Ordinary Shares or ADSs.
Item 5. Ownership of Five Percent or Less of a Class.
If this statement is being filed to report the fact that as of the date hereof
the reporting person has ceased to be the beneficial owner of more than five
percent of the class of securities, check the following:
</body></html>
"""


def test_parse_bili_sc13ga_exit_zeros():
    parsed = parse_13g_html(BILI_EXIT_HTML)
    assert "bilibili" in parsed["issuer_name"].lower()
    assert parsed["ownership_pct"] == 0.0
    assert parsed["shares"] == 0.0
    assert parsed.get("exit") is True


def test_raw_to_position_exit_no_presence_proxy():
    parsed = parse_13g_html(BILI_EXIT_HTML)
    pos = raw_to_position(
        parsed,
        parent_ticker="BABA",
        form="SC 13G/A",
        acc="0001104659-24-038466",
        filing_date="2024-03-22",
        cik="0001577552",
    )
    assert pos is not None
    assert pos["investee_ticker"] == "BILI"
    assert pos["shares_held"] == 0.0
    assert pos["ownership_pct"] == 0.0
    assert "13g_exit=1" in str(pos["note"])
    assert "shares_proxy=presence" not in str(pos["note"])


def test_name_only_13g_dropped_not_presence():
    assert (
        raw_to_position(
            {"issuer_name": "Ghost Holdings Ltd", "ticker": None},
            parent_ticker="BABA",
            form="SC 13G",
            acc="ghost",
            filing_date="2024-01-01",
            cik="0001577552",
        )
        is None
    )


def test_qoq_bili_exit_not_hold_at_one():
    """Prior 13F 10M → exit filing must sell/exit to 0, not hold at 1."""
    prior = {
        "investee_name": "BILIBILI INC",
        "investee_ticker": "BILI",
        "shares_held": 10_000_000.0,
        "market_value_usd": 122_000_000.0,
        "_cusip": "090040106",
        "note": "source=sec_api_13f",
        "_source": "13f",
    }
    exit_pos = raw_to_position(
        parse_13g_html(BILI_EXIT_HTML),
        parent_ticker="BABA",
        form="SC 13G/A",
        acc="0001104659-24-038466",
        filing_date="2024-03-22",
        cik="0001577552",
    )
    assert exit_pos is not None
    # Simulate collect_13g pop: exit filing removes BILI from running 13G map,
    # and 13F also dropped BILI → empty current period → exit/sell to 0.
    hist = diff_snapshots(
        "BABA",
        [
            ("2023-12-31", "2024-02-14", "13f-prior", [prior]),
            ("2024-03-31", "2024-03-22", "0001104659-24-038466", []),
        ],
    )
    bili = [r for r in hist if r.get("investee_ticker") == "BILI"]
    assert bili
    last = bili[-1]
    assert last["period_end"] == "2024-03-31"
    assert last["action"] in {"sell", "exit"}
    assert float(last["shares_held"] or 0) == 0.0
    assert float(last.get("shares_held") or 0) != 1.0

    # If exit row were wrongly merged as presence=1, that would be a hold@1 —
    # assert the exit position itself never invents that.
    assert float(exit_pos["shares_held"]) == 0.0


def test_exited_tickers_as_of_suppresses_stale_notes():
    from hidden_stock.quirks.holdings.sec_13g import exited_tickers_as_of

    exited_by_date = {
        "2024-03-22": ["BILI"],
        "2024-06-01": ["BILI", "XPEV"],
    }
    assert exited_tickers_as_of(exited_by_date, "2024-03-21") == set()
    assert exited_tickers_as_of(exited_by_date, "2024-03-22") == {"BILI"}
    assert exited_tickers_as_of(exited_by_date, "2024-12-31") == {"BILI", "XPEV"}
