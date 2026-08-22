from concurrent.futures import ThreadPoolExecutor, as_completed

import dagster as dg
import pandas as pd
import yfinance as yf

from ..resources.db_resource import DBResource

TABLE = "price_book_screen"


def _fetch_pb(ticker: str) -> dict | None:
    info = yf.Ticker(ticker).info
    pb = info.get("priceToBook")
    if pb is None:
        return None
    return {
        "ticker": ticker,
        "price": info.get("currentPrice") or info.get("regularMarketPrice"),
        "book_value": info.get("bookValue"),
        "shares": info.get("sharesOutstanding"),
        "pb_ratio": pb,
    }


@dg.asset(group_name="valuation")
def price_book_screen(
    context: dg.AssetExecutionContext, universe_tickers: pd.DataFrame, db: DBResource
) -> pd.DataFrame:
    tickers = universe_tickers["ticker"].tolist()
    rows = []
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(_fetch_pb, t): t for t in tickers}
        for fut in as_completed(futures):
            ticker = futures[fut]
            try:
                row = fut.result()
                if row:
                    rows.append(row)
            except Exception as e:
                context.log.error(f"yfinance lookup failed for {ticker}: {e}")

    df = pd.DataFrame(rows)
    # 0 < P/B < 1: below book value AND positive book value — negative book
    # value (net liabilities, common after heavy buybacks) also satisfies
    # "pb_ratio < 1" but isn't a "cheap stock", it's a distressed balance sheet.
    screened = (
        df[(df["pb_ratio"] < 1) & (df["pb_ratio"] > 0)].reset_index(drop=True) if not df.empty else df
    )
    engine = db.get_engine()
    screened.to_sql(TABLE, engine, schema="stock_data", if_exists="replace", index=False)
    context.add_output_metadata({"num_screened": len(screened), "universe_size": len(tickers)})
    return screened
