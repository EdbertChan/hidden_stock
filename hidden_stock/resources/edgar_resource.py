import time

import dagster as dg
import pandas as pd
import requests
from bs4 import BeautifulSoup

_TICKER_CIK_CACHE: dict[str, str] | None = None


def _load_ticker_cik_map(session: requests.Session) -> dict[str, str]:
    global _TICKER_CIK_CACHE
    if _TICKER_CIK_CACHE is None:
        resp = session.get("https://www.sec.gov/files/company_tickers.json", timeout=15)
        resp.raise_for_status()
        data = resp.json()
        _TICKER_CIK_CACHE = {
            entry["ticker"].upper(): str(entry["cik_str"]).zfill(10) for entry in data.values()
        }
    return _TICKER_CIK_CACHE


class EdgarResource(dg.ConfigurableResource):
    """SEC EDGAR client. No API key required, but SEC requires a descriptive
    User-Agent on every request and rate-limits to ~10 req/sec."""

    user_agent: str = dg.EnvVar("SEC_EDGAR_USER_AGENT")

    def _session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update({"User-Agent": self.user_agent})
        return session

    def get_cik(self, ticker: str) -> str | None:
        session = self._session()
        mapping = _load_ticker_cik_map(session)
        return mapping.get(ticker.upper())

    def get_latest_filing(
        self,
        cik: str,
        form_types: tuple[str, ...] = ("10-K", "10-Q"),
        as_of: str | None = None,
    ) -> dict | None:
        """Latest filing among form_types on or before as_of."""
        filings = self.list_filings(cik, form_types=form_types, as_of=as_of, limit=1)
        return filings[0] if filings else None

    def list_filings(
        self,
        cik: str,
        form_types: tuple[str, ...] = ("10-K", "10-Q"),
        as_of: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        """Recent filings matching form_types, newest first, optionally as-of capped."""
        session = self._session()
        resp = session.get(f"https://data.sec.gov/submissions/CIK{cik}.json", timeout=15)
        resp.raise_for_status()
        recent = resp.json()["filings"]["recent"]
        as_of_ts = pd.Timestamp(as_of) if as_of else None
        out: list[dict] = []
        for i, form in enumerate(recent["form"]):
            if form not in form_types:
                continue
            filing_date = recent["filingDate"][i]
            if as_of_ts is not None and pd.Timestamp(filing_date) > as_of_ts:
                continue
            out.append(
                {
                    "accession_no": recent["accessionNumber"][i],
                    "filing_date": filing_date,
                    "primary_document": recent["primaryDocument"][i],
                    "form": form,
                }
            )
            if len(out) >= limit:
                break
        return out

    def fetch_filing_document(self, cik: str, accession_no: str, primary_document: str) -> str:
        session = self._session()
        accession_nodash = accession_no.replace("-", "")
        url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession_nodash}/{primary_document}"
        time.sleep(0.15)  # stay well under SEC's ~10 req/sec limit
        resp = session.get(url, timeout=20)
        resp.raise_for_status()
        return resp.text

    def list_filing_documents(self, cik: str, accession_no: str) -> list[dict]:
        """Files in an accession (from the EDGAR index.json)."""
        session = self._session()
        accession_nodash = accession_no.replace("-", "")
        url = (
            f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
            f"{accession_nodash}/index.json"
        )
        time.sleep(0.15)
        resp = session.get(url, timeout=20)
        resp.raise_for_status()
        items = resp.json().get("directory", {}).get("item", [])
        out = []
        for it in items:
            name = it.get("name")
            if not name:
                continue
            out.append(
                {
                    "name": name,
                    "type": it.get("type"),
                    "size": it.get("size"),
                }
            )
        return out

    def fetch_13f_infotable(self, cik: str, accession_no: str) -> str | None:
        """Raw Form 13F informationTable XML (not the xsl HTML wrapper)."""
        session = self._session()
        accession_nodash = accession_no.replace("-", "")
        base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession_nodash}"

        candidates: list[str] = []
        try:
            docs = self.list_filing_documents(cik, accession_no)
            for d in docs:
                name = d["name"]
                lower = name.lower()
                if lower == "infotable.xml" or (
                    lower.endswith(".xml") and "infotable" in lower and "xsl" not in lower
                ):
                    candidates.append(name)
        except Exception:
            candidates = []

        if not candidates:
            candidates = ["infotable.xml", "form13fInfoTable.xml", "informationtable.xml"]

        for name in candidates:
            # Skip XSL-rendered HTML paths
            if "xsl" in name.lower():
                continue
            time.sleep(0.15)
            resp = session.get(f"{base}/{name}", timeout=30)
            if resp.status_code == 404:
                continue
            resp.raise_for_status()
            text = resp.text.lstrip()
            if text.startswith("<?xml") or text.startswith("<informationTable") or text.startswith("<ns1:"):
                return resp.text
            # Some filings wrap; still try if content-type is xml
            ctype = (resp.headers.get("content-type") or "").lower()
            if "xml" in ctype and "html" not in ctype:
                return resp.text
        return None

    def get_filing_text(self, html_text: str, max_chars: int = 180000) -> str:
        """Plain text of a filing for LLM extract jobs. Truncated to bound
        cost; the sections we need are almost always within the first ~180k
        characters of the primary document."""
        soup = BeautifulSoup(html_text, "html.parser")
        text = soup.get_text("\n")
        return text[:max_chars].strip()

    def extract_keyword_windows(
        self,
        text: str,
        keywords: tuple[str, ...] = (
            "lifo",
            "fifo",
            "last-in, first-out",
            "last-in, first out",
            "first-in, first-out",
            "costing method",
            "inventory accounting",
            "inventories are stated",
            "inventory is stated",
            "weighted-average",
            "weighted average cost",
            "international financial reporting standards",
            "u.s. generally accepted accounting principles",
            "ifrs",
            "u.s. gaap",
            "inventory",
            "inventories",
        ),
        window_chars: int = 2500,
        max_total_chars: int = 20000,
        priority_keywords: tuple[str, ...] = (
            "lifo",
            "fifo",
            "last-in, first-out",
            "last-in, first out",
            "first-in, first-out",
            "lifo reserve",
            "costing method",
            "inventory accounting",
            "international financial reporting standards",
            "ifrs",
            "u.s. gaap",
        ),
    ) -> str:
        """Keep text near inventory/LIFO keywords so the LLM sees the
        footnote, not the whole 10-K. Priority keywords (LIFO/FIFO) are
        packed first so generic 'inventory' hits do not crowd them out."""
        if not text:
            return ""
        lower = text.lower()

        def find_spans(kws: tuple[str, ...]) -> list[tuple[int, int]]:
            spans: list[tuple[int, int]] = []
            for kw in kws:
                start = 0
                while True:
                    idx = lower.find(kw, start)
                    if idx < 0:
                        break
                    spans.append(
                        (max(0, idx - window_chars), min(len(text), idx + len(kw) + window_chars))
                    )
                    start = idx + len(kw)
            return spans

        def merge(spans: list[tuple[int, int]]) -> list[list[int]]:
            if not spans:
                return []
            spans = sorted(spans)
            merged: list[list[int]] = [list(spans[0])]
            for lo, hi in spans[1:]:
                if lo <= merged[-1][1]:
                    merged[-1][1] = max(merged[-1][1], hi)
                else:
                    merged.append([lo, hi])
            return merged

        priority = merge(find_spans(priority_keywords))
        secondary = merge(find_spans(tuple(k for k in keywords if k not in priority_keywords)))

        # Priority first, then secondary that is not already covered.
        ordered: list[list[int]] = []
        covered: list[list[int]] = []

        def already_covered(lo: int, hi: int) -> bool:
            for clo, chi in covered:
                if lo >= clo and hi <= chi:
                    return True
            return False

        for lo, hi in priority + secondary:
            if already_covered(lo, hi):
                continue
            ordered.append([lo, hi])
            covered.append([lo, hi])

        if not ordered:
            return text[: min(8000, max_total_chars)].strip()

        parts = []
        total = 0
        for lo, hi in ordered:
            chunk = text[lo:hi].strip()
            if not chunk:
                continue
            if total + len(chunk) > max_total_chars:
                remain = max_total_chars - total
                if remain > 500:
                    parts.append(chunk[:remain])
                break
            parts.append(chunk)
            total += len(chunk)
        return "\n\n---\n\n".join(parts)

    def get_company_facts(self, cik: str) -> dict | None:
        """Full XBRL fact history (StockholdersEquity, shares outstanding, etc)
        for a company, used to build a historical book-value-per-share series."""
        session = self._session()
        time.sleep(0.15)
        resp = session.get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json", timeout=20)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()
