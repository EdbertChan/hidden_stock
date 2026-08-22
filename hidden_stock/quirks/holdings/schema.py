"""Schema for stock_data.equity_holdings rows."""

from __future__ import annotations

HOLDINGS_COLUMNS = [
    "parent_ticker",
    "as_of_date",
    "as_of_accession_no",
    "investee_name",
    "investee_ticker",
    "investee_exchange",
    "ownership_pct",
    "shares_held",
    "acquired_date",
    "first_filing_date",
    "first_accession_no",
    "gaap_treatment",
    "influence_disclosed",
    "ownership_band",
    "impacts_parent_ni",
    "ni_mechanism",
    "carrying_usd",
    "fair_value_disclosed_usd",
    "market_price",
    "market_value_usd",
    "price_as_of",
    "price_source",
    "market_vs_carrying",
    "suggested_adj_usd",
    "include_in_book_adj",
    "lookthrough_mtm_usd",
    "already_at_market",
    "adj_rationale",
    "source_quote",
    "confidence",
    "note",
]


def empty_holding_row(**overrides) -> dict:
    row = {c: None for c in HOLDINGS_COLUMNS}
    row["influence_disclosed"] = False
    row["impacts_parent_ni"] = False
    row["include_in_book_adj"] = False
    row["already_at_market"] = False
    row.update(overrides)
    return row
