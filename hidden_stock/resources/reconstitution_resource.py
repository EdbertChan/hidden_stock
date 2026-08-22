import io
import re

import dagster as dg
import pandas as pd
import pdfplumber
import requests

RECONSTITUTION_BASE = "https://www.lseg.com/content/dam/ftse-russell/en_us/documents/other"

# Confirmed live (HTTP 200) as of 2026-08-17. LSEG's footer requires a
# license to redistribute/store this data — fine for personal research/
# backtesting, not for republishing the raw list.
RECONSTITUTION_EVENTS = [
    {
        "date": "2023-06-23",
        "deletions": "ru3000-deletions-final-20230623.pdf",
        "additions": "ru3000-additions-final-20230623.pdf",
    },
    {
        "date": "2024-06-28",
        "deletions": "ru3000-deletions-final-20240628.pdf",
        "additions": "ru3000-additions-final-20240628.pdf",
    },
    {
        "date": "2025-06-27",
        "deletions": "ru3000-deletions-20250627.pdf",
        "additions": "ru3000-additions-20250627.pdf",
    },
    {
        "date": "2026-06-26",
        "deletions": "ru3000-deletions-20260626.pdf",
        "additions": "ru3000-additions-20260626.pdf",
    },
]

INDUSTRIES = (
    "Health Care|Technology|Industrials|Consumer Discretionary|Consumer Staples|"
    "Basic Materials|Utilities|Financials|Energy|Real Estate|Telecommunications"
)
ROW_PATTERN = re.compile(
    r"^\s*([A-Z0-9][A-Z0-9 .,()&'/-]*?)\s{2,}([A-Z]{1,6})\s{2,}(" + INDUSTRIES + r")\s*$",
    re.MULTILINE,
)

# LSEG only gives us Russell 3000 reconstitution PDFs (4 known annual events).
# S&P has no equivalent bulk download, but Wikipedia maintains a clean,
# continuously-updated table of every S&P 600 addition/removal with dates —
# used here as a second index source so index-hop cases (e.g. a company
# leaving the S&P 600 outside of a Russell reconstitution window) get caught.
SP600_HISTORY_URL = "https://en.wikipedia.org/wiki/Historical_components_of_the_S%26P_600"
SP600_COLUMNS = [
    "date",
    "added_ticker",
    "added_security",
    "removed_ticker",
    "removed_security",
    "reason",
    "refs",
]


class ReconstitutionResource(dg.ConfigurableResource):
    def _parse_pdf(self, url: str, reconstitution_date: str) -> pd.DataFrame:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        with pdfplumber.open(io.BytesIO(resp.content)) as pdf:
            text = "\n".join(page.extract_text(layout=True) or "" for page in pdf.pages)
        rows = ROW_PATTERN.findall(text)
        df = pd.DataFrame(rows, columns=["company", "ticker", "industry"])
        df["reconstitution_date"] = reconstitution_date
        return df

    def get_deletions(self, log=None) -> pd.DataFrame:
        russell = self._get_all("deletions", log)
        russell["source_index"] = "russell3000"
        sp600 = self._get_sp600_side("removed", log)
        return pd.concat([russell, sp600], ignore_index=True)

    def get_additions(self, log=None) -> pd.DataFrame:
        russell = self._get_all("additions", log)
        russell["source_index"] = "russell3000"
        sp600 = self._get_sp600_side("added", log)
        return pd.concat([russell, sp600], ignore_index=True)

    def _get_all(self, kind: str, log) -> pd.DataFrame:
        frames = []
        for event in RECONSTITUTION_EVENTS:
            url = f"{RECONSTITUTION_BASE}/{event[kind]}"
            try:
                frames.append(self._parse_pdf(url, event["date"]))
            except Exception as e:
                if log:
                    log.error(f"Failed to fetch/parse {kind} PDF for {event['date']} ({url}): {e}")
        if not frames:
            return pd.DataFrame(columns=["company", "ticker", "industry", "reconstitution_date"])
        return pd.concat(frames, ignore_index=True)

    def _fetch_sp600_changes(self, log) -> pd.DataFrame:
        try:
            resp = requests.get(
                SP600_HISTORY_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=30
            )
            resp.raise_for_status()
            tables = pd.read_html(io.StringIO(resp.text))
        except Exception as e:
            if log:
                log.error(f"Failed to fetch/parse S&P 600 historical components page: {e}")
            return pd.DataFrame(columns=SP600_COLUMNS + ["reconstitution_date"])
        df = tables[0]
        df.columns = SP600_COLUMNS
        df["reconstitution_date"] = (
            pd.to_datetime(df["date"], format="%B %d, %Y", errors="coerce").dt.date.astype(str)
        )
        return df

    def _get_sp600_side(self, side: str, log) -> pd.DataFrame:
        """side is 'added' or 'removed' — pulls that half of the S&P 600
        historical-changes table into the same [company, ticker, industry,
        reconstitution_date, source_index] shape as the Russell 3000 data."""
        changes = self._fetch_sp600_changes(log)
        rows = changes[changes[f"{side}_ticker"].notna()]
        df = pd.DataFrame(
            {
                "company": rows[f"{side}_security"],
                "ticker": rows[f"{side}_ticker"],
                "industry": None,
                "reconstitution_date": rows["reconstitution_date"],
            }
        ).reset_index(drop=True)
        df["source_index"] = "sp600"
        return df
