"""Investments-table parser: dash cells, two-line investees, 10-K year-pair headers."""

from __future__ import annotations

from hidden_stock.quirks.holdings.parse_notes import parse_investments_table

# Real shape of UBER 10-Q 0001543151-22-000024 (Q2 2022): Didi moved from
# Marketable to Non-marketable after delisting — two Didi lines, dash cells.
UBER_10Q_2022Q2 = """
<html><body><table>
<tr><td>As of</td><td>December 31, 2021</td><td>June 30, 2022</td></tr>
<tr><td>Non-marketable equity securities:</td></tr>
<tr><td>Didi</td><td>$</td><td>—</td><td>$</td><td>1,669</td></tr>
<tr><td>Other (1)</td><td></td><td>315</td><td></td><td>298</td></tr>
<tr><td>Marketable equity securities:</td></tr>
<tr><td>Didi</td><td></td><td>2,838</td><td></td><td>—</td></tr>
<tr><td>Grab</td><td></td><td>3,821</td><td></td><td>1,356</td></tr>
<tr><td>Aurora</td><td></td><td>3,388</td><td></td><td>575</td></tr>
</table></body></html>
"""

# Real shape of UBER 10-K 0001543151-26-000015: one month/day, two years.
UBER_10K_2025 = """
<html><body><table>
<tr><td>As of December 31,</td><td>2024</td><td>2025</td></tr>
<tr><td>Classified as investments:</td></tr>
<tr><td>Non-marketable equity securities:</td></tr>
<tr><td>Didi</td><td>$</td><td>2,602</td><td>$</td><td>3,011</td></tr>
<tr><td>Marketable equity securities:</td></tr>
<tr><td>Grab</td><td></td><td>2,529</td><td></td><td>2,674</td></tr>
<tr><td>Aurora</td><td></td><td>2,054</td><td></td><td>1,252</td></tr>
</table></body></html>
"""


def _fv(rows, ticker, as_of):
    by = {(r["investee_ticker"], r["as_of_date"]): r for r in rows}
    return by[(ticker, as_of)]["fair_value_disclosed_usd"]


def test_dash_cell_does_not_split_number_and_sections_sum():
    rows = parse_investments_table(
        UBER_10Q_2022Q2, parent_ticker="UBER", form="10-Q", filing_date="2022-08-04"
    )
    assert _fv(rows, "DIDIY", "2021-12-31") == 2_838_000_000.0  # was 283e6 (split)
    assert _fv(rows, "DIDIY", "2022-06-30") == 1_669_000_000.0  # was 8e6 (split)
    assert _fv(rows, "GRAB", "2022-06-30") == 1_356_000_000.0
    assert _fv(rows, "AUR", "2021-12-31") == 3_388_000_000.0


def test_10k_year_pair_header_parses():
    rows = parse_investments_table(
        UBER_10K_2025, parent_ticker="UBER", form="10-K", filing_date="2026-02-13"
    )
    assert _fv(rows, "DIDIY", "2025-12-31") == 3_011_000_000.0
    assert _fv(rows, "GRAB", "2024-12-31") == 2_529_000_000.0
    assert all(r["_source"] == "10k_investments_table" for r in rows)




# Production hands the parser flattened text (tables gone). A 10-K has other
# two-date headers long before the Investments table; the first one used to
# win and the 10-K yielded zero rows for every Uber annual filing.
UBER_10K_PLAIN = (
    "Consolidated Balance Sheets As of December 31, 2024 December 31, 2025 "
    "Cash and cash equivalents $ 6,000 $ 7,000 Total assets 50,000 60,000 "
    + "x " * 2500
    + "Note 2 – Investments and Fair Value Measurement Investments Our investments "
    "on the consolidated balance sheets consisted of the following as of December 31, "
    "2024 and 2025 (in millions): As of December 31, 2024 2025 Classified as investments: "
    "Non-marketable equity securities: Didi $ 2,602 $ 3,011 Other (2) 608 1,455 "
    "Marketable equity securities: Grab 2,529 2,674 Aurora (3) 2,054 1,252 "
)


def test_10k_plain_text_skips_earlier_headers():
    rows = parse_investments_table(UBER_10K_PLAIN, parent_ticker="UBER", form="10-K", filing_date="2026-02-13")
    assert _fv(rows, "DIDIY", "2025-12-31") == 3_011_000_000.0
    assert _fv(rows, "AUR", "2024-12-31") == 2_054_000_000.0


# FY2019 10-K narrative: "As of December 31, 2017 June 30, 2018 ... Didi, $501 million
# gain". "[\d,]+" matched the bare comma after Didi, so a $501M Didi "FV" row
# appeared at 2018-06-30 and blew up the first Dietz period.
NARRATIVE_NOT_A_ROW = (
    "Balances As of December 31, 2017 June 30, 2018 Restricted cash 1,000 1,200 "
    "During the period we recognized an unrealized gain on our investment in "
    "Didi, $501 million, and other gains of $20 million."
)


def test_narrative_comma_amount_is_not_a_row():
    assert parse_investments_table(NARRATIVE_NOT_A_ROW, parent_ticker="UBER", form="10-K") == []
