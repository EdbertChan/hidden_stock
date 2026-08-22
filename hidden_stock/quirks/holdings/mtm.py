"""Mark holdings to market and compute suggested book adjustments."""

from __future__ import annotations

import os
from typing import Any

import requests

from .gaap import assign_gaap_treatment
from .schema import empty_holding_row


def _f(v) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _market_vs_carrying(mtm: float | None) -> str:
    if mtm is None:
        return "unknown"
    if mtm > 0:
        return "above"
    if mtm < 0:
        return "below"
    return "flat"


def apply_gaap_and_adj(raw: dict) -> dict:
    """Fill GAAP fields and suggested_adj from carrying / market amounts."""
    row = empty_holding_row()
    for k, v in raw.items():
        if k in row or k in {
            "consolidated_disclosed",
            "fair_value_through_earnings",
            "measurement_alternative",
            "filing_gaap_hint",
            "force_cost_adj",
        }:
            row[k] = v

    gaap = assign_gaap_treatment(
        ownership_pct=_f(row.get("ownership_pct")),
        influence_disclosed=bool(row.get("influence_disclosed")),
        consolidated_disclosed=bool(raw.get("consolidated_disclosed")),
        fair_value_through_earnings=bool(raw.get("fair_value_through_earnings")),
        measurement_alternative=bool(raw.get("measurement_alternative")),
        filing_gaap_hint=raw.get("filing_gaap_hint") or raw.get("gaap_treatment"),
    )
    row.update(gaap)

    carrying = _f(row.get("carrying_usd"))
    disclosed_fv = _f(row.get("fair_value_disclosed_usd"))
    market_value = _f(row.get("market_value_usd"))
    reference = disclosed_fv if disclosed_fv is not None else market_value

    lookthrough = None
    if carrying is not None and reference is not None:
        lookthrough = reference - carrying
    row["lookthrough_mtm_usd"] = lookthrough
    row["market_vs_carrying"] = _market_vs_carrying(lookthrough)

    treatment = row.get("gaap_treatment")
    row["already_at_market"] = treatment == "fv_ni"

    # Book adj only for cost / measurement-alternative with known market.
    if treatment == "measurement_alternative_cost" and lookthrough is not None:
        row["include_in_book_adj"] = True
        row["suggested_adj_usd"] = lookthrough
    elif treatment == "unclear" and bool(raw.get("force_cost_adj")) and lookthrough is not None:
        row["include_in_book_adj"] = True
        row["suggested_adj_usd"] = lookthrough
        row["adj_rationale"] = (row.get("adj_rationale") or "") + "; forced cost-like adj"
    else:
        # Keep GAAP include_in_book_adj default; clear suggested unless included.
        if not row.get("include_in_book_adj"):
            row["suggested_adj_usd"] = 0.0 if lookthrough is not None else None
        elif lookthrough is not None:
            row["suggested_adj_usd"] = lookthrough
        else:
            row["include_in_book_adj"] = False
            row["suggested_adj_usd"] = None
            row["market_vs_carrying"] = "unknown"

    return row


def fetch_eodhd_price(ticker: str, as_of: str | None = None) -> dict[str, Any]:
    """Latest close on/before as_of (or last available). Returns price metadata."""
    api_key = os.environ.get("EODHD_API_KEY")
    if not api_key or not ticker:
        return {"market_price": None, "price_as_of": None, "price_source": None, "note": "no EODHD key or ticker"}

    symbol = str(ticker).strip().upper()
    # Keep exchange suffix if present (3690.HK, BABA.US); else assume US.
    if "." in symbol:
        url_symbol = symbol
    else:
        url_symbol = f"{symbol}.US"

    params: dict[str, str] = {"api_token": api_key, "fmt": "json", "order": "d"}
    if as_of:
        params["to"] = str(as_of)[:10]
    try:
        resp = requests.get(
            f"https://eodhd.com/api/eod/{url_symbol}",
            params=params,
            timeout=30,
        )
        if resp.status_code == 404:
            return {
                "market_price": None,
                "price_as_of": None,
                "price_source": "eodhd",
                "note": f"EODHD 404 for {url_symbol}",
            }
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list) or not data:
            return {
                "market_price": None,
                "price_as_of": None,
                "price_source": "eodhd",
                "note": f"no EOD bars for {url_symbol}",
            }
        # API with order=d returns newest first when `to` is set.
        bar = data[0]
        px = bar.get("adjusted_close")
        if px is None:
            px = bar.get("close")
        return {
            "market_price": float(px) if px is not None else None,
            "price_as_of": str(bar.get("date") or "")[:10] or None,
            "price_source": "eodhd",
            "note": None,
        }
    except Exception as e:
        return {
            "market_price": None,
            "price_as_of": None,
            "price_source": "eodhd",
            "note": f"EODHD error: {e}",
        }


def fetch_eodhd_market_cap(ticker: str) -> float | None:
    api_key = os.environ.get("EODHD_API_KEY")
    if not api_key or not ticker:
        return None
    symbol = str(ticker).strip().upper()
    url_symbol = symbol if "." in symbol else f"{symbol}.US"
    try:
        resp = requests.get(
            f"https://eodhd.com/api/fundamentals/{url_symbol}",
            params={"api_token": api_key, "filter": "Highlights"},
            timeout=30,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        if isinstance(data, dict):
            mc = data.get("MarketCapitalization")
            return float(mc) if mc is not None else None
    except Exception:
        return None
    return None


def enrich_holding_mtm(holding: dict, as_of: str | None = None) -> dict:
    """Attach EODHD market value when possible, then apply GAAP adj rules."""
    row = dict(holding)
    shares = _f(row.get("shares_held"))
    ownership_pct = _f(row.get("ownership_pct"))
    ticker = row.get("investee_ticker")

    if ticker and row.get("market_value_usd") is None:
        px_info = fetch_eodhd_price(str(ticker), as_of=as_of or row.get("as_of_date"))
        row["market_price"] = px_info.get("market_price")
        row["price_as_of"] = px_info.get("price_as_of")
        row["price_source"] = px_info.get("price_source")
        if px_info.get("note"):
            note = row.get("note") or ""
            row["note"] = f"{note}; {px_info['note']}".strip("; ")

        price = _f(row.get("market_price"))
        if price is not None and shares is not None and shares > 0:
            row["market_value_usd"] = price * shares
            row["price_source"] = (row.get("price_source") or "eodhd") + "|shares*price"
        elif ownership_pct is not None and ownership_pct > 0:
            mcap = fetch_eodhd_market_cap(str(ticker))
            if mcap is not None:
                row["market_value_usd"] = mcap * (ownership_pct / 100.0)
                row["price_source"] = (row.get("price_source") or "eodhd") + "|mcap*ownership_pct"
                if row.get("note"):
                    row["note"] = f"{row['note']}; mcap lookthrough"
                else:
                    row["note"] = "market_value via mcap * ownership_pct"

    return apply_gaap_and_adj(row)
