from concurrent.futures import ThreadPoolExecutor, as_completed

import dagster as dg
import pandas as pd

from ..resources.db_resource import DBResource
from ..resources.edgar_resource import EdgarResource
from ..resources.llm_protocol import FilingLLM

TABLE = "lifo_fifo_classifications"

# Classification fields always written. The LIFO adjustment *bundle* below is
# all-or-nothing: if the step applies (lifo_reserve_usd is a number), every
# bundle column must be populated — a yes-flag with no adjusted book is a bug.
COLUMNS = [
    "ticker",
    "accession_no",
    "filing_date",
    "accounting_basis",
    "method",
    "lifo_reserve_disclosed",
    "lifo_reserve_usd",
    "method_change_disclosed",
    "quirk_notes",
    "confidence",
    "source_quote",
    "adjusted_bvps",
    "adjusted_pb_ratio",
]

# Columns that travel together when a LIFO reserve adjustment is applied.
LIFO_ADJUSTMENT_BUNDLE = (
    "lifo_reserve_usd",
    "adjusted_bvps",
    "adjusted_pb_ratio",
)


def apply_lifo_adjustment(
    classification: dict,
    price: float | None,
    book_value: float | None,
    shares: float | None,
) -> dict:
    """Fill or clear the LIFO adjustment bundle.

    Bundle applies only for US GAAP (or unknown) LIFO filers with a usable
    reserve dollar amount. IFRS forbids LIFO — never apply the adjustment.
    """
    basis_raw = classification.get("accounting_basis") or "unclear"
    basis = str(basis_raw).strip().upper().replace(" ", "_")
    if basis in ("US_GAAP", "USGAAP", "GAAP"):
        classification["accounting_basis"] = "US_GAAP"
    elif basis in ("IFRS", "IAS"):
        classification["accounting_basis"] = "IFRS"
    elif basis == "OTHER":
        classification["accounting_basis"] = "other"
    else:
        classification["accounting_basis"] = "unclear"

    if classification["accounting_basis"] == "IFRS":
        note = classification.get("quirk_notes") or ""
        suffix = (
            "IFRS reporting basis: LIFO not permitted; LIFO→FIFO book adjustment not applied."
        )
        classification["quirk_notes"] = f"{note} {suffix}".strip() if note else suffix
        classification["lifo_reserve_usd"] = None
        classification["adjusted_bvps"] = None
        classification["adjusted_pb_ratio"] = None
        return classification

    reserve = classification.get("lifo_reserve_usd")
    try:
        reserve_f = float(reserve) if reserve is not None else None
    except (TypeError, ValueError):
        reserve_f = None

    can_adjust = (
        reserve_f is not None
        and reserve_f > 0
        and price is not None
        and book_value is not None
        and shares is not None
        and float(shares) > 0
        and float(book_value) > 0
    )
    if not can_adjust:
        note = classification.get("quirk_notes") or ""
        if classification.get("lifo_reserve_disclosed") and reserve_f is None:
            suffix = "LIFO reserve mentioned but no dollar amount; adjustment not applied."
            classification["quirk_notes"] = f"{note} {suffix}".strip() if note else suffix
        elif reserve_f is not None and not can_adjust:
            suffix = "LIFO reserve found but price/book/shares missing; adjustment not applied."
            classification["quirk_notes"] = f"{note} {suffix}".strip() if note else suffix
        classification["lifo_reserve_usd"] = None
        classification["adjusted_bvps"] = None
        classification["adjusted_pb_ratio"] = None
        return classification

    adjusted_bvps = float(book_value) + reserve_f / float(shares)
    classification["lifo_reserve_usd"] = reserve_f
    classification["adjusted_bvps"] = adjusted_bvps
    classification["adjusted_pb_ratio"] = float(price) / adjusted_bvps if adjusted_bvps > 0 else None
    if classification["adjusted_pb_ratio"] is None:
        classification["lifo_reserve_usd"] = None
        classification["adjusted_bvps"] = None
        classification["adjusted_pb_ratio"] = None
    return classification


def classify_ticker(
    context: dg.AssetExecutionContext,
    ticker: str,
    edgar: EdgarResource,
    llm: FilingLLM,
    already_done: set[tuple[str, str]],
    valuation: dict | None = None,
    as_of: str | None = None,
) -> dict | None:
    """Shared by the live screener and the historical backtest: fetch a
    10-K/10-Q and have the LLM classify the inventory footnote. When valuation
    {price, book_value, shares} is provided and a reserve dollar amount is
    found, write the full LIFO adjustment bundle (reserve + adjusted book +
    adjusted P/B). as_of limits the filing to on-or-before that date."""
    cik = edgar.get_cik(ticker)
    if not cik:
        context.log.warning(f"No CIK found for {ticker}, skipping LIFO/FIFO check")
        return None
    filing = edgar.get_latest_filing(cik, as_of=as_of)
    if not filing:
        context.log.warning(
            f"No 10-K/10-Q found for {ticker} (CIK {cik}"
            + (f", as_of {as_of}" if as_of else "")
            + ")"
        )
        return None
    if (ticker, filing["accession_no"]) in already_done:
        return None  # already classified this exact filing — skip the LLM call
    html = edgar.fetch_filing_document(cik, filing["accession_no"], filing["primary_document"])
    filing_text = edgar.get_filing_text(html)
    # Send keyword windows only — full 10-K burns tokens for no gain on this task.
    focused = edgar.extract_keyword_windows(filing_text)
    context.log.info(
        f"{ticker}: filing_text={len(filing_text)} chars → inventory windows={len(focused)} chars"
    )
    classification = llm.classify_footnote(ticker, focused)
    classification.update(
        {
            "ticker": ticker,
            "accession_no": filing["accession_no"],
            "filing_date": filing["filing_date"],
        }
    )
    valuation = valuation or {}
    apply_lifo_adjustment(
        classification,
        price=valuation.get("price"),
        book_value=valuation.get("book_value"),
        shares=valuation.get("shares"),
    )
    return classification


def _needs_reclassify(existing: pd.DataFrame, ticker: str) -> bool:
    """Re-run LIFO rows that claimed a reserve but lack the dollar/bundle."""
    if existing.empty:
        return False
    rows = existing[existing["ticker"] == ticker]
    if rows.empty:
        return False
    latest = rows.sort_values("filing_date").iloc[-1]
    if latest.get("method") != "LIFO":
        return False
    reserve = latest.get("lifo_reserve_usd")
    if pd.isna(reserve) if reserve is not None else True:
        # Old row: disclosed true but no usd, or missing adjusted_bvps
        if latest.get("lifo_reserve_disclosed") is True:
            return True
        adj = latest.get("adjusted_bvps")
        if latest.get("lifo_reserve_disclosed") is True and (adj is None or (isinstance(adj, float) and pd.isna(adj))):
            return True
    return False


@dg.asset(group_name="lifo_fifo")
def lifo_fifo_classifications(
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
    # Force reclassify LIFO rows that are missing the adjustment bundle dollars.
    if not existing.empty and "lifo_reserve_usd" in existing.columns:
        for t in screening_candidates["ticker"]:
            if _needs_reclassify(existing, t):
                already_done = {pair for pair in already_done if pair[0] != t}
    elif not existing.empty and "lifo_reserve_usd" not in existing.columns:
        # Schema upgrade: old table has no usd column — reclassify LIFO disclosed rows.
        for _, row in existing.iterrows():
            if row.get("method") == "LIFO" and row.get("lifo_reserve_disclosed") is True:
                already_done = {pair for pair in already_done if pair[0] != row["ticker"]}

    cand_by_ticker = {
        r.ticker: {
            "price": getattr(r, "price", None),
            "book_value": getattr(r, "book_value", None),
            "shares": getattr(r, "shares", None),
        }
        for r in screening_candidates.itertuples()
    }

    new_rows = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {
            pool.submit(
                classify_ticker,
                context,
                t,
                edgar,
                llm,
                already_done,
                cand_by_ticker.get(t),
            ): t
            for t in screening_candidates["ticker"]
        }
        for fut in as_completed(futures):
            ticker = futures[fut]
            try:
                result = fut.result()
                if result:
                    new_rows.append(result)
            except Exception as e:
                context.log.error(f"LIFO/FIFO classification failed for {ticker}: {e}")

    new_df = pd.DataFrame(new_rows, columns=COLUMNS)
    if not new_df.empty:
        db.ensure_columns(
            TABLE,
            {
                "accounting_basis": "TEXT",
                "lifo_reserve_usd": "DOUBLE PRECISION",
                "adjusted_bvps": "DOUBLE PRECISION",
                "adjusted_pb_ratio": "DOUBLE PRECISION",
            },
        )
        engine = db.get_engine()
        # Replace path if schema gained columns — append still works when table
        # already has the new columns; for first upgrade after old schema,
        # ensure columns exist by writing combined.
        new_df.to_sql(TABLE, engine, schema="stock_data", if_exists="append", index=False)

    combined = pd.concat([existing, new_df], ignore_index=True) if not existing.empty else new_df
    # Align columns if existing was missing new fields
    for col in COLUMNS:
        if col not in combined.columns:
            combined[col] = None
    combined = combined[COLUMNS] if not combined.empty else pd.DataFrame(columns=COLUMNS)
    context.add_output_metadata(
        {
            "num_new_classifications": len(new_df),
            "num_total_classifications": len(combined),
            "num_candidates": len(screening_candidates),
            "num_with_lifo_adjustment": int(combined["adjusted_bvps"].notna().sum())
            if len(combined) and "adjusted_bvps" in combined.columns
            else 0,
        }
    )
    return combined
