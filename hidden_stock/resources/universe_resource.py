import io

import dagster as dg
import pandas as pd
import requests

SP500_WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"

# CONFIRMED BROKEN as of 2026-08-17: iShares now puts a compliance/landing
# page in front of this endpoint — it returns their marketing HTML instead
# of the CSV, even with the documented switchLocale/siteEntryPassthrough
# bypass params. Route russell2000/russell3000 through manual_csv or a paid
# vendor until this is fixed.
ISHARES_CSV_URLS = {
    "russell2000": "https://www.ishares.com/us/products/239710/ishares-russell-2000-etf/1467271812596.ajax?fileType=csv&fileName=IWM_holdings&dataType=fund",
    "russell3000": "https://www.ishares.com/us/products/239714/ishares-russell-3000-etf/1467271812596.ajax?fileType=csv&fileName=IWV_holdings&dataType=fund",
}


class UniverseResource(dg.ConfigurableResource):
    manual_csv_path: str = "seed_data/manual_universe.csv"

    def get_tickers(self, index_source: str) -> pd.DataFrame:
        if index_source == "sp500":
            return self._get_sp500()
        if index_source in ISHARES_CSV_URLS:
            return self._get_ishares(index_source)
        if index_source == "manual_csv":
            df = pd.read_csv(self.manual_csv_path)
            df["index_name"] = index_source
            return df[["ticker", "index_name"]]
        raise ValueError(f"Unknown index_source: {index_source}")

    def _get_sp500(self) -> pd.DataFrame:
        # Wikipedia rejects requests with no User-Agent (pandas.read_html's
        # default urllib request gets a 403), so fetch the page ourselves first.
        resp = requests.get(SP500_WIKI_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
        resp.raise_for_status()
        tables = pd.read_html(io.StringIO(resp.text))
        df = tables[0][["Symbol"]].rename(columns={"Symbol": "ticker"})
        df["ticker"] = df["ticker"].str.replace(".", "-", regex=False)
        df["index_name"] = "sp500"
        return df

    def _get_ishares(self, index_source: str) -> pd.DataFrame:
        resp = requests.get(ISHARES_CSV_URLS[index_source], timeout=30)
        resp.raise_for_status()
        lines = resp.text.splitlines()
        header_idx = next(i for i, line in enumerate(lines) if line.startswith("Ticker,"))
        df = pd.read_csv(io.StringIO("\n".join(lines[header_idx:])))
        df = df.rename(columns={"Ticker": "ticker"})
        df = df[df["ticker"].notna() & (df["ticker"] != "-")]
        df["index_name"] = index_source
        return df[["ticker", "index_name"]]
