import dagster as dg
import pandas as pd


@dg.asset(group_name="candidates")
def screening_candidates(
    context: dg.AssetExecutionContext, price_book_screen: pd.DataFrame
) -> pd.DataFrame:
    """Fan-in point: this is the small filtered list the expensive EDGAR +
    Claude step runs against, never the full universe. Kept as its own asset
    so future criteria (e.g. requiring is_index_dropout) can be added here
    without touching the LIFO/FIFO step."""
    candidates = price_book_screen.reset_index(drop=True)
    context.add_output_metadata({"num_candidates": len(candidates)})
    return candidates
