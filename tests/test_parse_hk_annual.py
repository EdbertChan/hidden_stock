"""HKEX annual-report parser + identity helpers."""

from __future__ import annotations

from hidden_stock.quirks.holdings.identity import (
    clean_issuer_name,
    holding_key,
    resolve_issuer_ticker,
)
from hidden_stock.quirks.holdings.parse_hk_annual import (
    RMB_PER_USD,
    parse_hk_annual_text,
)

# Truncated FY2024 annual-report text (Note 22 + portfolio FV sentences).
FY2024_AR_FIXTURE = """
Notes to the Consolidated Financial Statements
For the year ended 31 December 2024
22 INVESTMENTS IN ASSOCIATES
2024 2023
RMB'Million RMB'Million
Investments in associates
– Listed entities 149,557 132,776
– Unlisted entities 140,786 120,920
290,343 253,696

The Group's share of the results, as well as the fair value of its stakes in the associates
which are listed entities, are shown in aggregate as follows:
2024
Listed entities (Note) 344,833 195,276 178,310 21,940 (1,102) 20,838 280,088
Unlisted entities 374,482 233,696 72,863 2,746 (104) 2,642

The fair value of our shareholdings5 in listed investee companies (excluding subsidiaries)
amounted to RMB569.8 billion as at 31 December 2024 (31 December 2023: RMB550.7 billion),
and the carrying book value of our shareholdings in unlisted investee companies
(excluding subsidiaries) amounted to RMB335.6 billion as at 31 December 2024
(31 December 2023: RMB337.3 billion).
"""


def test_holding_key_ticker_first():
    assert holding_key({"investee_ticker": "BILI", "cusip": "090040106"}) == "t:BILI"
    assert holding_key({"investee_ticker": None, "cusip": "090040106"}) == "c:090040106"


def test_resolve_via_identity():
    assert resolve_issuer_ticker("Tuya Inc.", None) == "TUYA"
    assert clean_issuer_name("Under the Securities Exchange Act of 1934 MOGU Inc.") == "MOGU Inc."


def test_parse_hk_annual_fy2024_fixture():
    rows = parse_hk_annual_text(
        FY2024_AR_FIXTURE,
        as_of="2024-12-31",
        source_url="https://example.test/ar2024.pdf",
    )
    assert len(rows) == 4
    by_name = {r["investee_name"]: r for r in rows}

    listed_assoc = by_name["Listed associates (aggregate, HK annual report Note 22)"]
    assert listed_assoc["investee_ticker"] == "PRIV_HK_LISTED_ASSOCIATES"
    assert abs(listed_assoc["carrying_usd"] - 149_557_000_000 / RMB_PER_USD) / (
        149_557_000_000 / RMB_PER_USD
    ) < 0.01
    assert abs(listed_assoc["fair_value_disclosed_usd"] - 280_088_000_000 / RMB_PER_USD) / (
        280_088_000_000 / RMB_PER_USD
    ) < 0.01
    assert "value_source=hk_annual_note22" in listed_assoc["note"]
    assert "ticker=private_note" in listed_assoc["note"]

    listed_fv = by_name["All listed investees excl. subsidiaries (aggregate FV)"]
    assert abs(listed_fv["market_value_usd"] - 569.8e9 / RMB_PER_USD) / (
        569.8e9 / RMB_PER_USD
    ) < 0.01
    # Nested associates must not also carry portfolio $
    assert listed_assoc.get("market_value_usd") is None
    assert "nested_in=listed_investees_fv" in listed_assoc["note"]

    unlisted = by_name["Unlisted investees excl. subsidiaries (aggregate carrying)"]
    assert unlisted.get("market_value_usd") is None
    assert abs(unlisted["carrying_usd"] - 335.6e9 / RMB_PER_USD) / (
        335.6e9 / RMB_PER_USD
    ) < 0.01


def test_parse_hk_annual_fail_closed_on_empty():
    assert parse_hk_annual_text("", as_of="2024-12-31", source_url="x") == []
    assert parse_hk_annual_text("no numbers here", as_of="2024-12-31", source_url="x") == []
