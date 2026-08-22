"""Tencent Holdings (TCEHY): HK annual aggregates + SEC 13G/D (shared sec_13g)."""

from __future__ import annotations

from .mtm import apply_gaap_and_adj
from .schema import HOLDINGS_COLUMNS, empty_holding_row
from .sec_13g import (
    ISSUER_TICKER_HINTS,
    collect_13g_period_snapshots,
    fetch_latest_13g_holdings,
    resolve_issuer_ticker,
)

TENCENT_CIK = "0001293451"
RMB_PER_USD = 7.1884

# Re-export for tests that import from tencent
__all__ = [
    "TENCENT_CIK",
    "ISSUER_TICKER_HINTS",
    "resolve_issuer_ticker",
    "fetch_tencent_13g_holdings",
    "collect_13g_period_snapshots",
    "build_13g_reporter_history",
    "hk_annual_aggregate_rows",
    "build_tencent_holdings",
    "_13g_raw_to_position",
]


def _13g_raw_to_position(parsed: dict, *, form: str, acc: str, filing_date: str, cik: str):
    """Test helper: map parsed dict → QoQ position (Tencent parent)."""
    from .sec_13g import raw_to_position

    return raw_to_position(
        parsed,
        parent_ticker="TCEHY",
        form=form,
        acc=acc,
        filing_date=filing_date,
        cik=cik,
    )


def fetch_tencent_13g_holdings(*, user_agent: str, max_filings: int = 40) -> list[dict]:
    rows, _meta = fetch_latest_13g_holdings(
        cik=TENCENT_CIK,
        parent_ticker="TCEHY",
        user_agent=user_agent,
        max_filings=max_filings,
    )
    return rows


def build_13g_reporter_history(
    *,
    parent_ticker: str = "TCEHY",
    user_agent: str,
    max_filings: int = 80,
) -> tuple[list[dict], dict]:
    """QoQ history for Tencent Schedule 13G/D."""
    from .history import diff_snapshots
    from .parents import normalize_parent

    parent = normalize_parent(parent_ticker)
    ordered, meta = collect_13g_period_snapshots(
        cik=TENCENT_CIK,
        parent_ticker=parent,
        user_agent=user_agent,
        max_filings=max_filings,
    )
    meta["parent_ticker"] = parent
    meta["strategy"] = "fanout_13g_hk"
    if meta.get("error"):
        return [], meta
    history = diff_snapshots(parent, ordered)
    return history, meta


def hk_annual_aggregate_rows(*, as_of: str = "2024-12-31") -> list[dict]:
    """FY2024 HK annual report aggregates (RMB → USD). Named investees not listed."""
    listed_assoc_carrying_rmb = 149_557_000_000
    listed_assoc_fv_rmb = 280_088_000_000
    unlisted_assoc_carrying_rmb = 140_786_000_000
    listed_investees_fv_rmb = 569_800_000_000
    unlisted_investees_carrying_rmb = 335_600_000_000

    def usd(rmb: float) -> float:
        return rmb / RMB_PER_USD

    return [
        {
            "investee_name": "Listed associates (aggregate, HK annual report Note 22)",
            "investee_ticker": None,
            "ownership_pct": None,
            "carrying_usd": usd(listed_assoc_carrying_rmb),
            "fair_value_disclosed_usd": usd(listed_assoc_fv_rmb),
            "market_value_usd": usd(listed_assoc_fv_rmb),
            "influence_disclosed": True,
            "filing_gaap_hint": "equity_method",
            "as_of_date": as_of,
            "source_quote": (
                "Investments in associates – Listed entities RMB149,557m; "
                "FV of stakes in listed associates RMB280,088m (FY2024)"
            ),
            "confidence": "high",
            "note": "HKEX annual report aggregate; individual names not disclosed in Note 22",
            "price_source": "hk_annual_report_2024",
            "_source": "hk_annual",
        },
        {
            "investee_name": "Unlisted associates (aggregate, HK annual report Note 22)",
            "investee_ticker": None,
            "ownership_pct": None,
            "carrying_usd": usd(unlisted_assoc_carrying_rmb),
            "influence_disclosed": True,
            "filing_gaap_hint": "equity_method",
            "as_of_date": as_of,
            "source_quote": "Investments in associates – Unlisted entities RMB140,786m (FY2024)",
            "confidence": "high",
            "note": "No observable market; lookthrough MTM unknown",
            "price_source": "hk_annual_report_2024",
            "_source": "hk_annual",
        },
        {
            "investee_name": "All listed investees excl. subsidiaries (aggregate FV)",
            "investee_ticker": None,
            "ownership_pct": None,
            "fair_value_disclosed_usd": usd(listed_investees_fv_rmb),
            "market_value_usd": usd(listed_investees_fv_rmb),
            "fair_value_through_earnings": True,
            "filing_gaap_hint": "fv_ni",
            "as_of_date": as_of,
            "source_quote": "Fair value of shareholdings in listed investee companies RMB569.8bn (31 Dec 2024)",
            "confidence": "high",
            "note": "Includes FVPL/FVOCI + listed associates on attributable basis",
            "price_source": "hk_annual_report_2024",
            "_source": "hk_annual",
        },
        {
            "investee_name": "Unlisted investees excl. subsidiaries (aggregate carrying)",
            "investee_ticker": None,
            "ownership_pct": None,
            "carrying_usd": usd(unlisted_investees_carrying_rmb),
            "measurement_alternative": True,
            "filing_gaap_hint": "cost",
            "as_of_date": as_of,
            "source_quote": "Carrying book value of unlisted investees RMB335.6bn (31 Dec 2024)",
            "confidence": "high",
            "note": "No market; cannot invent MTM adj",
            "price_source": "hk_annual_report_2024",
            "_source": "hk_annual",
        },
    ]


def build_tencent_holdings(*, user_agent: str) -> tuple[list[dict], dict]:
    """Combine HK aggregates + SEC 13G/D named stakes."""
    meta = {
        "parent_ticker": "TCEHY",
        "cik": TENCENT_CIK,
        "error": None,
        "source": "hk_annual_report+sec_13g",
        "filing_date": "2024-12-31",
        "accession_no": None,
        "num_raw": 0,
    }
    raw_rows = hk_annual_aggregate_rows()
    try:
        raw_rows.extend(fetch_tencent_13g_holdings(user_agent=user_agent))
    except Exception as e:
        meta["error"] = f"13G fetch: {e}"

    meta["num_raw"] = len(raw_rows)
    rows: list[dict] = []
    for raw in raw_rows:
        ticker = resolve_issuer_ticker(raw.get("investee_name"), raw.get("investee_ticker"))
        base = empty_holding_row(
            parent_ticker="TCEHY",
            as_of_date=raw.get("as_of_date") or "2024-12-31",
            as_of_accession_no=raw.get("as_of_accession_no"),
            investee_name=raw.get("investee_name"),
            investee_ticker=ticker,
            ownership_pct=raw.get("ownership_pct"),
            carrying_usd=raw.get("carrying_usd"),
            fair_value_disclosed_usd=raw.get("fair_value_disclosed_usd"),
            market_value_usd=raw.get("market_value_usd"),
            influence_disclosed=bool(raw.get("influence_disclosed")),
            source_quote=raw.get("source_quote"),
            confidence=raw.get("confidence"),
            note=raw.get("note"),
            first_filing_date=raw.get("first_filing_date") or raw.get("as_of_date"),
            first_accession_no=raw.get("first_accession_no") or raw.get("as_of_accession_no"),
            price_source=raw.get("price_source"),
        )
        for k in (
            "consolidated_disclosed",
            "fair_value_through_earnings",
            "measurement_alternative",
            "filing_gaap_hint",
        ):
            if k in raw:
                base[k] = raw[k]
        enriched = apply_gaap_and_adj(base)
        rows.append({c: enriched.get(c) for c in HOLDINGS_COLUMNS})
    return rows, meta
