"""Form 13F holdings via sec-api.io (structured JSON, filer CIK)."""

from __future__ import annotations

import os
import re
import time
from typing import Any

import diskcache
import requests

from .extract import load_investee_aliases, resolve_investee_ticker
from .parse_13f import load_cusip_tickers

_SEC_13F_URL = "https://api.sec-api.io/form-13f/holdings"
_CACHE = diskcache.Cache(os.path.expanduser("~/.cache/hidden_stock/sec_api_13f_filer"))
_CACHE_TTL_SECONDS = 6 * 60 * 60


def cik_for_query(cik: str | int) -> str:
    """Strip leading zeros for sec-api Lucene `cik:` queries."""
    s = re.sub(r"\D", "", str(cik or ""))
    return s.lstrip("0") or "0"


def _market_value_usd(raw_value: float) -> float | None:
    """sec-api Form 13F `value` is position size in USD (not thousands)."""
    if raw_value <= 0:
        return None
    return float(raw_value)


def map_holding_to_row(
    h: dict,
    *,
    parent_ticker: str,
    accession_no: str | None,
    filing_date: str | None,
    aliases: dict[str, str] | None = None,
    cusip_map: dict[str, str] | None = None,
) -> dict | None:
    """Map one sec-api holdings[] item to our raw holding dict, or None to skip."""
    if not isinstance(h, dict):
        return None

    title = (h.get("titleOfClass") or "").upper()
    put_call = (h.get("putCall") or "").upper()
    if put_call in {"PUT", "CALL"} or "PUT" in title or "CALL" in title:
        return None

    sh = h.get("shrsOrPrnAmt") or {}
    typ = (sh.get("sshPrnamtType") or "SH").upper()
    if typ and typ != "SH":
        return None

    name = (h.get("nameOfIssuer") or "").strip()
    if not name:
        return None

    aliases = aliases if aliases is not None else load_investee_aliases()
    cusip_map = cusip_map if cusip_map is not None else load_cusip_tickers()

    try:
        shares_held = float(sh.get("sshPrnamt") or 0) or None
    except (TypeError, ValueError):
        shares_held = None

    try:
        raw_value = float(h.get("value") or 0)
    except (TypeError, ValueError):
        raw_value = 0.0

    market_value_usd = _market_value_usd(raw_value)
    cusip = (h.get("cusip") or "").strip().upper() or None

    ticker = (h.get("ticker") or "").strip().upper() or None
    if not ticker and cusip and cusip in cusip_map:
        ticker = cusip_map[cusip]
    if not ticker:
        ticker = resolve_investee_ticker(name, None, aliases)

    market_price = None
    if market_value_usd and shares_held and shares_held > 0:
        market_price = market_value_usd / shares_held

    return {
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
            f"sec-api 13F {accession_no}: {name} cusip={cusip} "
            f"shares={shares_held} value={h.get('value')}"
        ),
        "confidence": "high",
        "note": f"source=sec_api_13f cusip={cusip} class={h.get('titleOfClass')}",
        "price_source": "13f",
        "price_as_of": filing_date,
        "_source": "sec_api_13f",
        "_cusip": cusip,
    }


def map_filing_holdings(
    filing: dict,
    *,
    parent_ticker: str,
    aliases: dict[str, str] | None = None,
    cusip_map: dict[str, str] | None = None,
) -> list[dict]:
    """Map one sec-api filing object to raw holding rows."""
    accession = filing.get("accessionNo")
    filed = (filing.get("filedAt") or "")[:10] or None
    rows: list[dict] = []
    for h in filing.get("holdings") or []:
        row = map_holding_to_row(
            h,
            parent_ticker=parent_ticker,
            accession_no=accession,
            filing_date=filed,
            aliases=aliases,
            cusip_map=cusip_map,
        )
        if row:
            rows.append(row)
    return rows


def _post_13f(body: dict, api_key: str) -> dict:
    data = None
    for attempt in range(6):
        resp = requests.post(
            _SEC_13F_URL,
            headers={"Authorization": api_key, "Content-Type": "application/json"},
            json=body,
            timeout=120,
        )
        if resp.status_code == 429:
            time.sleep(2**attempt)
            continue
        resp.raise_for_status()
        data = resp.json()
        break
    if data is None:
        raise RuntimeError("sec-api 13F rate limited after retries")
    return data


def fetch_filer_13f_filings(
    cik: str,
    *,
    as_of: str | None = None,
    max_filings: int = 40,
    lookback_start: str | None = None,
    use_cache: bool = True,
) -> list[dict]:
    """Fetch Form 13F filings for a filer CIK (newest first), capped at max_filings.

    When ``lookback_start`` is set, only filings with filedAt in
    ``[lookback_start, as_of|today]`` are returned.
    """
    from datetime import date

    from .lookback import date_on_or_after

    api_key = os.environ.get("SEC_API_KEY")
    if not api_key:
        raise RuntimeError("SEC_API_KEY not set")

    q_cik = cik_for_query(cik)
    end = (as_of or date.today().isoformat())[:10]
    start = (lookback_start or "2000-01-01")[:10]
    as_of_part = f" AND filedAt:[{start} TO {end}]"
    query = f"cik:{q_cik}{as_of_part}"
    cache_key = f"filer|{q_cik}|{start}|{end}|{max_filings}"
    if use_cache:
        cached = _CACHE.get(cache_key)
        if cached is not None:
            return list(cached)

    out: list[dict] = []
    frm = 0
    page = min(50, max(1, max_filings))
    total = None
    while len(out) < max_filings:
        body = {
            "query": query,
            "from": str(frm),
            "size": str(page),
            "sort": [{"filedAt": {"order": "desc"}}],
        }
        data = _post_13f(body, api_key)
        batch = data.get("data") or []
        tot = data.get("total")
        if isinstance(tot, dict):
            total = tot.get("value")
        else:
            total = tot

        for filing in batch:
            if not isinstance(filing, dict):
                continue
            filed = str(filing.get("filedAt") or "")[:10]
            if lookback_start and filed and not date_on_or_after(filed, start):
                continue
            out.append(filing)
            if len(out) >= max_filings:
                break

        if not batch or len(batch) < page:
            break
        frm += page
        if total is not None and frm >= total:
            break
        time.sleep(0.25)

    if use_cache:
        _CACHE.set(cache_key, out, expire=_CACHE_TTL_SECONDS)
    return out


def collect_filer_13f_periods(
    *,
    parent_ticker: str,
    cik: str,
    as_of: str | None = None,
    max_filings: int = 40,
    lookback_start: str | None = None,
) -> tuple[list[tuple[str, str, str, list[dict]]], dict[str, Any]]:
    """Oldest→newest (period_end, filing_date, accession, rows); one filing per period."""
    meta: dict[str, Any] = {
        "parent_ticker": parent_ticker,
        "cik": cik,
        "num_filings": 0,
        "num_periods": 0,
        "error": None,
        "source": "sec_api_13f",
        "lookback_start": lookback_start,
    }
    try:
        filings = fetch_filer_13f_filings(
            cik,
            as_of=as_of,
            max_filings=max_filings,
            lookback_start=lookback_start,
        )
    except Exception as e:
        meta["error"] = str(e)
        return [], meta

    meta["num_filings"] = len(filings)
    aliases = load_investee_aliases()
    cusip_map = load_cusip_tickers()

    by_period: dict[str, tuple[str, str, str, list[dict]]] = {}
    for filing in filings:
        period_end = (filing.get("periodOfReport") or "")[:10]
        filed = (filing.get("filedAt") or "")[:10]
        if not period_end:
            period_end = filed
        if not period_end:
            continue
        accession = filing.get("accessionNo") or ""
        # Prefer latest filedAt when multiple filings share a period.
        prev = by_period.get(period_end)
        if prev is not None and filed and prev[1] and filed < prev[1]:
            continue
        rows = map_filing_holdings(
            filing, parent_ticker=parent_ticker, aliases=aliases, cusip_map=cusip_map
        )
        by_period[period_end] = (period_end, filed or period_end, accession, rows)

    ordered = [by_period[k] for k in sorted(by_period.keys())]
    meta["num_periods"] = len(ordered)
    if not ordered:
        meta["error"] = meta.get("error") or f"no 13F filings for CIK {cik}"
    return ordered, meta


def latest_filer_13f_rows(
    *,
    parent_ticker: str,
    cik: str,
    as_of: str | None = None,
) -> tuple[list[dict], dict[str, Any]]:
    """Latest period holdings for live extract."""
    ordered, meta = collect_filer_13f_periods(
        parent_ticker=parent_ticker, cik=cik, as_of=as_of, max_filings=8
    )
    if not ordered:
        return [], meta
    period_end, filing_date, accession, rows = ordered[-1]
    meta["13f_accession"] = accession
    meta["13f_filing_date"] = filing_date
    meta["13f_period_end"] = period_end
    meta["num_13f"] = len(rows)
    return rows, meta
