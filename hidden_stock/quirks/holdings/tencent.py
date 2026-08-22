"""Tencent Holdings (TCEHY): no US 20-F — use SEC 13G/D + HK annual aggregates."""

from __future__ import annotations

import re
import time
from xml.etree import ElementTree as ET

import requests

from .mtm import enrich_holding_mtm
from .schema import HOLDINGS_COLUMNS, empty_holding_row

TENCENT_CIK = "0001293451"
# Approximate USD/RMB for FY2024 year-end disclosures (HKEX annual report).
RMB_PER_USD = 7.1884

ISSUER_TICKER_HINTS = {
    "kanzhun": "BZ",
    "ke holdings": "BEKE",
    "reddit": "RDDT",
    "cango": "CANG",
    "horizon quantum": None,
    "pinduoduo": "PDD",
    "pdd holdings": "PDD",
    "sea limited": "SE",
    "spotify": "SPOT",
    "meituan": "3690.HK",
    "vipshop": "VIPS",
    "nio": "NIO",
    "bilibili": "BILI",
    "tencent music": "TME",
    "huya": "HUYA",
    "cheetah": "CMCM",
    "global blue": "GB",
    "cheche": "CCG",
}


def _local(tag: str) -> str:
    return tag.split("}")[-1]


def _parse_13g_xml(xml_text: str) -> dict:
    root = ET.fromstring(xml_text)
    out: dict = {}
    for el in root.iter():
        name = _local(el.tag)
        text = (el.text or "").strip()
        if not text:
            continue
        key = name.lower()
        if key in {"issuername", "nameofissuer"} and "issuer_name" not in out:
            out["issuer_name"] = text
        elif key in {"issuercusipnumber", "cusip"} and "cusip" not in out:
            out["cusip"] = text
        elif key in {"classpercent", "percentofclass"} and "ownership_pct" not in out:
            try:
                out["ownership_pct"] = float(re.sub(r"[^0-9.]", "", text.split()[0]))
            except (TypeError, ValueError, IndexError):
                pass
        elif key in {"issuertradingsymbol", "tradingsymbol"} and "ticker" not in out:
            out["ticker"] = text.upper()
    return out


def fetch_tencent_13g_holdings(
    *,
    user_agent: str,
    max_filings: int = 40,
) -> list[dict]:
    """Latest Schedule 13G/D where Tencent is the reporting person → investee rows."""
    session = requests.Session()
    session.headers.update({"User-Agent": user_agent})
    sub = session.get(f"https://data.sec.gov/submissions/CIK{TENCENT_CIK}.json", timeout=30)
    sub.raise_for_status()
    recent = sub.json()["filings"]["recent"]
    forms_ok = {
        "SC 13G",
        "SC 13G/A",
        "SC 13D",
        "SC 13D/A",
        "SCHEDULE 13G",
        "SCHEDULE 13G/A",
        "SCHEDULE 13D",
        "SCHEDULE 13D/A",
    }

    by_issuer: dict[str, dict] = {}
    n = 0
    for i, form in enumerate(recent["form"]):
        if form not in forms_ok:
            continue
        if n >= max_filings:
            break
        n += 1
        acc = recent["accessionNumber"][i]
        primary = recent["primaryDocument"][i]
        # Prefer raw XML over xsl path
        doc = primary.split("/")[-1] if primary else "primary_doc.xml"
        if not doc.endswith(".xml"):
            doc = "primary_doc.xml"
        nodash = acc.replace("-", "")
        url = f"https://www.sec.gov/Archives/edgar/data/{int(TENCENT_CIK)}/{nodash}/{doc}"
        time.sleep(0.12)
        try:
            resp = session.get(url, timeout=20)
            if resp.status_code != 200:
                continue
            parsed = _parse_13g_xml(resp.text)
        except Exception:
            continue
        name = parsed.get("issuer_name")
        if not name:
            continue
        key = name.strip().lower()
        row = {
            "investee_name": name,
            "investee_ticker": parsed.get("ticker"),
            "ownership_pct": parsed.get("ownership_pct"),
            "as_of_date": recent["filingDate"][i],
            "as_of_accession_no": acc,
            "first_filing_date": recent["filingDate"][i],
            "first_accession_no": acc,
            "source_quote": f"{form} {acc}: {name} {parsed.get('ownership_pct')}%",
            "confidence": "medium",
            "note": f"Tencent Schedule 13 filing (CIK {TENCENT_CIK})",
            "filing_gaap_hint": "fv_ni" if (parsed.get("ownership_pct") or 0) < 20 else None,
            "influence_disclosed": bool((parsed.get("ownership_pct") or 0) >= 20),
        }
        # Keep newest filing per issuer name
        prev = by_issuer.get(key)
        if prev is None or str(row["as_of_date"]) >= str(prev["as_of_date"]):
            by_issuer[key] = row

    return list(by_issuer.values())


def resolve_issuer_ticker(name: str | None, ticker: str | None) -> str | None:
    if ticker:
        return str(ticker).strip().upper()
    if not name:
        return None
    key = name.strip().lower()
    for alias, sym in ISSUER_TICKER_HINTS.items():
        if alias in key:
            return sym
    return None


def hk_annual_aggregate_rows(*, as_of: str = "2024-12-31") -> list[dict]:
    """FY2024 HK annual report aggregates (RMB → USD). Named investees not listed."""
    listed_assoc_carrying_rmb = 149_557_000_000  # Note 22 listed associates carrying
    listed_assoc_fv_rmb = 280_088_000_000  # fair value of stakes in listed associates
    unlisted_assoc_carrying_rmb = 140_786_000_000
    listed_investees_fv_rmb = 569_800_000_000  # all listed investees excl subsidiaries
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
        },
    ]


def build_tencent_holdings(*, user_agent: str) -> tuple[list[dict], dict]:
    """Combine HK aggregates + SEC 13G/D named US stakes."""
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
        # Skip EODHD for aggregate synthetic rows (already have FV/carrying).
        if raw.get("price_source") == "hk_annual_report_2024":
            from .mtm import apply_gaap_and_adj

            enriched = apply_gaap_and_adj(base)
        else:
            enriched = enrich_holding_mtm(base, as_of=base.get("as_of_date"))
        rows.append({c: enriched.get(c) for c in HOLDINGS_COLUMNS})
    return rows, meta
