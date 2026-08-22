"""Deterministic 13F + 20-F note parsers (no LLM / no network)."""

from pathlib import Path

from hidden_stock.quirks.holdings.parse_13f import parse_13f_infotable_xml
from hidden_stock.quirks.holdings.parse_notes import parse_investment_notes
from hidden_stock.quirks.holdings.runner import merge_raw_holdings

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_parse_13f_xpeng_weibo():
    xml = (FIXTURES / "baba_13f_infotable.xml").read_text()
    rows = parse_13f_infotable_xml(
        xml,
        parent_ticker="BABA",
        accession_no="0001104659-26-062448",
        filing_date="2026-05-15",
    )
    by_ticker = {r["investee_ticker"]: r for r in rows}
    assert "XPEV" in by_ticker
    assert "WB" in by_ticker
    assert by_ticker["XPEV"]["shares_held"] == 33_537_232
    assert by_ticker["XPEV"]["market_value_usd"] == 573_822_040
    assert by_ticker["WB"]["shares_held"] == 9_000_000
    assert by_ticker["XPEV"]["_source"] == "13f"


def test_parse_notes_moonshot_and_ant():
    text = (FIXTURES / "baba_moonshot_ant_note.txt").read_text()
    rows = parse_investment_notes(
        text,
        parent_ticker="BABA",
        form="20-F",
        accession_no="0000950170-25-090161",
        filing_date="2025-06-26",
    )
    moon = next(r for r in rows if "moonshot" in r["investee_name"].lower())
    assert moon["ownership_pct"] == 36.0
    assert moon["carrying_usd"] == 800_000_000
    assert moon["_source"] == "20f_note"
    ant = next(r for r in rows if "ant" in r["investee_name"].lower())
    assert ant["ownership_pct"] == 33.0
    assert ant["influence_disclosed"] is True


def test_merge_prefers_13f_market_keeps_note_private():
    xml = (FIXTURES / "baba_13f_infotable.xml").read_text()
    f13 = parse_13f_infotable_xml(xml, parent_ticker="BABA", accession_no="a", filing_date="2026-05-15")
    notes = parse_investment_notes(
        (FIXTURES / "baba_moonshot_ant_note.txt").read_text(),
        parent_ticker="BABA",
        form="20-F",
        accession_no="b",
        filing_date="2025-06-26",
    )
    notes.append(
        {
            "parent_ticker": "BABA",
            "investee_name": "XPENG INC",
            "investee_ticker": "XPEV",
            "ownership_pct": 8.0,
            "_source": "20f_note",
        }
    )
    merged = merge_raw_holdings([f13, notes])
    tickers = {r.get("investee_ticker") for r in merged}
    names = " ".join((r.get("investee_name") or "").lower() for r in merged)
    assert "XPEV" in tickers
    assert "WB" in tickers
    assert "moonshot" in names
    assert "ant" in names
    xpev = next(r for r in merged if r.get("investee_ticker") == "XPEV")
    assert xpev["shares_held"] == 33_537_232
    assert xpev.get("ownership_pct") == 8.0
