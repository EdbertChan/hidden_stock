"""Roll holdings rows into parent-level summary columns."""

from __future__ import annotations

import pandas as pd

PARENT_ROLLUP_COLUMNS = [
    "holdings_count",
    "holdings_book_adj_usd",
    "bvps_holdings_adj",
    "pb_holdings_adj",
    "holdings_lookthrough_mtm_usd",
    "holdings_above_count",
    "holdings_below_count",
    "holdings_ni_impact_count",
]


def rollup_holdings(
    holdings: pd.DataFrame | list[dict],
    *,
    price: float | None = None,
    book_value_per_share: float | None = None,
    shares: float | None = None,
) -> dict:
    """Aggregate holdings for one parent into summary columns."""
    if isinstance(holdings, list):
        df = pd.DataFrame(holdings)
    else:
        df = holdings.copy()

    empty = {c: None for c in PARENT_ROLLUP_COLUMNS}
    empty["holdings_count"] = 0
    empty["holdings_above_count"] = 0
    empty["holdings_below_count"] = 0
    empty["holdings_ni_impact_count"] = 0
    empty["holdings_book_adj_usd"] = 0.0
    empty["holdings_lookthrough_mtm_usd"] = 0.0

    if df.empty:
        return empty

    def _sum_where(mask, col: str) -> float:
        if col not in df.columns:
            return 0.0
        s = pd.to_numeric(df.loc[mask, col], errors="coerce").fillna(0.0)
        return float(s.sum())

    include = df["include_in_book_adj"].fillna(False).astype(bool) if "include_in_book_adj" in df.columns else False
    book_adj = _sum_where(include, "suggested_adj_usd")

    # Lookthrough: equity method / consolidated / non-book-adj rows with MTM.
    if "lookthrough_mtm_usd" in df.columns:
        lt_mask = ~include if isinstance(include, pd.Series) else True
        lookthrough = _sum_where(lt_mask, "lookthrough_mtm_usd")
    else:
        lookthrough = 0.0

    above = int((df.get("market_vs_carrying") == "above").sum()) if "market_vs_carrying" in df.columns else 0
    below = int((df.get("market_vs_carrying") == "below").sum()) if "market_vs_carrying" in df.columns else 0
    ni_impact = (
        int(df["impacts_parent_ni"].fillna(False).astype(bool).sum())
        if "impacts_parent_ni" in df.columns
        else 0
    )

    out = {
        "holdings_count": int(len(df)),
        "holdings_book_adj_usd": book_adj,
        "holdings_lookthrough_mtm_usd": lookthrough,
        "holdings_above_count": above,
        "holdings_below_count": below,
        "holdings_ni_impact_count": ni_impact,
        "bvps_holdings_adj": None,
        "pb_holdings_adj": None,
    }

    bvps = None
    try:
        if book_value_per_share is not None and shares and shares > 0:
            bvps = float(book_value_per_share) + (book_adj / float(shares))
        elif book_value_per_share is not None and book_adj == 0:
            bvps = float(book_value_per_share)
    except (TypeError, ValueError):
        bvps = None

    out["bvps_holdings_adj"] = bvps
    try:
        if price is not None and bvps is not None and bvps > 0:
            out["pb_holdings_adj"] = float(price) / bvps
    except (TypeError, ValueError):
        out["pb_holdings_adj"] = None

    return out
