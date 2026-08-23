"""HKEX annual report parser (unique form type — not 13F / 13G / US notes).

Lists Annual Reports via HKEXnews, fetches PDFs, extracts Note 22 associate
aggregates and portfolio FV / carrying disclosures. Fail closed: skip years
that miss required patterns (never invent).
"""

from __future__ import annotations

import io
import json
import re
import time
from typing import Any

import pdfplumber
import requests

HKEX_BASE = "https://www1.hkexnews.hk"
HKEX_PREFIX = f"{HKEX_BASE}/search/prefix.do"
HKEX_TITLE_SEARCH = f"{HKEX_BASE}/search/titleSearchServlet.do"

# Tencent Holdings — default stock for this parser v1.
DEFAULT_STOCK_CODE = "00700"
TENCENT_STOCK_ID = 7609  # HKEXnews stockId for 00700

# Approximate CNY/USD for disclosed RMB totals (same constant as prior hardcode).
RMB_PER_USD = 7.1884

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 "
    "hidden_stock research"
)


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": _UA,
            "Referer": f"{HKEX_BASE}/search/titlesearch.xhtml?lang=en",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        }
    )
    return s


def resolve_hkex_stock_id(stock_code: str = DEFAULT_STOCK_CODE) -> int | None:
    """Map SEHK code (e.g. 00700) → HKEXnews stockId."""
    code = re.sub(r"\D", "", str(stock_code or ""))[-5:].zfill(5)
    if code == "00700":
        return TENCENT_STOCK_ID
    s = _session()
    r = s.get(
        HKEX_PREFIX,
        params={
            "callback": "callback",
            "lang": "EN",
            "type": "A",
            "name": code,
            "market": "SEHK",
        },
        timeout=60,
    )
    r.raise_for_status()
    m = re.search(r"callback\((.*)\)\s*;?\s*$", r.text, re.S)
    if not m:
        return None
    data = json.loads(m.group(1))
    for item in data.get("stockInfo") or []:
        if str(item.get("code") or "").zfill(5) == code:
            return int(item["stockId"])
    return None


def _parse_title_year(title: str) -> int | None:
    m = re.search(r"ANNUAL\s+REPORT\s+(\d{4})", title or "", re.I)
    if m:
        return int(m.group(1))
    return None


def fetch_hk_annual_pdfs(
    *,
    stock_code: str = DEFAULT_STOCK_CODE,
    years: int = 8,
    session: requests.Session | None = None,
) -> list[dict[str, Any]]:
    """List HKEXnews Annual Report PDFs newest-first.

    Each item: ``{year, title, date_time, file_link, url, news_id}``.
    """
    s = session or _session()
    stock_id = resolve_hkex_stock_id(stock_code)
    if stock_id is None:
        return []
    from datetime import date

    to_d = date.today().strftime("%Y%m%d")
    from_y = max(2000, date.today().year - max(1, int(years)) - 1)
    from_d = f"{from_y}0101"
    r = s.get(
        HKEX_TITLE_SEARCH,
        params={
            "lang": "E",
            "market": "SEHK",
            "searchType": "1",
            "documentType": "-1",
            "t1code": "40000",
            "t2Gcode": "-2",
            "t2code": "40100",  # Annual Report
            "stockId": str(stock_id),
            "fromDate": from_d,
            "toDate": to_d,
            "category": "0",
            "rowRange": "100",
            "sortDir": "0",
        },
        timeout=90,
    )
    r.raise_for_status()
    payload = r.json()
    raw = payload.get("result") or "[]"
    rows = json.loads(raw) if isinstance(raw, str) else raw
    out: list[dict[str, Any]] = []
    seen_years: set[int] = set()
    for row in rows or []:
        title = str(row.get("TITLE") or "")
        year = _parse_title_year(title)
        if year is None or year in seen_years:
            continue
        link = str(row.get("FILE_LINK") or "").strip()
        if not link or not link.lower().endswith(".pdf"):
            continue
        # Prefer English annual report (skip ESG-only / Chinese-only if titled differently)
        if "ESG" in title.upper() and "ANNUAL REPORT" not in title.upper():
            continue
        url = link if link.startswith("http") else f"{HKEX_BASE}{link}"
        seen_years.add(year)
        out.append(
            {
                "year": year,
                "title": title,
                "date_time": row.get("DATE_TIME"),
                "file_link": link,
                "url": url,
                "news_id": row.get("NEWS_ID"),
            }
        )
        if len(out) >= int(years):
            break
    return out


def pdf_to_text(pdf_bytes: bytes) -> str:
    """Extract text from pages that mention associates / investees (speed)."""
    parts: list[str] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            t = page.extract_text() or ""
            low = t.lower()
            if (
                "investments in associates" in low
                or "listed investee" in low
                or "unlisted investee" in low
                or "listed entities" in low
            ):
                parts.append(t)
    return "\n".join(parts)


def _num(s: str) -> float:
    return float(str(s).replace(",", ""))


def parse_hk_annual_text(
    text: str,
    *,
    as_of: str,
    source_url: str,
    rmb_per_usd: float = RMB_PER_USD,
) -> list[dict]:
    """Parse Note 22 / portfolio aggregate disclosures from annual-report text.

    Returns up to four rows (may be fewer if a pattern misses — never invent).
    """
    if not text or not text.strip():
        return []

    def usd(rmb: float) -> float:
        return float(rmb) / float(rmb_per_usd)

    year = int(str(as_of)[:4])

    # Fair value of stakes in listed associates (last number on the year's Listed line)
    listed_assoc_fv = None
    m = re.search(
        rf"{year}\s+Listed entities(?:\s*\(Note\))?\s+([^\n]+)",
        text,
        re.I,
    )
    if m:
        nums = re.findall(r"\(?([\d,]+)\)?", m.group(1))
        # Assets, Liabilities, Revenues, Profit, OCI, Total, FV — need 7 cells
        if len(nums) >= 7:
            listed_assoc_fv = _num(nums[6]) * 1_000_000

    # Prefer carrying from the Note 22 balance breakdown (two-column YoY), not the
    # results table (Listed entities (Note) with 7 numeric columns).
    listed_assoc_carrying = None
    unlisted_assoc_carrying = None
    m = re.search(
        r"Investments in associates\s*[–\-]?\s*Listed entities\s+([\d,]+)\s+[\d,]+"
        r".{0,60}?Unlisted entities\s+([\d,]+)",
        text,
        re.I | re.S,
    )
    if not m:
        m = re.search(
            r"(?<!\()\bListed entities\s+([\d,]+)\s+([\d,]+)\s*"
            r"[–\-]?\s*Unlisted entities\s+([\d,]+)\s+([\d,]+)",
            text,
            re.I | re.S,
        )
        if m:
            listed_assoc_carrying = _num(m.group(1)) * 1_000_000
            unlisted_assoc_carrying = _num(m.group(3)) * 1_000_000
    else:
        listed_assoc_carrying = _num(m.group(1)) * 1_000_000
        unlisted_assoc_carrying = _num(m.group(2)) * 1_000_000

    # Portfolio: listed investees FV (RMB billions)
    listed_investees_fv = None
    m = re.search(
        r"fair value of (?:our )?shareholdings\s*\d*\s*in listed investee "
        r"companies[^0-9]{0,160}RMB\s*([\d.]+)\s*billion",
        text,
        re.I | re.S,
    )
    if m:
        listed_investees_fv = _num(m.group(1)) * 1_000_000_000

    unlisted_investees_carrying = None
    m = re.search(
        r"carrying book value of (?:our )?shareholdings\s+in unlisted\s+"
        r"investee companies[^0-9]{0,160}RMB\s*([\d.]+)\s*billion",
        text,
        re.I | re.S,
    )
    if m:
        unlisted_investees_carrying = _num(m.group(1)) * 1_000_000_000

    rows: list[dict] = []
    fy_tag = f"hk_annual_report_{year}"
    base_note = (
        f"source=hk_annual; form=hkex_annual_report; as_of={as_of}; "
        f"url={source_url}; value_source=hk_annual_note22"
    )

    if listed_assoc_carrying is not None:
        row = {
            "investee_name": "Listed associates (aggregate, HK annual report Note 22)",
            "investee_ticker": "PRIV_HK_LISTED_ASSOCIATES",
            "ownership_pct": None,
            "carrying_usd": usd(listed_assoc_carrying),
            "influence_disclosed": True,
            "filing_gaap_hint": "equity_method",
            "as_of_date": as_of,
            "source_quote": (
                f"Investments in associates – Listed entities "
                f"RMB{listed_assoc_carrying/1e6:,.0f}m (FY{year})"
            ),
            "confidence": "high",
            "note": (
                f"{base_note}; ticker=private_note; "
                "HKEX annual report aggregate; names not disclosed"
            ),
            "price_source": fy_tag,
            "_source": "hk_annual",
            "filing_url": source_url,
        }
        if listed_assoc_fv is not None:
            row["fair_value_disclosed_usd"] = usd(listed_assoc_fv)
            # Nested in all-listed-investees FV — do not double-count in portfolio $.
            if listed_investees_fv is None:
                row["market_value_usd"] = usd(listed_assoc_fv)
            else:
                row["note"] = (
                    f"{row['note']}; nested_in=listed_investees_fv; "
                    "excluded_from_portfolio_mv"
                )
            row["source_quote"] += (
                f"; FV of stakes in listed associates RMB{listed_assoc_fv/1e6:,.0f}m"
            )
        rows.append(row)

    if unlisted_assoc_carrying is not None:
        rows.append(
            {
                "investee_name": "Unlisted associates (aggregate, HK annual report Note 22)",
                "investee_ticker": "PRIV_HK_UNLISTED_ASSOCIATES",
                "ownership_pct": None,
                "carrying_usd": usd(unlisted_assoc_carrying),
                "influence_disclosed": True,
                "filing_gaap_hint": "equity_method",
                "as_of_date": as_of,
                "source_quote": (
                    f"Investments in associates – Unlisted entities "
                    f"RMB{unlisted_assoc_carrying/1e6:,.0f}m (FY{year})"
                ),
                "confidence": "high",
                "note": (
                    f"{base_note}; ticker=private_note; "
                    "No observable market; lookthrough MTM unknown"
                ),
                "price_source": fy_tag,
                "_source": "hk_annual",
                "filing_url": source_url,
            }
        )

    if listed_investees_fv is not None:
        rows.append(
            {
                "investee_name": "All listed investees excl. subsidiaries (aggregate FV)",
                "investee_ticker": "PRIV_HK_LISTED_INVESTEES_FV",
                "ownership_pct": None,
                "fair_value_disclosed_usd": usd(listed_investees_fv),
                "market_value_usd": usd(listed_investees_fv),
                "fair_value_through_earnings": True,
                "filing_gaap_hint": "fv_ni",
                "as_of_date": as_of,
                "source_quote": (
                    f"Fair value of shareholdings in listed investee companies "
                    f"RMB{listed_investees_fv/1e9:.1f}bn (31 Dec {year})"
                ),
                "confidence": "high",
                "note": (
                    f"{base_note}; ticker=private_note; "
                    "Includes FVPL/FVOCI + listed associates on attributable basis"
                ),
                "price_source": fy_tag,
                "_source": "hk_annual",
                "filing_url": source_url,
            }
        )

    if unlisted_investees_carrying is not None:
        rows.append(
            {
                "investee_name": "Unlisted investees excl. subsidiaries (aggregate carrying)",
                "investee_ticker": "PRIV_HK_UNLISTED_INVESTEES",
                "ownership_pct": None,
                "carrying_usd": usd(unlisted_investees_carrying),
                "measurement_alternative": True,
                "filing_gaap_hint": "cost",
                "as_of_date": as_of,
                "source_quote": (
                    f"Carrying book value of unlisted investees "
                    f"RMB{unlisted_investees_carrying/1e9:.1f}bn (31 Dec {year})"
                ),
                "confidence": "high",
                "note": (
                    f"{base_note}; ticker=private_note; "
                    "No market; cannot invent MTM adj"
                ),
                "price_source": fy_tag,
                "_source": "hk_annual",
                "filing_url": source_url,
            }
        )

    return rows


def collect_hk_annual_aggregate_rows(
    *,
    stock_code: str = DEFAULT_STOCK_CODE,
    lookback_years: int = 8,
    session: requests.Session | None = None,
) -> tuple[list[dict], dict]:
    """Fetch + parse Annual Reports → aggregate rows; meta records misses."""
    s = session or _session()
    meta: dict[str, Any] = {
        "stock_code": stock_code,
        "lookback_years": lookback_years,
        "num_pdfs": 0,
        "years_ok": [],
        "parse_miss": [],
        "error": None,
    }
    try:
        pdfs = fetch_hk_annual_pdfs(
            stock_code=stock_code, years=lookback_years, session=s
        )
    except Exception as e:
        meta["error"] = f"list_failed: {e}"
        return [], meta
    meta["num_pdfs"] = len(pdfs)
    all_rows: list[dict] = []
    for item in pdfs:
        year = int(item["year"])
        as_of = f"{year}-12-31"
        url = item["url"]
        time.sleep(0.2)
        try:
            resp = s.get(url, timeout=180)
            resp.raise_for_status()
            text = pdf_to_text(resp.content)
            rows = parse_hk_annual_text(text, as_of=as_of, source_url=url)
        except Exception as e:
            meta["parse_miss"].append({"year": year, "reason": str(e), "url": url})
            continue
        if len(rows) < 2:
            meta["parse_miss"].append(
                {"year": year, "reason": f"only_{len(rows)}_rows", "url": url}
            )
            continue
        meta["years_ok"].append(year)
        all_rows.extend(rows)
    return all_rows, meta
