"""ownership_pct domain: (0, 100]. Parsers skip (and log) a bad row, never emit >100."""

from __future__ import annotations

import logging

import pytest

from hidden_stock.quirks.holdings.identity import assert_pct_domain


def test_assert_pct_domain_accepts_open_closed_interval():
    assert assert_pct_domain(0.01, field="ownership_pct", context="t") == 0.01
    assert assert_pct_domain(100, field="ownership_pct", context="t") == 100.0
    assert assert_pct_domain(None, field="ownership_pct", context="t") is None


@pytest.mark.parametrize("bad", [0, -1.0, 100.0001, 612.0, 1952.0, "abc", float("nan")])
def test_assert_pct_domain_rejects_out_of_domain(bad):
    with pytest.raises(ValueError, match="ownership_pct"):
        assert_pct_domain(bad, field="ownership_pct", context="t")


def test_sec_13g_row_with_612_pct_is_skipped_and_logged(caplog):
    from hidden_stock.quirks.holdings.sec_13g import raw_to_position

    parsed = {"issuer_name": "Didi Global Inc.", "cusip": "23292E108", "ownership_pct": 612.0, "shares": 1000.0}
    with caplog.at_level(logging.WARNING):
        pos = raw_to_position(parsed, parent_ticker="UBER", form="SC 13G", acc="a1", filing_date="2024-05-08", cik="1")
    assert pos is None
    assert any("ownership_pct=612.0" in r.getMessage() for r in caplog.records)
    # 0% is a 13G exit, still a row.
    exit_pos = raw_to_position({**parsed, "ownership_pct": 0.0, "shares": 0.0}, parent_ticker="UBER", form="SC 13G", acc="a2", filing_date="2024-08-08", cik="1")
    assert exit_pos is not None and exit_pos["ownership_pct"] == 0.0


def test_broker_row_with_612_pct_is_skipped_and_logged(caplog):
    from hidden_stock.quirks.holdings.broker_sotp import _CMBIGM_INLINE, _row_from_match

    m = _CMBIGM_INLINE.search("Tencent 700 HK 612.0 713,178 19")
    assert m is not None
    with caplog.at_level(logging.WARNING):
        assert _row_from_match(m) is None
    assert any("ownership_pct=612.0" in r.getMessage() for r in caplog.records)
    ok = _row_from_match(_CMBIGM_INLINE.search("Meituan 3690 HK 1.7 100,000 5,000"))
    assert ok is not None and ok["ownership_pct"] == 1.7


def test_parse_notes_pct_over_100_row_is_skipped_and_logged(caplog):
    from hidden_stock.quirks.holdings.parse_notes import parse_investment_notes

    text = (
        'Investment in Moonshot AI Ltd ("Moonshot"). We acquired approximately 612% equity '
        "interest and invested US $0.8 billion."
    )
    with caplog.at_level(logging.WARNING):
        rows = parse_investment_notes(text, parent_ticker="UBER", form="10-K")
    assert not [r for r in rows if "moonshot" in r["investee_name"].lower()]
    assert any("ownership_pct=612.0" in r.getMessage() for r in caplog.records)
