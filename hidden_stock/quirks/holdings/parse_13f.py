"""Deterministic Form 13F informationTable XML parser."""

from __future__ import annotations

import re
from xml.etree import ElementTree as ET

from .extract import load_investee_aliases, resolve_investee_ticker
from .identity import load_cusip_tickers  # re-export for callers

INFO_TABLE_NS = "http://www.sec.gov/edgar/document/thirteenf/informationtable"

__all__ = ["load_cusip_tickers", "parse_13f_infotable_xml"]


def _local(tag: str) -> str:
    return tag.split("}")[-1] if "}" in tag else tag


def parse_13f_infotable_xml(
    xml_text: str,
    *,
    parent_ticker: str,
    accession_no: str | None = None,
    filing_date: str | None = None,
    aliases: dict[str, str] | None = None,
    cusip_map: dict[str, str] | None = None,
) -> list[dict]:
    """Parse raw 13F informationTable XML into raw holding dicts (pre-GAAP)."""
    if not xml_text or not xml_text.strip():
        return []

    aliases = aliases if aliases is not None else load_investee_aliases()
    cusip_map = cusip_map if cusip_map is not None else load_cusip_tickers()

    # Tolerate default namespace
    text = xml_text.strip()
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []

    rows: list[dict] = []
    for el in root.iter():
        if _local(el.tag) != "infoTable":
            continue
        fields: dict[str, str] = {}
        shares = None
        put_call = None
        for child in el.iter():
            name = _local(child.tag)
            val = (child.text or "").strip()
            if not val:
                continue
            if name == "nameOfIssuer":
                fields["name"] = val
            elif name == "titleOfClass":
                fields["title"] = val
            elif name == "cusip":
                fields["cusip"] = val.upper()
            elif name == "value":
                fields["value"] = val
            elif name == "sshPrnamt":
                shares = val
            elif name == "putCall":
                put_call = val.upper()
            elif name == "investmentDiscretion":
                fields["discretion"] = val

        # Skip options
        if put_call in {"PUT", "CALL"}:
            continue
        name = fields.get("name")
        if not name:
            continue

        try:
            raw_value = float(re.sub(r"[^0-9.]", "", fields.get("value") or "") or 0)
        except ValueError:
            raw_value = 0.0

        try:
            shares_held = float(re.sub(r"[^0-9.]", "", shares or "") or 0) or None
        except ValueError:
            shares_held = None

        # SEC 13F historically reports value in $ thousands; many recent XML
        # tables store whole dollars. Use per-share sanity to choose.
        market_value_usd = None
        if raw_value > 0:
            if shares_held and shares_held > 0:
                per_share = raw_value / shares_held
                if per_share < 0.5:
                    market_value_usd = raw_value * 1000.0  # thousands → USD
                elif per_share <= 5000:
                    market_value_usd = raw_value  # already USD
                else:
                    market_value_usd = raw_value * 1000.0
            else:
                market_value_usd = raw_value * 1000.0

        cusip = fields.get("cusip")
        ticker = None
        if cusip and cusip in cusip_map:
            ticker = cusip_map[cusip]
        if not ticker:
            ticker = resolve_investee_ticker(name, None, aliases)

        market_price = None
        if market_value_usd and shares_held and shares_held > 0:
            market_price = market_value_usd / shares_held

        rows.append(
            {
                "parent_ticker": parent_ticker,
                "investee_name": name,
                "investee_ticker": ticker,
                "shares_held": shares_held,
                "market_value_usd": market_value_usd,
                "market_price": market_price,
                "fair_value_disclosed_usd": market_value_usd,
                "carrying_usd": None,
                "ownership_pct": None,
                "as_of_date": filing_date,
                "as_of_accession_no": accession_no,
                "first_filing_date": filing_date,
                "first_accession_no": accession_no,
                "fair_value_through_earnings": True,
                "filing_gaap_hint": "fv_ni",
                "source_quote": (
                    f"13F {accession_no}: {name} cusip={cusip} "
                    f"shares={shares_held} value={fields.get('value')}"
                ),
                "confidence": "high",
                "note": f"source=13f cusip={cusip} class={fields.get('title')}",
                "price_source": "13f",
                "price_as_of": filing_date,
                "_source": "13f",
                "_cusip": cusip,
            }
        )
    return rows
