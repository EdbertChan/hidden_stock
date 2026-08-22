"""GAAP ownership-band treatment for equity holdings.

Bands are defaults; filings can override via influence / consolidation flags.
"""

from __future__ import annotations

from typing import Any


GAAP_TREATMENTS = (
    "fv_ni",
    "measurement_alternative_cost",
    "equity_method",
    "consolidated",
    "other",
    "unclear",
)

OWNERSHIP_BANDS = ("lt_20", "ge_20_lt_50", "ge_50", "unknown")


def ownership_band(ownership_pct: float | None) -> str:
    if ownership_pct is None:
        return "unknown"
    try:
        pct = float(ownership_pct)
    except (TypeError, ValueError):
        return "unknown"
    if pct < 0:
        return "unknown"
    if pct < 20:
        return "lt_20"
    if pct < 50:
        return "ge_20_lt_50"
    return "ge_50"


def assign_gaap_treatment(
    *,
    ownership_pct: float | None = None,
    influence_disclosed: bool | None = None,
    consolidated_disclosed: bool | None = None,
    fair_value_through_earnings: bool | None = None,
    measurement_alternative: bool | None = None,
    filing_gaap_hint: str | None = None,
) -> dict[str, Any]:
    """Map ownership % + disclosure flags → treatment and adjustment policy.

    Returns keys:
      gaap_treatment, ownership_band, impacts_parent_ni, ni_mechanism,
      include_in_book_adj (default before MTM), adj_rationale
    """
    band = ownership_band(ownership_pct)
    hint = (filing_gaap_hint or "").strip().lower() or None

    # Explicit consolidation / control first (incl. >50% ownership).
    if consolidated_disclosed or hint in {"consolidated", "consolidate", "subsidiary"}:
        return {
            "gaap_treatment": "consolidated",
            "ownership_band": band if band != "unknown" else "ge_50",
            "impacts_parent_ni": True,
            "ni_mechanism": "consolidated_subsidiary",
            "include_in_book_adj": False,
            "adj_rationale": "Control/consolidation: investee in consolidated NI; exclude from investment MTM book adj",
        }

    if band == "ge_50":
        return {
            "gaap_treatment": "consolidated",
            "ownership_band": band,
            "impacts_parent_ni": True,
            "ni_mechanism": "consolidated_subsidiary",
            "include_in_book_adj": False,
            "adj_rationale": "Ownership >= 50%: default consolidate; exclude from investment MTM book adj",
        }

    if influence_disclosed or hint in {"equity_method", "equity method", "significant_influence"}:
        return {
            "gaap_treatment": "equity_method",
            "ownership_band": band if band != "unknown" else "ge_20_lt_50",
            "impacts_parent_ni": True,
            "ni_mechanism": "equity_earnings",
            "include_in_book_adj": False,
            "adj_rationale": "Equity method: earnings pick-up in NI; lookthrough MTM informational only",
        }

    if fair_value_through_earnings or hint in {"fv_ni", "fair_value", "fair value", "asc_321"}:
        return {
            "gaap_treatment": "fv_ni",
            "ownership_band": band if band != "unknown" else "lt_20",
            "impacts_parent_ni": True,
            "ni_mechanism": "fv_changes",
            "include_in_book_adj": False,
            "adj_rationale": "Already FV-NI on books; store market for audit, no additional book adj",
        }

    if measurement_alternative or hint in {
        "measurement_alternative",
        "measurement_alternative_cost",
        "cost",
        "cost_method",
    }:
        return {
            "gaap_treatment": "measurement_alternative_cost",
            "ownership_band": band if band != "unknown" else "lt_20",
            "impacts_parent_ni": False,
            "ni_mechanism": "none_until_sale",
            "include_in_book_adj": True,
            "adj_rationale": "Cost / measurement alternative: book adj = market − carrying when market known",
        }

    # Default by ownership band.
    if band == "ge_50":
        return {
            "gaap_treatment": "consolidated",
            "ownership_band": band,
            "impacts_parent_ni": True,
            "ni_mechanism": "consolidated_subsidiary",
            "include_in_book_adj": False,
            "adj_rationale": "Ownership >= 50%: default consolidate; exclude from investment MTM book adj",
        }
    if band == "ge_20_lt_50":
        return {
            "gaap_treatment": "equity_method",
            "ownership_band": band,
            "impacts_parent_ni": True,
            "ni_mechanism": "equity_earnings",
            "include_in_book_adj": False,
            "adj_rationale": "Ownership 20–50%: default equity method; lookthrough MTM informational only",
        }
    if band == "lt_20":
        # Marketable equity securities are typically FV-NI; without a clear
        # cost/measurement-alternative flag we assume FV and do not double-count.
        return {
            "gaap_treatment": "fv_ni",
            "ownership_band": band,
            "impacts_parent_ni": True,
            "ni_mechanism": "fv_changes",
            "include_in_book_adj": False,
            "adj_rationale": "Ownership < 20%: default ASC 321 FV-NI; no extra book adj unless cost alternative disclosed",
        }

    return {
        "gaap_treatment": "unclear",
        "ownership_band": "unknown",
        "impacts_parent_ni": False,
        "ni_mechanism": "unclear",
        "include_in_book_adj": False,
        "adj_rationale": "Insufficient ownership / disclosure to assign GAAP treatment",
    }
