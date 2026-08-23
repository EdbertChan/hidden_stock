#!/usr/bin/env python3
"""Experimental probe: recover Tencent's Meituan (3690.HK) stake from HK sources.

Litmus: Meituan is SEHK-only — absent from SEC 13G. Pre-Dagster; do not wire
into tencent.py / assets yet.

  uv run python scripts/explore_tencent_meituan_stake.py [--years 5] \\
    [--json-out exports/meituan_stake_probe.json]
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

import pdfplumber
import requests

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from hidden_stock.quirks.holdings.parse_hk_annual import (  # noqa: E402
    HKEX_BASE,
    HKEX_TITLE_SEARCH,
    TENCENT_STOCK_ID,
    _session,
)

# Anchor filing from research (distribution in specie → ~1.7%).
KNOWN_DISTRIBUTION_PDF = (
    "https://www1.hkexnews.hk/listedco/listconews/sehk/2023/0324/2023032400883.pdf"
)

MEITUAN_STOCK = "03690"
DI_SEARCH_URL = "https://di.hkex.com.hk/di/NSSrchCorpList.aspx"

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 "
    "hidden_stock research"
)


def _pct(s: str) -> float | None:
    try:
        return float(str(s).replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return None


def _shares(s: str) -> float | None:
    try:
        return float(str(s).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def search_tencent_meituan_announcements(
    *,
    years: int = 5,
    session: requests.Session | None = None,
) -> list[dict[str, Any]]:
    """HKEXnews title list for 00700; keep rows whose title mentions Meituan."""
    s = session or _session()
    to_d = date.today().strftime("%Y%m%d")
    from_y = max(2000, date.today().year - max(1, int(years)) - 1)
    from_d = f"{from_y}0101"
    r = s.get(
        HKEX_TITLE_SEARCH,
        params={
            "lang": "E",
            "market": "SEHK",
            "searchType": "0",
            "documentType": "-1",
            "t1code": "-2",
            "t2Gcode": "-2",
            "t2code": "-2",
            "stockId": str(TENCENT_STOCK_ID),
            "fromDate": from_d,
            "toDate": to_d,
            "category": "0",
            "rowRange": "100",
            "sortDir": "0",
            "title": "Meituan",  # HKEXnews title keyword filter
        },
        timeout=90,
    )
    r.raise_for_status()
    payload = r.json()
    raw = payload.get("result") or "[]"
    rows = json.loads(raw) if isinstance(raw, str) else raw
    out: list[dict[str, Any]] = []
    for row in rows or []:
        title = str(row.get("TITLE") or "")
        if not re.search(r"meituan|美团", title, re.I):
            continue
        link = str(row.get("FILE_LINK") or "").strip()
        if not link or not link.lower().endswith(".pdf"):
            continue
        url = link if link.startswith("http") else f"{HKEX_BASE}{link}"
        out.append(
            {
                "title": title,
                "date_time": row.get("DATE_TIME"),
                "url": url,
                "news_id": row.get("NEWS_ID"),
            }
        )
    return out


def fetch_pdf_text(url: str, *, session: requests.Session, max_pages: int = 12) -> str:
    r = session.get(url, timeout=120)
    r.raise_for_status()
    parts: list[str] = []
    with pdfplumber.open(io.BytesIO(r.content)) as pdf:
        for i, page in enumerate(pdf.pages[:max_pages]):
            t = page.extract_text() or ""
            if t.strip():
                parts.append(t)
    return "\n".join(parts)


def extract_meituan_stake_from_text(
    text: str,
    *,
    url: str,
    title: str | None = None,
    as_of_hint: str | None = None,
) -> list[dict[str, Any]]:
    """Pull ownership % / share counts near Meituan language. Fail closed."""
    if not text or not re.search(r"meituan|美团", text, re.I):
        return []
    rows: list[dict[str, Any]] = []
    # Post-distribution wording (2023 anchor).
    for m in re.finditer(
        r"shareholding percentage in Meituan[^\n]{0,120}?"
        r"(?:reduced to|will be|is|was)\s+approximately\s+"
        r"([\d.]+)\s*%",
        text,
        re.I | re.S,
    ):
        pct = _pct(m.group(1))
        if pct is None:
            continue
        quote = re.sub(r"\s+", " ", m.group(0))[:280]
        rows.append(
            {
                "source": "hkexnews_announcement",
                "url": url,
                "title": title,
                "as_of": as_of_hint,
                "ownership_pct": pct,
                "shares": None,
                "quote": quote,
                "confidence": "high",
            }
        )
    # Generic “interest in Meituan … X%”
    for m in re.finditer(
        r"(?:interest|shareholding|stake)\s+in\s+Meituan[^\n%]{0,100}?([\d.]+)\s*%",
        text,
        re.I,
    ):
        pct = _pct(m.group(1))
        if pct is None or pct <= 0 or pct > 100:
            continue
        quote = re.sub(r"\s+", " ", m.group(0))[:280]
        if any(abs((r.get("ownership_pct") or -1) - pct) < 1e-6 for r in rows):
            continue
        rows.append(
            {
                "source": "hkexnews_announcement",
                "url": url,
                "title": title,
                "as_of": as_of_hint,
                "ownership_pct": pct,
                "shares": None,
                "quote": quote,
                "confidence": "medium",
            }
        )
    # Share counts near Meituan Class B
    for m in re.finditer(
        r"Meituan[^\n]{0,80}?([\d,]{6,})\s+(?:Class\s+B\s+)?(?:ordinary\s+)?shares",
        text,
        re.I,
    ):
        sh = _shares(m.group(1))
        if sh is None or sh < 1_000:
            continue
        quote = re.sub(r"\s+", " ", m.group(0))[:280]
        rows.append(
            {
                "source": "hkexnews_announcement",
                "url": url,
                "title": title,
                "as_of": as_of_hint,
                "ownership_pct": None,
                "shares": sh,
                "quote": quote,
                "confidence": "medium",
            }
        )
    return rows


def probe_hkex_di(
    *,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """Best-effort DI probe for Tencent → Meituan (03690). Soft-fail on blocks."""
    s = session or requests.Session()
    s.headers.update({"User-Agent": _UA, "Accept": "text/html,application/xhtml+xml"})
    out: dict[str, Any] = {
        "source": "hkex_di",
        "di_status": "unknown",
        "url": DI_SEARCH_URL,
        "rows": [],
        "error": None,
    }
    try:
        # Landing / search page — often needs form POST; probe GET first.
        r = s.get(
            DI_SEARCH_URL,
            params={"lang": "EN"},
            timeout=45,
        )
        if r.status_code in (403, 429, 503):
            out["di_status"] = "blocked"
            out["error"] = f"HTTP {r.status_code}"
            return out
        text = r.text or ""
        if re.search(r"captcha|cloudflare|access denied", text, re.I):
            out["di_status"] = "blocked"
            out["error"] = "captcha_or_waf"
            return out
        # Try corporate list / stock search endpoints used by the DI UI.
        # Stock-code search page (may 404 / redirect — document result).
        stock_url = (
            "https://di.hkex.com.hk/di/NSSrchCorpList.aspx"
            f"?sa1=cl&scsd={date.today().replace(year=date.today().year - 2).strftime('%d/%m/%Y')}"
            f"&sced={date.today().strftime('%d/%m/%Y')}"
            f"&sc={MEITUAN_STOCK}&src=MAIN&lang=EN"
        )
        out["url"] = stock_url
        r2 = s.get(stock_url, timeout=45)
        out["http_status"] = r2.status_code
        body = r2.text or ""
        if r2.status_code != 200 or re.search(
            r"captcha|cloudflare|access denied", body, re.I
        ):
            out["di_status"] = "blocked"
            out["error"] = f"stock_search HTTP {r2.status_code}"
            return out
        # Look for Tencent + percentage in table-ish HTML.
        if not re.search(r"Tencent", body, re.I):
            out["di_status"] = "no_tencent_hit"
            out["error"] = "page_loaded_but_no_tencent_mention"
            # Still record a snippet length for debugging.
            out["body_chars"] = len(body)
            return out
        for m in re.finditer(
            r"Tencent[\s\S]{0,400}?([\d.]+)\s*%",
            body,
            re.I,
        ):
            pct = _pct(m.group(1))
            if pct is None or pct <= 0 or pct > 100:
                continue
            quote = re.sub(r"\s+", " ", m.group(0))[:280]
            out["rows"].append(
                {
                    "source": "hkex_di",
                    "url": stock_url,
                    "title": "DI search HTML",
                    "as_of": None,
                    "ownership_pct": pct,
                    "shares": None,
                    "quote": quote,
                    "confidence": "low",
                }
            )
        out["di_status"] = "ok" if out["rows"] else "parsed_empty"
        return out
    except Exception as e:
        out["di_status"] = "blocked"
        out["error"] = str(e)
        return out


def composition_has_meituan(exports_dir: Path) -> dict[str, Any]:
    path = exports_dir / "tcehy_equity_holdings_history.csv"
    if not path.is_file():
        return {"path": str(path), "present": False, "reason": "missing_csv"}
    text = path.read_text(encoding="utf-8", errors="replace")
    hit = bool(re.search(r"meituan|3690", text, re.I))
    return {"path": str(path), "present": hit, "reason": "qoq_csv_scan"}


def _as_of_from_hkex_datetime(dt: Any) -> str | None:
    """HKEXnews DATE_TIME is often milliseconds since epoch as string."""
    if dt is None:
        return None
    try:
        ms = int(str(dt).strip())
        if ms > 10_000_000_000:
            ms //= 1000
        return date.fromtimestamp(ms).isoformat()
    except Exception:
        s = str(dt)[:10]
        if re.match(r"\d{4}-\d{2}-\d{2}", s):
            return s
        return None


def run_probe(*, years: int = 5, max_pdfs: int = 8) -> dict[str, Any]:
    s = _session()
    evidence: list[dict[str, Any]] = []
    meta: dict[str, Any] = {
        "years": years,
        "announcements_listed": 0,
        "pdfs_fetched": 0,
        "errors": [],
    }

    # Always try the known distribution PDF first (stable litmus anchor).
    try:
        text = fetch_pdf_text(KNOWN_DISTRIBUTION_PDF, session=s)
        meta["pdfs_fetched"] += 1
        evidence.extend(
            extract_meituan_stake_from_text(
                text,
                url=KNOWN_DISTRIBUTION_PDF,
                title="PAYMENT OF INTERIM DIVIDEND … MEITUAN (known anchor)",
                as_of_hint="2023-03-24",
            )
        )
    except Exception as e:
        meta["errors"].append(f"known_pdf: {e}")

    try:
        anns = search_tencent_meituan_announcements(years=years, session=s)
        meta["announcements_listed"] = len(anns)
    except Exception as e:
        anns = []
        meta["errors"].append(f"title_search: {e}")

    for ann in anns[:max_pdfs]:
        url = ann["url"]
        if url == KNOWN_DISTRIBUTION_PDF and evidence:
            continue
        try:
            time.sleep(0.4)
            text = fetch_pdf_text(url, session=s)
            meta["pdfs_fetched"] += 1
            evidence.extend(
                extract_meituan_stake_from_text(
                    text,
                    url=url,
                    title=ann.get("title"),
                    as_of_hint=_as_of_from_hkex_datetime(ann.get("date_time")),
                )
            )
        except Exception as e:
            meta["errors"].append(f"pdf {url}: {e}")

    di = probe_hkex_di(session=requests.Session())
    meta["di"] = {
        "di_status": di.get("di_status"),
        "error": di.get("error"),
        "url": di.get("url"),
        "http_status": di.get("http_status"),
    }
    evidence.extend(di.get("rows") or [])

    # Litmus: ownership in 1.0–3.0% (post-distribution band).
    litmus_hits = [
        e
        for e in evidence
        if e.get("ownership_pct") is not None and 1.0 <= float(e["ownership_pct"]) <= 3.0
    ]
    composition = composition_has_meituan(_ROOT / "exports")
    litmus_pass = len(litmus_hits) >= 1

    return {
        "litmus": "PASS" if litmus_pass else "FAIL",
        "litmus_rule": "ownership_pct in [1.0, 3.0] from announcement/DI",
        "litmus_hits": litmus_hits,
        "composition_csv_has_meituan": composition,
        "evidence": evidence,
        "meta": meta,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--years", type=int, default=5)
    ap.add_argument(
        "--json-out",
        type=Path,
        default=_ROOT / "exports" / "meituan_stake_probe.json",
    )
    ap.add_argument("--max-pdfs", type=int, default=8)
    args = ap.parse_args()

    result = run_probe(years=args.years, max_pdfs=args.max_pdfs)
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")

    print(f"LITMUS: {result['litmus']}")
    print(f"rule: {result['litmus_rule']}")
    print(
        "composition_csv Meituan present:",
        result["composition_csv_has_meituan"].get("present"),
        f"({result['composition_csv_has_meituan'].get('reason')})",
    )
    print(
        f"announcements_listed={result['meta'].get('announcements_listed')} "
        f"pdfs_fetched={result['meta'].get('pdfs_fetched')} "
        f"di_status={result['meta'].get('di', {}).get('di_status')}"
    )
    print("evidence rows:", len(result.get("evidence") or []))
    for e in (result.get("litmus_hits") or result.get("evidence") or [])[:5]:
        print(
            f"  - pct={e.get('ownership_pct')} shares={e.get('shares')} "
            f"src={e.get('source')} conf={e.get('confidence')}"
        )
        print(f"    {e.get('quote')}")
        print(f"    {e.get('url')}")
    if result["meta"].get("errors"):
        print("errors:")
        for err in result["meta"]["errors"][:8]:
            print(f"  - {err}")
    print(f"wrote {args.json_out}")
    return 0 if result["litmus"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
