"""Export equity holdings to CSV and Google Sheets."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from .history import HISTORY_COLUMNS, build_holdings_history
from .parents import normalize_parent
from .runner import process_parent_holdings
from .rollup import rollup_holdings
from .schema import HOLDINGS_COLUMNS

SHEET_POSITIONS = "positions_qoq"
SHEET_CURRENT = "current_holdings"
SHEET_PORTFOLIO = "portfolio_by_period"
SHEET_HOLDINGS_QOQ_CHART = "holdings_qoq_chart"
SHEET_RETURNS = "returns_by_period"
SHEET_REALIZED = "realized_pnl_qoq"
SHEET_HOLDING_RETURNS = "holding_returns"
SHEET_REPORTED_VS_EST = "reported_vs_est"
# chart_data tab removed — named stack lives on holdings_qoq_chart (calendar quarters).
# Thin returns_chart / realized_*_chart tabs removed — Dietz embeds on returns_by_period.
# hk_composition tab removed — composition_* columns live on positions_qoq.

# Sheets already scaled to $ millions (not raw USD).
_SHEETS_IN_MILLIONS = frozenset(
    {
        SHEET_HOLDINGS_QOQ_CHART,
    }
)

# Raw USD ledger columns → display as $X.XXB (value unchanged).
_USD_COL = re.compile(
    r"(?:^|_)(market_value_usd|value_prev|value_delta|carrying_usd|"
    r"fair_value_disclosed_usd|suggested_adj_usd|lookthrough_mtm_usd|"
    r"realized_usd|realized_pnl|market_vs_carrying|"
    r"cost_basis_est_usd|mark_at_filing_est_usd|parent_aggregate_market_value_usd)"
    r"(?:$|_)",
    re.I,
)
_SHARES_COL = re.compile(r"shares", re.I)
_PCT_COL = re.compile(r"(ownership_pct|return_pct|dietz|_pct$|cagr|growth)", re.I)
_MN_COL = re.compile(r"(_mn|_usd_mn|hkd_mn|mkt_cap)", re.I)

# 13F values that blow up the stacked chart (likely unit/parse artifacts).
CHART_EXCLUDE_TICKERS = frozenset({"MRDB"})
# At-a-glance named stack: latest-period top N only (rest omitted, no OTHER).
HOLDINGS_QOQ_CHART_TOP_N = 7

POSITIONS_COLS = [
    "period_end",
    "investee_ticker",
    "investee_name",
    "cusip",
    "action",
    "shares_held",
    "ownership_pct",
    "shares_prev",
    "shares_delta",
    "market_value_usd",
    "value_prev",
    "value_delta",
    "filing_date",
    "accession_no",
    "filing_url",
    "first_seen_period",
    "exited_period",
    "composition_parent",
    "composition_as_of",
    "child_value_source",
    "parent_aggregate_market_value_usd",
    "cost_basis_est_usd",
    "cost_basis_est_price",
    "cost_basis_est_as_of",
    "cost_basis_est_source",
    "mark_at_filing_est_usd",
    "note",
]


def engine_from_env() -> Engine:
    return create_engine(
        f"postgresql+psycopg2://{os.environ['POSTGRES_USER']}:{os.environ['POSTGRES_PASSWORD']}"
        f"@{os.environ['POSTGRES_HOST']}:{os.environ['POSTGRES_PORT']}/{os.environ['POSTGRES_DB']}"
    )


def refresh_current(parent: str, edgar: Any, *, engine: Engine | None = None) -> pd.DataFrame:
    """Refresh live holdings for one parent into Postgres (full-table replace for that run)."""
    eng = engine or engine_from_env()
    rows, meta = process_parent_holdings(
        parent_ticker=parent, edgar=edgar, llm=None, use_llm_fallback=False
    )
    hold = pd.DataFrame(rows, columns=HOLDINGS_COLUMNS) if rows else pd.DataFrame(columns=HOLDINGS_COLUMNS)
    roll = rollup_holdings(rows)
    roll_df = pd.DataFrame(
        [
            {
                **roll,
                "ticker": parent,
                "as_of_date": meta.get("filing_date"),
                "accession_no": meta.get("accession_no"),
                "extract_error": meta.get("error"),
            }
        ]
    )
    # Preserve other parents already in the table when present.
    try:
        existing = pd.read_sql("SELECT * FROM stock_data.equity_holdings", eng)
        existing = existing[existing["parent_ticker"].astype(str).str.upper() != parent]
        hold_out = pd.concat([existing, hold], ignore_index=True) if len(existing) else hold
    except Exception:
        hold_out = hold
    try:
        existing_r = pd.read_sql("SELECT * FROM stock_data.equity_holdings_parent_rollups", eng)
        existing_r = existing_r[existing_r["ticker"].astype(str).str.upper() != parent]
        roll_out = pd.concat([existing_r, roll_df], ignore_index=True) if len(existing_r) else roll_df
    except Exception:
        roll_out = roll_df
    hold_out.to_sql("equity_holdings", eng, schema="stock_data", if_exists="replace", index=False)
    roll_out.to_sql(
        "equity_holdings_parent_rollups", eng, schema="stock_data", if_exists="replace", index=False
    )
    return hold


def refresh_history(
    parent: str,
    edgar: Any,
    *,
    max_filings: int = 80,
    lookback_years: int = 5,
    engine: Engine | None = None,
) -> pd.DataFrame:
    eng = engine or engine_from_env()
    rows, _meta = build_holdings_history(
        parent_ticker=parent,
        edgar=edgar,
        max_filings=max_filings,
        lookback_years=lookback_years,
    )
    hist = pd.DataFrame(rows, columns=HISTORY_COLUMNS) if rows else pd.DataFrame(columns=HISTORY_COLUMNS)
    try:
        existing = pd.read_sql("SELECT * FROM stock_data.equity_holdings_history", eng)
        existing = existing[existing["parent_ticker"].astype(str).str.upper() != parent]
        out = pd.concat([existing, hist], ignore_index=True) if len(existing) else hist
    except Exception:
        out = hist
    out.to_sql("equity_holdings_history", eng, schema="stock_data", if_exists="replace", index=False)
    return hist


def load_current(parent: str, engine: Engine | None = None) -> pd.DataFrame:
    eng = engine or engine_from_env()
    try:
        return pd.read_sql(
            "SELECT * FROM stock_data.equity_holdings WHERE parent_ticker = %(p)s",
            eng,
            params={"p": parent},
        )
    except Exception:
        return pd.DataFrame(columns=HOLDINGS_COLUMNS)


def load_history(parent: str, engine: Engine | None = None) -> pd.DataFrame:
    eng = engine or engine_from_env()
    try:
        return pd.read_sql(
            "SELECT * FROM stock_data.equity_holdings_history WHERE parent_ticker = %(p)s",
            eng,
            params={"p": parent},
        )
    except Exception:
        return pd.DataFrame(columns=HISTORY_COLUMNS)


def positions_qoq_frame(hist: pd.DataFrame) -> pd.DataFrame:
    if hist.empty:
        return pd.DataFrame(columns=POSITIONS_COLS)
    from .edgar_urls import edgar_filing_index_url
    from .runner import PARENT_CIK_OVERRIDES

    _KNOWN_CIK = {"UBER": "0001543151", "BABA": "0001577552"}
    records = hist.to_dict(orient="records")
    for r in records:
        url = r.get("filing_url")
        if url and str(url) not in ("", "nan", "None"):
            continue
        parent = str(r.get("parent_ticker") or "").upper()
        cik = PARENT_CIK_OVERRIDES.get(parent) or _KNOWN_CIK.get(parent)
        r["filing_url"] = edgar_filing_index_url(cik, r.get("accession_no"))
    out = pd.DataFrame(records)
    for c in POSITIONS_COLS:
        if c not in out.columns:
            out[c] = None
    out = out[POSITIONS_COLS]
    return out.sort_values(["period_end", "investee_ticker"], kind="mergesort").reset_index(drop=True)


def portfolio_by_period_frame(hist: pd.DataFrame) -> pd.DataFrame:
    """Long format ready for stacked bar: period_end × investee_ticker × market_value_usd.

    Rows with missing market_value are dropped (missing ≠ $0).
    HK composition residuals are excluded (mirror parent $ — no double-count).
    """
    if hist.empty:
        return pd.DataFrame(columns=["period_end", "investee_ticker", "market_value_usd"])
    df = hist.copy()
    df = df[df["action"].astype(str) != "exit"].copy()
    try:
        from .composition import excluded_from_portfolio_mv

        mask = [
            not excluded_from_portfolio_mv(r) for r in df.to_dict(orient="records")
        ]
        df = df.iloc[[i for i, ok in enumerate(mask) if ok]].copy()
    except Exception:
        pass
    df["investee_ticker"] = df["investee_ticker"].fillna("").astype(str).str.upper()
    blank = df["investee_ticker"] == ""
    if blank.any() and "investee_name" in df.columns:
        df.loc[blank, "investee_ticker"] = (
            df.loc[blank, "investee_name"].fillna("UNKNOWN").astype(str)
        )
    elif blank.any():
        df.loc[blank, "investee_ticker"] = "UNKNOWN"
    # Drop residual tickers even if note filter missed.
    df = df[~df["investee_ticker"].astype(str).str.endswith("_RESIDUAL")].copy()
    df["market_value_usd"] = pd.to_numeric(df["market_value_usd"], errors="coerce")
    df = df[df["market_value_usd"].notna()].copy()
    if df.empty:
        return pd.DataFrame(columns=["period_end", "investee_ticker", "market_value_usd"])
    out = (
        df.groupby(["period_end", "investee_ticker"], as_index=False)["market_value_usd"]
        .sum()
        .sort_values(["period_end", "investee_ticker"], kind="mergesort")
        .reset_index(drop=True)
    )
    return out


def display_stack_by_period_frame(hist: pd.DataFrame) -> pd.DataFrame:
    """Named QoQ sizing for chart display — not portfolio / Dietz SoT.

    Prefer a **stable** series so calendar quarters do not cliff when broker
    overlays land:

    1. ``mark_at_filing_est_usd`` (EOD×shares at filing)
    2. else non-broker ``market_value_usd`` (13F / disclosed)
    3. else broker ``market_value_usd`` (last resort)

    Mixing (2)/(3) ahead of (1) caused PDD 2023-03 / 2026-06 display cliffs.
    Skips ``PRIV_*``, residuals, exits. Never invents.
    """
    cols = ["period_end", "investee_ticker", "display_value_usd"]
    if hist is None or hist.empty:
        return pd.DataFrame(columns=cols)
    df = hist.copy()
    df = df[df["action"].astype(str).str.lower() != "exit"].copy()
    df["investee_ticker"] = df["investee_ticker"].fillna("").astype(str).str.upper()
    blank = df["investee_ticker"] == ""
    if blank.any() and "investee_name" in df.columns:
        df.loc[blank, "investee_ticker"] = (
            df.loc[blank, "investee_name"].fillna("UNKNOWN").astype(str).str.upper()
        )
    elif blank.any():
        df.loc[blank, "investee_ticker"] = "UNKNOWN"
    t = df["investee_ticker"].astype(str)
    df = df[
        ~t.str.startswith("PRIV_")
        & ~t.str.endswith("_RESIDUAL")
        & (t != "")
        & (t != "NAN")
    ].copy()
    if df.empty:
        return pd.DataFrame(columns=cols)

    mv = pd.to_numeric(df.get("market_value_usd"), errors="coerce")
    mark = (
        pd.to_numeric(df.get("mark_at_filing_est_usd"), errors="coerce")
        if "mark_at_filing_est_usd" in df.columns
        else pd.Series(float("nan"), index=df.index)
    )
    if "note" in df.columns:
        note = df["note"].fillna("").astype(str)
    else:
        note = pd.Series("", index=df.index, dtype=str)
    is_broker = note.str.contains("value_source=broker_sotp", regex=False)
    disclosed = mv.where(~is_broker)
    broker = mv.where(is_broker)
    df["display_value_usd"] = mark.where(mark.notna(), disclosed)
    df["display_value_usd"] = df["display_value_usd"].where(
        df["display_value_usd"].notna(), broker
    )
    df = df[df["display_value_usd"].notna()].copy()
    if df.empty:
        return pd.DataFrame(columns=cols)
    out = (
        df.groupby(["period_end", "investee_ticker"], as_index=False)["display_value_usd"]
        .sum()
        .sort_values(["period_end", "investee_ticker"], kind="mergesort")
        .reset_index(drop=True)
    )
    return out


def _wide_stack_chart(
    long: pd.DataFrame,
    *,
    value_col: str,
    top_n: int | None = None,
    exclude_tickers: frozenset[str] | None = None,
) -> pd.DataFrame:
    """Pivot long period×ticker×$ to wide $M chart matrix sorted by latest size."""
    if long is None or long.empty:
        return pd.DataFrame(columns=["period_end"])
    port = long.copy()
    skip = exclude_tickers if exclude_tickers is not None else CHART_EXCLUDE_TICKERS
    port = port[~port["investee_ticker"].astype(str).str.upper().isin(skip)].copy()
    if port.empty or value_col not in port.columns:
        return pd.DataFrame(columns=["period_end"])

    latest = str(sorted(port["period_end"].astype(str).unique())[-1])
    latest_vals = (
        port[port["period_end"].astype(str) == latest]
        .groupby("investee_ticker")[value_col]
        .sum()
        .sort_values(ascending=False)
    )
    if top_n is not None:
        keep = set(latest_vals.head(int(top_n)).index)
        port = port[port["investee_ticker"].isin(keep)].copy()
        latest_vals = latest_vals.head(int(top_n))
    series = list(latest_vals.index)
    earlier = [
        t
        for t in port["investee_ticker"].astype(str).unique()
        if t and t.upper() != "NAN" and t not in series
    ]
    earlier.sort(
        key=lambda t: -float(port[port["investee_ticker"] == t][value_col].max() or 0)
    )
    series = series + earlier
    agg = port.groupby(["period_end", "investee_ticker"], as_index=False)[value_col].sum()
    wide = (
        agg.pivot(index="period_end", columns="investee_ticker", values=value_col)
        .reindex(columns=series)
        .fillna(0.0)
        .reset_index()
    )
    wide.columns.name = None
    for c in series:
        wide[c] = (pd.to_numeric(wide[c], errors="coerce").fillna(0.0) / 1e6).round(2)
    return wide


def quarterly_display_stack_frame(hist: pd.DataFrame) -> pd.DataFrame:
    """Calendar quarter-end snapshots of named display `$` (not filing-event grain).

    For each quarter-end (3/31, 6/30, 9/30, 12/31): last display value with
    ``period_end ≤`` that quarter while the name is still open (no exit yet).
    """
    cols = ["period_end", "investee_ticker", "display_value_usd"]
    disp = display_stack_by_period_frame(hist)
    if disp is None or disp.empty:
        return pd.DataFrame(columns=cols)

    exit_by: dict[str, str] = {}
    if hist is not None and not hist.empty and "action" in hist.columns:
        ex = hist[hist["action"].astype(str).str.lower() == "exit"].copy()
        if not ex.empty:
            ex["investee_ticker"] = (
                ex["investee_ticker"].fillna("").astype(str).str.upper()
            )
            for _, r in ex.iterrows():
                t = str(r.get("investee_ticker") or "")
                pe = str(r.get("exited_period") or r.get("period_end") or "")[:10]
                if t and pe and (t not in exit_by or pe < exit_by[t]):
                    exit_by[t] = pe

    pe_min = str(disp["period_end"].astype(str).min())[:10]
    pe_max = str(disp["period_end"].astype(str).max())[:10]
    today = pd.Timestamp.today().normalize()
    # Last *closed* calendar quarter-end on or before today (never invent the
    # in-progress quarter — pandas QuarterEnd(0) rolls *forward*).
    closed = pd.date_range(end=today, periods=1, freq="QE")
    last_closed = closed[0] if len(closed) else today
    # Extend pe_max to its containing quarter-end, then clamp to last_closed.
    data_q = pd.Timestamp(pe_max) + pd.offsets.QuarterEnd(0)
    pe_cap = min(data_q, last_closed)
    pe_min_ts = min(pd.Timestamp(pe_min), pe_cap)
    try:
        q_ends = pd.date_range(start=pe_min_ts, end=pe_cap, freq="QE")
    except ValueError:
        q_ends = pd.date_range(start=pe_min_ts, end=pe_cap, freq="Q")
    q_ends = q_ends[q_ends <= last_closed]
    if len(q_ends) == 0:
        q_ends = pd.DatetimeIndex([last_closed])

    disp = disp.sort_values(["investee_ticker", "period_end"], kind="mergesort")
    out_rows: list[dict] = []
    tickers = sorted(disp["investee_ticker"].astype(str).unique())
    for q in q_ends:
        q_str = q.strftime("%Y-%m-%d")
        for t in tickers:
            if t in exit_by and exit_by[t] <= q_str:
                continue
            sub = disp[
                (disp["investee_ticker"].astype(str) == t)
                & (disp["period_end"].astype(str).str[:10] <= q_str)
            ]
            if sub.empty:
                continue
            val = float(sub.iloc[-1]["display_value_usd"])
            out_rows.append(
                {
                    "period_end": q_str,
                    "investee_ticker": t,
                    "display_value_usd": val,
                }
            )
    if not out_rows:
        return pd.DataFrame(columns=cols)
    return (
        pd.DataFrame(out_rows)
        .groupby(["period_end", "investee_ticker"], as_index=False)["display_value_usd"]
        .sum()
        .sort_values(["period_end", "investee_ticker"], kind="mergesort")
        .reset_index(drop=True)
    )


def holdings_qoq_chart_frame(
    hist: pd.DataFrame,
    *,
    top_n: int | None = HOLDINGS_QOQ_CHART_TOP_N,
    exclude_tickers: frozenset[str] | None = None,
) -> pd.DataFrame:
    """Wide $M matrix for ``holdings_qoq_chart`` — calendar quarters, top-N named sizing.

    Default ``top_n=7`` (latest quarter by size) so gains are readable at a glance.
    Pass ``top_n=None`` for the full named universe.
    """
    qlong = quarterly_display_stack_frame(hist)
    if not qlong.empty:
        wide = _wide_stack_chart(
            qlong,
            value_col="display_value_usd",
            top_n=top_n,
            exclude_tickers=exclude_tickers,
        )
        if not wide.empty:
            wide.attrs["chart_basis"] = "display_estimate_or_broker"
            wide.attrs["chart_top_n"] = top_n
            return wide
    # UBER-style fallback: quarter-resample portfolio SoT if no display marks.
    port = portfolio_by_period_frame(hist)
    if port.empty:
        return pd.DataFrame(columns=["period_end"])
    # Treat portfolio rows as display for quarterly resample via a synthetic hist.
    synth = port.rename(columns={"market_value_usd": "mark_at_filing_est_usd"})
    synth["action"] = "hold"
    synth["market_value_usd"] = port["market_value_usd"]
    q2 = quarterly_display_stack_frame(synth)
    # quarterly_display_stack prefers mark_at_filing_est; synth sets both — OK
    if q2.empty:
        wide = _wide_stack_chart(
            port,
            value_col="market_value_usd",
            top_n=top_n,
            exclude_tickers=exclude_tickers,
        )
        if not wide.empty:
            wide.attrs["chart_basis"] = "portfolio_sot"
            wide.attrs["chart_top_n"] = top_n
        return wide
    wide = _wide_stack_chart(
        q2,
        value_col="display_value_usd",
        top_n=top_n,
        exclude_tickers=exclude_tickers,
    )
    if not wide.empty:
        wide.attrs["chart_basis"] = "portfolio_sot"
        wide.attrs["chart_top_n"] = top_n
    return wide


def chart_data_frame(
    hist: pd.DataFrame,
    *,
    top_n: int | None = None,
    exclude_tickers: frozenset[str] | None = None,
) -> pd.DataFrame:
    """Deprecated alias — prefer ``holdings_qoq_chart_frame`` (calendar quarters)."""
    return holdings_qoq_chart_frame(
        hist, top_n=top_n, exclude_tickers=exclude_tickers
    )


def write_csvs(
    parent: str,
    hold: pd.DataFrame,
    hist: pd.DataFrame,
    out_dir: Path | str,
    *,
    lookback_start: str | None = None,
) -> dict[str, Path]:
    from .history import assert_unique_period_ticker
    from .validate import (
        assert_estimates_not_in_market_value,
        assert_live_shares_held_sane,
        scrub_live_pct_as_shares,
    )

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    # Refuse to ship Sheets/CSV when QoQ history doubles a ticker in one period
    # (SERV exit+13G miss after AUR-only CI).
    if hist is not None and len(hist):
        from .lookback import coalesce_period_ticker, normalize_ticker

        # Stamp CUSIP/name → ticker and coalesce 13F+13G alias collisions before assert.
        hist = hist.copy()
        hist = pd.DataFrame(coalesce_period_ticker(hist.to_dict(orient="records")))
        if "investee_ticker" in hist.columns:
            hist["investee_ticker"] = hist["investee_ticker"].map(normalize_ticker)
        hist_recs = hist.to_dict(orient="records")
        assert_unique_period_ticker(
            hist_recs,
            context=f"export/{parent}",
        )
        assert_estimates_not_in_market_value(
            hist_recs, context=f"export/{parent}/history"
        )
    if hold is not None and len(hold):
        cleaned = scrub_live_pct_as_shares(hold.to_dict(orient="records"))
        assert_live_shares_held_sane(cleaned, context=f"export/{parent}/current")
        assert_estimates_not_in_market_value(
            cleaned, context=f"export/{parent}/current"
        )
        hold = pd.DataFrame(cleaned)
    slug = parent.lower().replace("-", "")

    def _window(df: pd.DataFrame) -> pd.DataFrame:
        if df is None or df.empty or not lookback_start or "period_end" not in df.columns:
            return df
        pe = df["period_end"].astype(str).str[:10]
        return df[pe >= str(lookback_start)[:10]].copy()

    # positions_qoq keeps inception backfill rows for first_seen / lot audit.
    positions = positions_qoq_frame(hist)
    # Chart + portfolio stay on the display window.
    portfolio = portfolio_by_period_frame(_window(hist))
    qoq_chart = holdings_qoq_chart_frame(_window(hist))
    from .performance import performance_frames

    # Lots/Dietz use full (inception-extended) history; display tabs window-filtered.
    perf = performance_frames(
        hist if hist is not None else pd.DataFrame(),
        parent=parent,
    )
    returns_by_period = _window(perf["returns_by_period"])
    paths = {
        "current": out / f"{slug}_equity_holdings.csv",
        "history": out / f"{slug}_equity_holdings_history.csv",
        "portfolio": out / f"{slug}_portfolio_by_period.csv",
        "holdings_qoq_chart": out / f"{slug}_holdings_qoq_chart.csv",
        "returns_by_period": out / f"{slug}_returns_by_period.csv",
        "realized_pnl_qoq": out / f"{slug}_realized_pnl_qoq.csv",
        "holding_returns": out / f"{slug}_holding_returns.csv",
        "reported_vs_est": out / f"{slug}_reported_vs_est.csv",
    }
    hold.to_csv(paths["current"], index=False)
    positions.to_csv(paths["history"], index=False)
    portfolio.to_csv(paths["portfolio"], index=False)
    qoq_chart.to_csv(paths["holdings_qoq_chart"], index=False)
    returns_by_period.to_csv(paths["returns_by_period"], index=False)
    perf["realized_pnl_qoq"].to_csv(paths["realized_pnl_qoq"], index=False)
    perf["holding_returns"].to_csv(paths["holding_returns"], index=False)
    perf["reported_vs_est"].to_csv(paths["reported_vs_est"], index=False)
    return paths


def _load_service_account_info() -> dict:
    raw = os.environ.get("GOOGLE_SHEETS_CREDENTIALS_JSON", "").strip()
    if not raw:
        raise RuntimeError(
            "GOOGLE_SHEETS_CREDENTIALS_JSON is unset (path to service-account JSON or raw JSON)"
        )
    if raw.startswith("{"):
        return json.loads(raw)
    path = Path(raw).expanduser()
    if not path.is_file():
        raise RuntimeError(f"GOOGLE_SHEETS_CREDENTIALS_JSON path not found: {path}")
    return json.loads(path.read_text())


def _env_truthy(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _oauth_paths() -> tuple[Path | None, Path | None]:
    """Desktop OAuth client secrets + authorized_user token (creates sheets as you)."""
    secrets = os.environ.get("GOOGLE_SHEETS_OAUTH_CLIENT_SECRETS", "").strip()
    token = os.environ.get("GOOGLE_SHEETS_OAUTH_TOKEN", "").strip()
    secrets_p = Path(secrets).expanduser() if secrets else None
    token_p = Path(token).expanduser() if token else None
    return secrets_p, token_p


def _gspread_client(*, prefer_user: bool = False):
    """Return (gspread client, identity_email, auth_mode).

    auth_mode is ``oauth`` (your Drive) or ``service_account`` (0 personal Drive quota).
    Prefer OAuth when creating new experiment sheets.
    """
    import gspread
    from google.oauth2.service_account import Credentials

    secrets_p, token_p = _oauth_paths()
    if prefer_user and secrets_p and secrets_p.is_file() and token_p and token_p.is_file():
        gc = gspread.oauth(
            credentials_filename=str(secrets_p),
            authorized_user_filename=str(token_p),
        )
        email = ""
        try:
            email = gc.http_client.credentials.client_id  # type: ignore[attr-defined]
        except Exception:
            email = "oauth-user"
        try:
            from google.oauth2.credentials import Credentials as UserCreds

            creds = UserCreds.from_authorized_user_file(str(token_p))
            # id_token / token info may not have email; leave label.
            if getattr(creds, "token", None):
                email = "oauth-user"
        except Exception:
            pass
        return gc, email, "oauth"

    info = _load_service_account_info()
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    return gspread.authorize(creds), info.get("client_email"), "service_account"


def _df_to_rows(df: pd.DataFrame) -> list[list[Any]]:
    if df.empty:
        return [[]]
    clean = df.where(pd.notnull(df), "")
    header = [str(c) for c in clean.columns]
    body: list[list[Any]] = []
    for _, series in clean.iterrows():
        row: list[Any] = []
        for v in series.tolist():
            if v == "" or v is None:
                row.append("")
            elif isinstance(v, bool):
                row.append(v)
            elif isinstance(v, (int, float)):
                if isinstance(v, float) and (pd.isna(v) or abs(v) == float("inf")):
                    row.append("")
                else:
                    row.append(v)
            else:
                # numpy scalars
                try:
                    if pd.isna(v):
                        row.append("")
                        continue
                except (TypeError, ValueError):
                    pass
                if hasattr(v, "item"):
                    try:
                        row.append(v.item())
                        continue
                    except Exception:
                        pass
                row.append(v)
        body.append(row)
    return [header, *body]


def _number_format_for_column(col: str, *, sheet: str) -> str | None:
    """Google Sheets numberFormat pattern for a column (display only)."""
    c = str(col)
    # Returns Dietz columns are unitless fractions / already-*100 — never append "%".
    if sheet == SHEET_RETURNS and c in {
        "dietz_return",
        "dietz_return_pct",
        "cum_dietz_growth",
        "cum_growth_index",
    }:
        return "0.00"
    if sheet in _SHEETS_IN_MILLIONS:
        if c in {"period_end", "investee_ticker", "investee_name"}:
            return None
        return '$#,##0.0"M"'
    if _PCT_COL.search(c) or c.startswith("stake_pct_"):
        return '0.00"%"'
    if _USD_COL.search(c):
        # 136595348061 → $136.60B (underlying value unchanged)
        return '$#,##0.00,,,"B"'
    if _SHARES_COL.search(c):
        return "#,##0"
    if _MN_COL.search(c):
        return "#,##0.0"
    return None


def _apply_worksheet_number_formats(ws, df: pd.DataFrame, *, sheet: str) -> None:
    if df is None or df.empty:
        return
    reqs: list[dict] = []
    nrows = len(df) + 1
    for i, col in enumerate(df.columns):
        pattern = _number_format_for_column(str(col), sheet=sheet)
        if not pattern:
            continue
        reqs.append(
            {
                "repeatCell": {
                    "range": {
                        "sheetId": ws.id,
                        "startRowIndex": 1,
                        "endRowIndex": nrows,
                        "startColumnIndex": i,
                        "endColumnIndex": i + 1,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "numberFormat": {"type": "NUMBER", "pattern": pattern}
                        }
                    },
                    "fields": "userEnteredFormat.numberFormat",
                }
            }
        )
    reqs.append(
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": ws.id,
                    "gridProperties": {"frozenRowCount": 1},
                },
                "fields": "gridProperties.frozenRowCount",
            }
        }
    )
    ws.spreadsheet.batch_update({"requests": reqs})


def _replace_worksheet(sh, title: str, df: pd.DataFrame) -> None:
    try:
        ws = sh.worksheet(title)
        sh.del_worksheet(ws)
    except Exception:
        pass
    rows = _df_to_rows(df)
    nrows = max(len(rows), 1)
    ncols = max(len(rows[0]) if rows and rows[0] else len(df.columns) or 1, 1)
    ws = sh.add_worksheet(title=title, rows=nrows + 10, cols=ncols + 2)
    if rows and rows[0]:
        ws.update(rows, value_input_option="USER_ENTERED")
        try:
            _apply_worksheet_number_formats(ws, df, sheet=title)
        except Exception:
            pass


def _gspread_credentials(gc):
    """Pull google-auth credentials off a gspread client (SA or OAuth)."""
    http = getattr(gc, "http_client", None) or getattr(gc, "client", None)
    for attr in ("auth", "credentials", "creds"):
        if http is not None and getattr(http, attr, None) is not None:
            return getattr(http, attr)
    if hasattr(gc, "auth"):
        return gc.auth
    raise RuntimeError("could not read credentials from gspread client")


def _upsert_qoq_stacked_chart(
    spreadsheet_id: str,
    gc,
    chart_df: pd.DataFrame,
    *,
    chart_basis: str | None = None,
    sheet_title: str = SHEET_HOLDINGS_QOQ_CHART,
) -> None:
    """Stacked column chart on ``holdings_qoq_chart`` (calendar quarters)."""
    if chart_df.empty or len(chart_df.columns) < 2:
        return
    from googleapiclient.discovery import build

    creds = _gspread_credentials(gc)
    svc = build("sheets", "v4", credentials=creds)
    meta = svc.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    sheet_id = None
    delete_reqs: list[dict] = []
    for s in meta.get("sheets", []):
        props = s.get("properties") or {}
        if props.get("title") == sheet_title:
            sheet_id = props.get("sheetId")
            for ch in s.get("charts") or []:
                delete_reqs.append({"deleteEmbeddedObject": {"objectId": ch["chartId"]}})
            break
    if sheet_id is None:
        return

    nrows = len(chart_df) + 1
    ncols = len(chart_df.columns)
    series = [
        {
            "series": {
                "sourceRange": {
                    "sources": [
                        {
                            "sheetId": sheet_id,
                            "startRowIndex": 0,
                            "endRowIndex": nrows,
                            "startColumnIndex": i,
                            "endColumnIndex": i + 1,
                        }
                    ]
                }
            },
            "targetAxis": "LEFT_AXIS",
        }
        for i in range(1, ncols)
    ]
    basis = chart_basis or getattr(chart_df, "attrs", {}).get("chart_basis")
    if basis == "display_estimate_or_broker":
        title = "Top holdings by calendar quarter ($M)"
        subtitle = (
            f"top {HOLDINGS_QOQ_CHART_TOP_N} by latest size; "
            "basis=display_estimate_or_broker; not_portfolio_sot; "
            "calendar QoQ (not 13G filing dates)"
        )
    else:
        title = "Holdings market value by calendar quarter ($M)"
        subtitle = "calendar QoQ"
    spec: dict[str, Any] = {
        "title": title,
        "subtitle": subtitle,
        "basicChart": {
            "chartType": "COLUMN",
            "legendPosition": "RIGHT_LEGEND",
            "stackedType": "STACKED",
            "headerCount": 1,
            "domains": [
                {
                    "domain": {
                        "sourceRange": {
                            "sources": [
                                {
                                    "sheetId": sheet_id,
                                    "startRowIndex": 0,
                                    "endRowIndex": nrows,
                                    "startColumnIndex": 0,
                                    "endColumnIndex": 1,
                                }
                            ]
                        }
                    }
                }
            ],
            "series": series,
        },
    }
    # Own tab: pin chart at top-left with a small offset so headers stay readable.
    add_req = {
        "addChart": {
            "chart": {
                "spec": spec,
                "position": {
                    "overlayPosition": {
                        "anchorCell": {
                            "sheetId": sheet_id,
                            "rowIndex": 0,
                            "columnIndex": 0,
                        },
                        "offsetXPixels": 0,
                        "offsetYPixels": 40,
                        "widthPixels": 1200,
                        "heightPixels": 560,
                    }
                },
            }
        }
    }
    svc.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id, body={"requests": [*delete_reqs, add_req]}
    ).execute()


def _returns_sheet_frame(returns_by_period: pd.DataFrame) -> pd.DataFrame:
    """returns_by_period plus dietz_return_pct for the Dietz combo chart."""
    if returns_by_period is None or returns_by_period.empty:
        return returns_by_period if returns_by_period is not None else pd.DataFrame()
    out = returns_by_period.copy()
    if "dietz_return" in out.columns and "dietz_return_pct" not in out.columns:
        out["dietz_return_pct"] = (
            pd.to_numeric(out["dietz_return"], errors="coerce") * 100.0
        )
        cols = list(out.columns)
        cols.remove("dietz_return_pct")
        i = cols.index("dietz_return") + 1
        cols.insert(i, "dietz_return_pct")
        out = out[cols]
    return out


def _upsert_returns_combo_chart(
    spreadsheet_id: str,
    gc,
    returns_df: pd.DataFrame,
    *,
    cagr: float | None = None,
    years: float | None = None,
) -> None:
    """Combo chart on returns_by_period: Dietz % (columns) + cum growth (line)."""
    from .performance import linked_dietz_cagr

    if returns_df is None or returns_df.empty or "period_end" not in returns_df.columns:
        return
    data = returns_df[
        returns_df["period_end"].astype(str).str.match(r"^\d{4}-\d{2}-\d{2}", na=False)
    ].copy()
    if data.empty:
        return
    cols = [str(c) for c in data.columns]
    pct_col = "dietz_return_pct" if "dietz_return_pct" in cols else None
    if pct_col is None and "dietz_return" in cols:
        data["dietz_return_pct"] = (
            pd.to_numeric(data["dietz_return"], errors="coerce") * 100.0
        )
        pct_col = "dietz_return_pct"
        cols = [str(c) for c in data.columns]
    growth_col = (
        "cum_dietz_growth"
        if "cum_dietz_growth" in cols
        else ("cum_growth_index" if "cum_growth_index" in cols else None)
    )
    if pct_col is None or growth_col is None:
        return

    domain_i = cols.index("period_end")
    pct_i = cols.index(pct_col)
    growth_i = cols.index(growth_col)

    from googleapiclient.discovery import build

    creds = _gspread_credentials(gc)
    svc = build("sheets", "v4", credentials=creds)
    meta = svc.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    sheet_id = None
    delete_reqs: list[dict] = []
    for s in meta.get("sheets", []):
        props = s.get("properties") or {}
        if props.get("title") == SHEET_RETURNS:
            sheet_id = props.get("sheetId")
            for ch in s.get("charts") or []:
                delete_reqs.append({"deleteEmbeddedObject": {"objectId": ch["chartId"]}})
            break
    if sheet_id is None:
        return

    nrows = len(data) + 1
    if cagr is None:
        info = linked_dietz_cagr(
            pd.DataFrame(
                {
                    "period_end": data["period_end"],
                    "dietz_return": data[pct_col].apply(
                        lambda x: float(x) / 100.0 if x is not None and x == x else None
                    ),
                    "cum_dietz_growth": data[growth_col],
                }
            )
        )
        cagr = info.get("cagr")
        years = info.get("years")
    if cagr is not None and years is not None:
        subtitle = (
            f"CAGR {cagr * 100:.1f}% over {years:.2f}y (linked Dietz; estimated)"
        )
    else:
        subtitle = "CAGR n/a (linked Dietz; estimated)"

    def _col_series(col_index: int, series_type: str, axis: str) -> dict:
        return {
            "series": {
                "sourceRange": {
                    "sources": [
                        {
                            "sheetId": sheet_id,
                            "startRowIndex": 0,
                            "endRowIndex": nrows,
                            "startColumnIndex": col_index,
                            "endColumnIndex": col_index + 1,
                        }
                    ]
                }
            },
            "targetAxis": axis,
            "type": series_type,
        }

    add_req = {
        "addChart": {
            "chart": {
                "spec": {
                    "title": "Estimated QoQ Dietz return (%)",
                    "subtitle": subtitle,
                    "basicChart": {
                        "chartType": "COMBO",
                        "legendPosition": "BOTTOM_LEGEND",
                        "headerCount": 1,
                        "domains": [
                            {
                                "domain": {
                                    "sourceRange": {
                                        "sources": [
                                            {
                                                "sheetId": sheet_id,
                                                "startRowIndex": 0,
                                                "endRowIndex": nrows,
                                                "startColumnIndex": domain_i,
                                                "endColumnIndex": domain_i + 1,
                                            }
                                        ]
                                    }
                                }
                            }
                        ],
                        "series": [
                            _col_series(pct_i, "COLUMN", "LEFT_AXIS"),
                            _col_series(growth_i, "LINE", "RIGHT_AXIS"),
                        ],
                    },
                },
                "position": {
                    "overlayPosition": {
                        "anchorCell": {
                            "sheetId": sheet_id,
                            "rowIndex": 0,
                            "columnIndex": max(len(cols), 4),
                        },
                        "widthPixels": 900,
                        "heightPixels": 480,
                    }
                },
            }
        }
    }
    svc.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id, body={"requests": [*delete_reqs, add_req]}
    ).execute()


def _create_spreadsheet(gc, title: str, *, folder_id: str | None = None):
    """Create a new spreadsheet; folder_id should be a Shared Drive folder for SAs."""
    if folder_id:
        return gc.create(title, folder_id=folder_id)
    return gc.create(title)


def push_google_sheets(
    hold: pd.DataFrame,
    hist: pd.DataFrame,
    *,
    parent: str | None = None,
    spreadsheet_id: str | None = None,
    title: str = "equity holdings",
    create_new: bool | None = None,
    lookback_start: str | None = None,
) -> dict[str, str]:
    """Write data tabs + stacked QoQ chart. By default creates a **new** spreadsheet each call.

    Service accounts have 0 Drive quota, so new sheets need either:
    - user OAuth token (``GOOGLE_SHEETS_OAUTH_*``), or
    - a Shared Drive folder (``GOOGLE_SHEETS_DRIVE_FOLDER_ID``) shared with the SA.

    Set ``create_new=False`` (or ``GOOGLE_SHEETS_CREATE_NEW=0``) to reuse
    ``GOOGLE_SHEETS_SPREADSHEET_ID`` / ``spreadsheet_id``.
    """
    from datetime import datetime, timezone

    if create_new is None:
        create_new = _env_truthy("GOOGLE_SHEETS_CREATE_NEW", default=True)

    folder_id = (os.environ.get("GOOGLE_SHEETS_DRIVE_FOLDER_ID") or "").strip() or None
    sid_arg = (spreadsheet_id or "").strip()
    sid_env = (os.environ.get("GOOGLE_SHEETS_SPREADSHEET_ID") or "").strip()

    prefer_user = create_new or bool(_oauth_paths()[1] and _oauth_paths()[1].is_file())
    gc, email, auth_mode = _gspread_client(prefer_user=prefer_user)

    created = False
    if create_new:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H%M")
        full_title = f"{title} · {stamp}"
        try:
            sh = _create_spreadsheet(gc, full_title, folder_id=folder_id)
            sid = sh.id
            created = True
        except Exception as e:
            raise RuntimeError(
                "Could not create a new Google Spreadsheet. Service accounts have 0 Drive "
                "quota. One-time fix: run `python scripts/google_sheets_oauth_setup.py` and set "
                "GOOGLE_SHEETS_OAUTH_CLIENT_SECRETS + GOOGLE_SHEETS_OAUTH_TOKEN (creates sheets "
                "in your Drive), OR set GOOGLE_SHEETS_DRIVE_FOLDER_ID to a Shared Drive folder "
                f"shared with {email!r}. create error: {e}"
            ) from e
    else:
        sid = sid_arg or sid_env
        if not sid:
            raise RuntimeError(
                "create_new=False but no spreadsheet_id / GOOGLE_SHEETS_SPREADSHEET_ID set"
            )
        sh = gc.open_by_key(sid)

    positions = positions_qoq_frame(hist)

    def _window(df: pd.DataFrame) -> pd.DataFrame:
        if df is None or df.empty or not lookback_start or "period_end" not in df.columns:
            return df
        pe = df["period_end"].astype(str).str[:10]
        return df[pe >= str(lookback_start)[:10]].copy()

    windowed = _window(hist)
    portfolio = portfolio_by_period_frame(windowed)
    qoq_chart = holdings_qoq_chart_frame(windowed)
    from .performance import linked_dietz_cagr, performance_frames

    perf = performance_frames(hist, parent=parent)
    returns_by_period = _returns_sheet_frame(_window(perf["returns_by_period"]))
    _replace_worksheet(sh, SHEET_POSITIONS, positions)
    _replace_worksheet(sh, SHEET_CURRENT, hold)
    _replace_worksheet(sh, SHEET_PORTFOLIO, portfolio)
    _replace_worksheet(sh, SHEET_HOLDINGS_QOQ_CHART, qoq_chart)
    _replace_worksheet(sh, SHEET_RETURNS, returns_by_period)
    _replace_worksheet(sh, SHEET_REALIZED, perf["realized_pnl_qoq"])
    _replace_worksheet(sh, SHEET_HOLDING_RETURNS, perf["holding_returns"])
    _replace_worksheet(sh, SHEET_REPORTED_VS_EST, perf["reported_vs_est"])

    # Drop legacy thin chart / composition tabs if present.
    for legacy_title in (
        "hk_composition",
        "chart_data",
        "returns_chart",
        "realized_chart",
        "realized_by_ticker_chart",
    ):
        try:
            legacy = sh.worksheet(legacy_title)
            sh.del_worksheet(legacy)
        except Exception:
            pass

    chart_ok = "true"
    returns_chart_ok = "true"
    try:
        _upsert_qoq_stacked_chart(
            sid,
            gc,
            qoq_chart,
            chart_basis=getattr(qoq_chart, "attrs", {}).get("chart_basis"),
            sheet_title=SHEET_HOLDINGS_QOQ_CHART,
        )
    except Exception as e:
        chart_ok = f"false:{e}"
    try:
        cagr_info = linked_dietz_cagr(perf["returns_by_period"])
        _upsert_returns_combo_chart(
            sid,
            gc,
            returns_by_period,
            cagr=cagr_info.get("cagr"),
            years=cagr_info.get("years"),
        )
    except Exception as e:
        returns_chart_ok = f"false:{e}"

    if created:
        try:
            default = sh.worksheet("Sheet1")
            if len(sh.worksheets()) > 1:
                sh.del_worksheet(default)
        except Exception:
            pass

    url = f"https://docs.google.com/spreadsheets/d/{sid}"
    return {
        "spreadsheet_id": sid,
        "url": url,
        "auth": auth_mode,
        "identity": email or "",
        "created": str(created).lower(),
        "title": sh.title if hasattr(sh, "title") else title,
        "chart": chart_ok,
        "returns_chart": returns_chart_ok,
    }


def export_parent(
    parent: str,
    *,
    refresh: bool = True,
    history: bool = True,
    max_filings: int = 80,
    lookback_years: int = 5,
    out_dir: Path | str = "exports",
    push_sheets: bool = True,
    spreadsheet_id: str | None = None,
    create_new: bool | None = None,
    edgar: Any | None = None,
) -> dict[str, Any]:
    """Refresh Postgres (optional), write CSVs, optionally push Sheets tabs."""
    parent = normalize_parent(parent)
    eng = engine_from_env()
    if refresh or history:
        if edgar is None:
            from hidden_stock.resources.edgar_resource import EdgarResource

            edgar = EdgarResource(user_agent=os.environ["SEC_EDGAR_USER_AGENT"])
    from .lookback import lookback_start_date

    lookback_start = lookback_start_date(lookback_years=lookback_years)
    if history:
        hist = refresh_history(
            parent,
            edgar,
            max_filings=max_filings,
            lookback_years=lookback_years,
            engine=eng,
        )
        from .composition import live_holdings_from_history
        from .rollup import rollup_holdings

        hold = pd.DataFrame(
            live_holdings_from_history(hist.to_dict(orient="records"))
        )
        # Persist live slice alongside history (single SoT).
        try:
            existing = pd.read_sql("SELECT * FROM stock_data.equity_holdings", eng)
            existing = existing[
                existing["parent_ticker"].astype(str).str.upper() != parent
            ]
            hold_out = (
                pd.concat([existing, hold], ignore_index=True) if len(existing) else hold
            )
            hold_out.to_sql(
                "equity_holdings", eng, schema="stock_data", if_exists="replace", index=False
            )
            roll = rollup_holdings(hold.to_dict(orient="records"))
            as_of = None
            if "as_of_date" in hold.columns and len(hold):
                as_of = hold["as_of_date"].astype(str).max()
            roll_df = pd.DataFrame(
                [
                    {
                        **roll,
                        "ticker": parent,
                        "as_of_date": as_of,
                        "accession_no": None,
                        "extract_error": None,
                    }
                ]
            )
            try:
                existing_r = pd.read_sql(
                    "SELECT * FROM stock_data.equity_holdings_parent_rollups", eng
                )
                existing_r = existing_r[
                    existing_r["ticker"].astype(str).str.upper() != parent
                ]
                roll_out = (
                    pd.concat([existing_r, roll_df], ignore_index=True)
                    if len(existing_r)
                    else roll_df
                )
            except Exception:
                roll_out = roll_df
            roll_out.to_sql(
                "equity_holdings_parent_rollups",
                eng,
                schema="stock_data",
                if_exists="replace",
                index=False,
            )
        except Exception:
            pass
    else:
        hold = (
            refresh_current(parent, edgar, engine=eng)
            if refresh
            else load_current(parent, eng)
        )
        hist = load_history(parent, eng)
        if hist is not None and not hist.empty:
            from .composition import live_holdings_from_history

            hold = pd.DataFrame(
                live_holdings_from_history(hist.to_dict(orient="records"))
            )
    paths = write_csvs(parent, hold, hist, out_dir, lookback_start=lookback_start)
    result: dict[str, Any] = {
        "parent": parent,
        "num_current": len(hold),
        "num_history": len(hist),
        "lookback_years": lookback_years,
        "lookback_start": lookback_start,
        "csv": {k: str(v) for k, v in paths.items()},
    }
    if push_sheets:
        try:
            sheets = push_google_sheets(
                hold,
                hist,
                parent=parent,
                spreadsheet_id=spreadsheet_id,
                title=f"{parent} equity holdings",
                create_new=create_new,
                lookback_start=lookback_start,
            )
            result["sheets"] = sheets
        except Exception as e:
            result["sheets_error"] = str(e)
    return result
