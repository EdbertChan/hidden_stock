"""Quarter-over-quarter 13F position history (buys / sells / exits)."""

from __future__ import annotations

import re
import time
from typing import Any
from xml.etree import ElementTree as ET

from .parse_13f import load_cusip_tickers, parse_13f_infotable_xml

HISTORY_COLUMNS = [
    "parent_ticker",
    "period_end",
    "filing_date",
    "accession_no",
    "investee_name",
    "investee_ticker",
    "cusip",
    "shares_held",
    "market_value_usd",
    "shares_prev",
    "shares_delta",
    "value_prev",
    "value_delta",
    "action",  # new | buy | sell | exit | hold
    "first_seen_period",
    "exited_period",
    "note",
]


def _local(tag: str) -> str:
    return tag.split("}")[-1] if "}" in tag else tag


def parse_13f_period_end(primary_doc_xml: str) -> str | None:
    """Return periodOfReport as YYYY-MM-DD."""
    if not primary_doc_xml:
        return None
    try:
        root = ET.fromstring(primary_doc_xml.strip())
    except ET.ParseError:
        return None
    for el in root.iter():
        name = _local(el.tag).lower()
        if name in {"periodofreport", "reportcalendarorquarter"} and el.text:
            raw = el.text.strip()
            # MM-DD-YYYY
            m = re.match(r"(\d{1,2})-(\d{1,2})-(\d{4})", raw)
            if m:
                mm, dd, yyyy = m.groups()
                return f"{yyyy}-{int(mm):02d}-{int(dd):02d}"
            m = re.match(r"(\d{4})-(\d{2})-(\d{2})", raw)
            if m:
                return raw[:10]
    return None


def _key(row: dict) -> str:
    # CUSIP is stable across 13F periods; ticker mapping may appear later.
    c = (row.get("_cusip") or row.get("cusip") or "").strip().upper()
    if c:
        return f"c:{c}"
    t = (row.get("investee_ticker") or "").strip().upper()
    if t:
        return f"t:{t}"
    name = re.sub(r"[^a-z0-9]+", "", (row.get("investee_name") or "").lower())
    return f"n:{name}"


def classify_action(shares_prev: float | None, shares: float | None) -> str:
    prev = float(shares_prev or 0)
    cur = float(shares or 0)
    if prev <= 0 and cur > 0:
        return "new"
    if prev > 0 and cur <= 0:
        return "exit"
    if cur > prev:
        return "buy"
    if cur < prev:
        return "sell"
    return "hold"


def diff_snapshots(
    parent_ticker: str,
    ordered_periods: list[tuple[str, str, str, list[dict]]],
) -> list[dict]:
    """ordered_periods: list of (period_end, filing_date, accession, rows) oldest→newest."""
    history: list[dict] = []
    prev_map: dict[str, dict] = {}
    first_seen: dict[str, str] = {}

    for period_end, filing_date, accession, rows in ordered_periods:
        cur_map = {_key(r): r for r in rows if _key(r) not in {"t:", "c:", "n:"}}
        keys = set(prev_map) | set(cur_map)

        for k in sorted(keys):
            prev = prev_map.get(k)
            cur = cur_map.get(k)
            shares_prev = float(prev["shares_held"]) if prev and prev.get("shares_held") is not None else 0.0
            value_prev = float(prev["market_value_usd"]) if prev and prev.get("market_value_usd") is not None else None
            shares = float(cur["shares_held"]) if cur and cur.get("shares_held") is not None else 0.0
            value = float(cur["market_value_usd"]) if cur and cur.get("market_value_usd") is not None else None

            if prev is None and cur is None:
                continue
            if k not in first_seen and shares > 0:
                first_seen[k] = period_end

            action = classify_action(shares_prev if prev else 0.0, shares if cur else 0.0)
            # Skip inventing "exit" on first period with empty prev for names never held
            if prev is None and (cur is None or shares <= 0):
                continue
            # On first snapshot, everything present is "new" (or hold if we want baseline)
            if prev is None and cur is not None:
                action = "new"

            name = (cur or prev or {}).get("investee_name")
            ticker = (cur or prev or {}).get("investee_ticker")
            cusip = (cur or prev or {}).get("_cusip") or (cur or prev or {}).get("cusip")

            exited_period = period_end if action == "exit" else None
            history.append(
                {
                    "parent_ticker": parent_ticker,
                    "period_end": period_end,
                    "filing_date": filing_date,
                    "accession_no": accession,
                    "investee_name": name,
                    "investee_ticker": ticker,
                    "cusip": cusip,
                    "shares_held": shares if action != "exit" else 0.0,
                    "market_value_usd": value if action != "exit" else 0.0,
                    "shares_prev": shares_prev if prev is not None else None,
                    "shares_delta": (0.0 if action == "exit" else shares) - (shares_prev if prev else 0.0),
                    "value_prev": value_prev,
                    "value_delta": (
                        None
                        if value is None and value_prev is None
                        else (0.0 if action == "exit" else (value or 0.0)) - (value_prev or 0.0)
                    ),
                    "action": action,
                    "first_seen_period": first_seen.get(k),
                    "exited_period": exited_period,
                    "note": f"13F QoQ {period_end}",
                }
            )

        prev_map = cur_map

    return history


def build_13f_history(
    *,
    parent_ticker: str,
    edgar,
    as_of: str | None = None,
    max_filings: int = 40,
) -> tuple[list[dict], dict[str, Any]]:
    """Fetch historical 13F-HR snapshots and emit QoQ position rows."""
    meta: dict[str, Any] = {
        "parent_ticker": parent_ticker,
        "num_filings": 0,
        "num_periods": 0,
        "error": None,
    }
    cik = edgar.get_cik(parent_ticker)
    if not cik:
        meta["error"] = "no CIK"
        return [], meta

    filings = edgar.list_filings(
        cik, form_types=("13F-HR",), as_of=as_of, limit=max_filings
    )
    # list_filings is newest-first; process oldest→newest for diffs
    filings = list(reversed(filings))
    meta["num_filings"] = len(filings)

    # One snapshot per period_end (keep latest filing if duplicates)
    by_period: dict[str, tuple[str, str, str, list[dict]]] = {}
    cusip_map = load_cusip_tickers()

    for f in filings:
        xml = edgar.fetch_13f_infotable(cik, f["accession_no"])
        if not xml:
            continue
        # period end from primary_doc.xml
        period_end = None
        try:
            # primary often under xsl path in submissions; try sibling
            docs = edgar.list_filing_documents(cik, f["accession_no"])
            primary_name = None
            for d in docs:
                n = d["name"].lower()
                if n == "primary_doc.xml" or (n.endswith("primary_doc.xml") and "xsl" not in n):
                    primary_name = d["name"]
                    break
            if primary_name:
                primary_xml = edgar.fetch_filing_document(cik, f["accession_no"], primary_name)
                period_end = parse_13f_period_end(primary_xml)
        except Exception:
            period_end = None
        if not period_end:
            # Fall back to filing date quarter guess — last day of prior month-ish
            period_end = f["filing_date"]

        rows = parse_13f_infotable_xml(
            xml,
            parent_ticker=parent_ticker,
            accession_no=f["accession_no"],
            filing_date=f["filing_date"],
            cusip_map=cusip_map,
        )
        by_period[period_end] = (period_end, f["filing_date"], f["accession_no"], rows)
        time.sleep(0.05)

    ordered = [by_period[k] for k in sorted(by_period.keys())]
    meta["num_periods"] = len(ordered)
    history = diff_snapshots(parent_ticker, ordered)
    return history, meta
