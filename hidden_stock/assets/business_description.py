from concurrent.futures import ThreadPoolExecutor, as_completed

import dagster as dg
import pandas as pd

from ..resources.db_resource import DBResource
from ..resources.edgar_resource import EdgarResource
from ..resources.llm_protocol import FilingLLM

TABLE = "business_descriptions"
COLUMNS = ["ticker", "accession_no", "filing_date", "description", "source_quote"]


def describe_ticker(
    context: dg.AssetExecutionContext,
    ticker: str,
    edgar: EdgarResource,
    llm: FilingLLM,
    already_done: set[tuple[str, str]],
) -> dict | None:
    """Shared by the live screener and the historical backtest: fetch the
    latest 10-K's text and have the LLM find the "Item 1. Business" section
    and summarize it, skipping any (ticker, accession_no) already described.
    Must back its answer with a verbatim source_quote."""
    cik = edgar.get_cik(ticker)
    if not cik:
        context.log.warning(f"No CIK found for {ticker}, skipping business description")
        return None
    filing = edgar.get_latest_filing(cik, form_types=("10-K",))
    if not filing:
        context.log.warning(f"No 10-K found for {ticker} (CIK {cik})")
        return None
    if (ticker, filing["accession_no"]) in already_done:
        return None  # already described this exact filing — skip the LLM call
    html = edgar.fetch_filing_document(cik, filing["accession_no"], filing["primary_document"])
    filing_text = edgar.get_filing_text(html)
    result = llm.describe_business(ticker, filing_text)
    return {
        "ticker": ticker,
        "accession_no": filing["accession_no"],
        "filing_date": filing["filing_date"],
        "description": result["description"],
        "source_quote": result["source_quote"],
    }


@dg.asset(group_name="business_description")
def business_descriptions(
    context: dg.AssetExecutionContext,
    screening_candidates: pd.DataFrame,
    db: DBResource,
    edgar: EdgarResource,
    llm: dg.ResourceParam[FilingLLM],
) -> pd.DataFrame:
    existing = db.read_table_if_exists(TABLE)
    already_done = (
        set(zip(existing["ticker"], existing["accession_no"])) if not existing.empty else set()
    )

    new_rows = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {
            pool.submit(describe_ticker, context, t, edgar, llm, already_done): t
            for t in screening_candidates["ticker"]
        }
        for fut in as_completed(futures):
            ticker = futures[fut]
            try:
                result = fut.result()
                if result:
                    new_rows.append(result)
            except Exception as e:
                context.log.error(f"Business description failed for {ticker}: {e}")

    new_df = pd.DataFrame(new_rows, columns=COLUMNS)
    if not new_df.empty:
        engine = db.get_engine()
        new_df.to_sql(TABLE, engine, schema="stock_data", if_exists="append", index=False)

    combined = pd.concat([existing, new_df], ignore_index=True) if not existing.empty else new_df
    context.add_output_metadata(
        {
            "num_new_descriptions": len(new_df),
            "num_total_descriptions": len(combined),
            "num_candidates": len(screening_candidates),
        }
    )
    return combined
