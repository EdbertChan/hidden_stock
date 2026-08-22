"""Ownership as-of a date: insiders (Form 3/4/5) + institutions (13F).

EODHD PercentInsiders / PercentInstitutions are current-only. For backtest:

- **Insiders:** reconstruct officer/director common positions from Forms 3/4/5
  filed on or before the deletion date (owners stale > LOOKBACK_YEARS dropped).
- **Institutions:** sum Form 13F share holdings for the latest quarter whose
  filings are public by the deletion date (filedAt ≤ as_of).
- **Non-institutions:** ``100 - percent_institutions`` (insiders + retail +
  other non-13F holders). This is the primary "neglected stock" ownership
  signal; insider % remains as a secondary skin-in-the-game check.

Live screener still uses EODHD SharesStats for all three percentages.
"""

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import dagster as dg
import diskcache
import pandas as pd
import requests
from dagster import AssetExecutionContext

from ..resources.db_resource import DBResource

TABLE = "insider_ownership"
BACKTEST_TABLE = "backtest_insider_ownership"
COLUMNS = [
    "ticker",
    "deletion_date",
    "as_of_date",
    "percent_insiders",
    "percent_institutions",
    "percent_non_institutions",
    "insider_shares",
    "institutional_shares",
    "shares_outstanding",
    "shares_float",
    "insider_owner_count",
    "institutional_holder_count",
    "filings_considered",
    "institutional_period",
    "source",
    "note",
]

LOOKBACK_YEARS = 3
_SEC_API_URL = "https://api.sec-api.io/insider-trading"
_SEC_13F_URL = "https://api.sec-api.io/form-13f/holdings"
_OWNERSHIP_FILTER = "SharesStats,General::UpdatedAt"
_eodhd_cache = diskcache.Cache(os.path.expanduser("~/.cache/hidden_stock/eodhd_ownership"))
_sec_cache = diskcache.Cache(os.path.expanduser("~/.cache/hidden_stock/sec_api_insiders"))
_shares_cache = diskcache.Cache(os.path.expanduser("~/.cache/hidden_stock/eodhd_shares_out"))
_sec_13f_cache = diskcache.Cache(os.path.expanduser("~/.cache/hidden_stock/sec_api_13f"))
_CACHE_TTL_SECONDS = 24 * 60 * 60


def _parse_shares_stats(payload: dict) -> dict:
    if "SharesStats" in payload and isinstance(payload["SharesStats"], dict):
        return payload["SharesStats"]
    if "PercentInsiders" in payload or "SharesOutstanding" in payload:
        return payload
    return {}


def fetch_eodhd_ownership_current(ticker: str) -> dict:
    """Today's EODHD SharesStats — live screener only."""
    cached = _eodhd_cache.get(f"current:{ticker}")
    if cached is not None:
        return cached

    api_key = os.environ.get("EODHD_API_KEY")
    if not api_key:
        raise RuntimeError("EODHD_API_KEY not set")

    resp = requests.get(
        f"https://eodhd.com/api/v1.1/fundamentals/{ticker}.US",
        params={"api_token": api_key, "filter": _OWNERSHIP_FILTER, "fmt": "json"},
        timeout=30,
    )
    if resp.status_code == 404:
        row = {c: None for c in COLUMNS}
        row.update(
            {
                "ticker": ticker,
                "source": "eodhd_shares_stats_current",
                "note": "EODHD fundamentals 404",
            }
        )
        _eodhd_cache.set(f"current:{ticker}", row, expire=_CACHE_TTL_SECONDS)
        return row

    resp.raise_for_status()
    data = resp.json()
    stats = _parse_shares_stats(data if isinstance(data, dict) else {})
    updated = data.get("General::UpdatedAt") or (data.get("General") or {}).get("UpdatedAt")
    row = {
        "ticker": ticker,
        "deletion_date": None,
        "as_of_date": updated,
        "percent_insiders": stats.get("PercentInsiders"),
        "percent_institutions": stats.get("PercentInstitutions"),
        "percent_non_institutions": None,
        "insider_shares": None,
        "institutional_shares": None,
        "shares_outstanding": stats.get("SharesOutstanding"),
        "shares_float": stats.get("SharesFloat"),
        "insider_owner_count": None,
        "institutional_holder_count": None,
        "filings_considered": None,
        "institutional_period": None,
        "source": "eodhd_shares_stats_current",
        "note": "current snapshot — live path only",
    }
    inst = stats.get("PercentInstitutions")
    if inst is not None:
        try:
            row["percent_non_institutions"] = max(0.0, min(100.0, 100.0 - float(inst)))
        except (TypeError, ValueError):
            pass
    _eodhd_cache.set(f"current:{ticker}", row, expire=_CACHE_TTL_SECONDS)
    return row


def _is_common_equity(title: str) -> bool:
    t = (title or "").lower()
    if any(x in t for x in ("preferred", " pref", "warrant", "option", "right", "unit", "debt")):
        return False
    return "common" in t or t.strip() in {"com", "common stock", "ordinary shares"}


def _shares_from_filing(filing: dict) -> float:
    nd = filing.get("nonDerivativeTable") or {}
    by_title: dict[str, float] = {}
    for h in nd.get("holdings") or []:
        title = h.get("securityTitle") or ""
        if not _is_common_equity(title):
            continue
        sh = (h.get("postTransactionAmounts") or {}).get("sharesOwnedFollowingTransaction")
        if sh is not None:
            try:
                by_title[title] = float(sh)
            except (TypeError, ValueError):
                pass
    for t in sorted(nd.get("transactions") or [], key=lambda x: x.get("transactionDate") or ""):
        title = t.get("securityTitle") or ""
        if not _is_common_equity(title):
            continue
        sh = (t.get("postTransactionAmounts") or {}).get("sharesOwnedFollowingTransaction")
        if sh is not None:
            try:
                by_title[title] = float(sh)
            except (TypeError, ValueError):
                pass
    return float(sum(by_title.values()))


def _is_officer_or_director(filing: dict) -> bool:
    rel = ((filing.get("reportingOwner") or {}).get("relationship")) or {}
    return bool(rel.get("isOfficer") or rel.get("isDirector"))


def _fetch_sec_filings(ticker: str, as_of: str) -> list:
    """All Form 3/4/5 for ticker filed on/before as_of. Cached per (ticker, as_of)."""
    cache_key = f"{ticker}|{as_of}"
    cached = _sec_cache.get(cache_key)
    if cached is not None:
        return cached

    api_key = os.environ.get("SEC_API_KEY")
    if not api_key:
        raise RuntimeError("SEC_API_KEY not set")

    rows = []
    frm = 0
    total = None
    while True:
        body = {
            "query": f"issuer.tradingSymbol:{ticker} AND filedAt:[2009-01-01 TO {as_of}]",
            "from": str(frm),
            "size": "50",
            "sort": [{"filedAt": {"order": "desc"}}],
        }
        data = None
        for attempt in range(6):
            resp = requests.post(
                _SEC_API_URL,
                headers={"Authorization": api_key, "Content-Type": "application/json"},
                json=body,
                timeout=60,
            )
            if resp.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            resp.raise_for_status()
            data = resp.json()
            break
        if data is None:
            raise RuntimeError("sec-api rate limited after retries")
        batch = data.get("transactions") or []
        rows.extend(batch)
        tot = data.get("total")
        if isinstance(tot, dict):
            total = tot.get("value")
        else:
            total = tot
        if not batch or len(batch) < 50:
            break
        frm += 50
        if total is not None and frm >= total:
            break
        if frm >= 5000:
            break
        time.sleep(0.35)  # polite pacing between pages

    _sec_cache.set(cache_key, rows, expire=_CACHE_TTL_SECONDS)
    return rows


def _shares_outstanding_as_of(ticker: str, as_of: str):
    """Nearest EODHD quarterly outstanding shares on or before as_of.
    Returns (shares, dateFormatted) or (None, None)."""
    cache_key = f"{ticker}|{as_of}"
    cached = _shares_cache.get(cache_key)
    if cached is not None:
        return cached

    api_key = os.environ.get("EODHD_API_KEY")
    if not api_key:
        return None, None

    resp = requests.get(
        f"https://eodhd.com/api/v1.1/fundamentals/{ticker}.US",
        params={"api_token": api_key, "filter": "outstandingShares", "fmt": "json"},
        timeout=30,
    )
    if resp.status_code == 404:
        _shares_cache.set(cache_key, (None, None), expire=_CACHE_TTL_SECONDS)
        return None, None
    resp.raise_for_status()
    data = resp.json()
    quarterly = data.get("quarterly") if isinstance(data, dict) else None
    if not isinstance(quarterly, dict):
        _shares_cache.set(cache_key, (None, None), expire=_CACHE_TTL_SECONDS)
        return None, None

    best_date = None
    best_shares = None
    for v in quarterly.values():
        if not isinstance(v, dict):
            continue
        dt = v.get("dateFormatted")
        if not dt or str(dt) > as_of:
            continue
        try:
            shares = float(v.get("shares"))
        except (TypeError, ValueError):
            continue
        if best_date is None or str(dt) > best_date:
            best_date = str(dt)
            best_shares = shares

    result = (best_shares, best_date)
    _shares_cache.set(cache_key, result, expire=_CACHE_TTL_SECONDS)
    return result


def _13f_period_for_as_of(as_of: str) -> str:
    """Latest calendar quarter-end that 13Fs would typically have filed by as_of.

    Managers have 45 days after quarter-end to file. We take the last quarter-end
    that is at least 45 days before as_of.
    """
    d = datetime.strptime(str(as_of)[:10], "%Y-%m-%d").date() - timedelta(days=45)
    # Snap to prior calendar quarter-end.
    q_month = ((d.month - 1) // 3) * 3 + 3
    q_year = d.year
    if d.month <= 3:
        # before/on Q1 end handling: if still in Q1 after snap logic
        pass
    # Quarter ends: Mar 31, Jun 30, Sep 30, Dec 31 on or before d
    candidates = []
    for year in range(d.year - 1, d.year + 1):
        for month, day in ((3, 31), (6, 30), (9, 30), (12, 31)):
            candidates.append(datetime(year, month, day).date())
    prior = [c for c in candidates if c <= d]
    if not prior:
        return f"{d.year - 1}-12-31"
    return max(prior).isoformat()


def _shares_from_13f_holding(h: dict, ticker: str) -> float:
    if (h.get("ticker") or "").upper() != ticker.upper():
        return 0.0
    if h.get("putCall"):
        return 0.0
    title = (h.get("titleOfClass") or "").upper()
    if "PUT" in title or "CALL" in title:
        return 0.0
    amt = (h.get("shrsOrPrnAmt") or {}).get("sshPrnamt")
    typ = (h.get("shrsOrPrnAmt") or {}).get("sshPrnamtType")
    if typ and str(typ).upper() != "SH":
        return 0.0
    try:
        return float(amt or 0)
    except (TypeError, ValueError):
        return 0.0


def _institutional_from_13f(ticker: str, as_of: str, period: str | None = None, _depth: int = 0) -> dict:
    """Sum 13F common-share holdings for ticker as of the latest filed quarter.

    Returns institutional_shares, holder_count, period, note.
    Cached per (ticker, period, as_of).
    """
    period = period or _13f_period_for_as_of(as_of)
    cache_key = f"{ticker}|{period}|{as_of}"
    cached = _sec_13f_cache.get(cache_key)
    if cached is not None:
        return cached

    api_key = os.environ.get("SEC_API_KEY")
    empty = {
        "institutional_shares": None,
        "institutional_holder_count": 0,
        "institutional_period": period,
        "note": None,
    }
    if not api_key:
        empty["note"] = "SEC_API_KEY not set"
        return empty

    by_cik: dict[str, tuple[str, float]] = {}
    frm = 0
    total = None
    try:
        while True:
            body = {
                "query": (
                    f"holdings.ticker:{ticker} AND periodOfReport:{period} "
                    f"AND filedAt:[2000-01-01 TO {as_of}]"
                ),
                "from": str(frm),
                "size": "50",
                "sort": [{"filedAt": {"order": "desc"}}],
            }
            data = None
            for attempt in range(6):
                resp = requests.post(
                    _SEC_13F_URL,
                    headers={"Authorization": api_key, "Content-Type": "application/json"},
                    json=body,
                    timeout=120,
                )
                if resp.status_code == 429:
                    time.sleep(2 ** attempt)
                    continue
                resp.raise_for_status()
                data = resp.json()
                break
            if data is None:
                raise RuntimeError("sec-api 13F rate limited after retries")

            batch = data.get("data") or []
            tot = data.get("total")
            if isinstance(tot, dict):
                total = tot.get("value")
            else:
                total = tot

            for filing in batch:
                cik = str(filing.get("cik") or "").strip()
                if not cik:
                    continue
                filed = (filing.get("filedAt") or "")[:10]
                shares = 0.0
                for h in filing.get("holdings") or []:
                    shares += _shares_from_13f_holding(h, ticker)
                prev = by_cik.get(cik)
                if prev is None or filed > prev[0]:
                    by_cik[cik] = (filed, shares)

            if not batch or len(batch) < 50:
                break
            frm += 50
            if total is not None and frm >= total:
                break
            if frm >= 10000:
                break
            time.sleep(0.25)
    except Exception as e:
        empty["note"] = f"13F error: {e}"
        _sec_13f_cache.set(cache_key, empty, expire=_CACHE_TTL_SECONDS)
        return empty

    if not by_cik:
        if _depth < 1:
            try:
                p = datetime.strptime(period, "%Y-%m-%d").date()
                prev_period = _13f_period_for_as_of((p - timedelta(days=1)).isoformat())
            except ValueError:
                prev_period = None
            if prev_period and prev_period != period:
                result = _institutional_from_13f(ticker, as_of, period=prev_period, _depth=_depth + 1)
                _sec_13f_cache.set(cache_key, result, expire=_CACHE_TTL_SECONDS)
                return result
        empty["note"] = f"no 13F holders for {ticker} period {period} filed ≤ {as_of}"
        _sec_13f_cache.set(cache_key, empty, expire=_CACHE_TTL_SECONDS)
        return empty

    inst_shares = float(sum(sh for _, sh in by_cik.values()))
    out = {
        "institutional_shares": inst_shares,
        "institutional_holder_count": len(by_cik),
        "institutional_period": period,
        "note": f"13F sum over {len(by_cik)} managers, period {period}, filed ≤ {as_of}",
    }
    _sec_13f_cache.set(cache_key, out, expire=_CACHE_TTL_SECONDS)
    return out


def ownership_as_of_deletion(ticker: str, deletion_date: str) -> dict:
    """Insider (Form 4) + institutional (13F) + non-institutional % as-of deletion."""
    as_of = str(deletion_date)[:10]
    empty = {
        "ticker": ticker,
        "deletion_date": as_of,
        "as_of_date": as_of,
        "percent_insiders": None,
        "percent_institutions": None,
        "percent_non_institutions": None,
        "insider_shares": None,
        "institutional_shares": None,
        "shares_outstanding": None,
        "shares_float": None,
        "insider_owner_count": 0,
        "institutional_holder_count": 0,
        "filings_considered": 0,
        "institutional_period": None,
        "source": "sec_api_form4_and_13f",
        "note": None,
    }
    notes: list[str] = []

    # --- Insiders (Form 3/4/5) ---
    try:
        filings = _fetch_sec_filings(ticker, as_of)
    except Exception as e:
        notes.append(f"Form4 error: {e}")
        filings = []

    empty["filings_considered"] = len(filings)
    latest = {}
    for f in filings:
        if not _is_officer_or_director(f):
            continue
        owner = f.get("reportingOwner") or {}
        oid = str(owner.get("cik") or owner.get("name") or "").strip()
        if not oid:
            continue
        filed = (f.get("filedAt") or "")[:10]
        if not filed:
            continue
        if oid not in latest or filed > latest[oid][0]:
            latest[oid] = (filed, f)

    cutoff = datetime.strptime(as_of, "%Y-%m-%d").date() - timedelta(days=365 * LOOKBACK_YEARS)
    insider_shares = 0.0
    owners = 0
    for filed, f in latest.values():
        try:
            filed_d = datetime.strptime(filed, "%Y-%m-%d").date()
        except ValueError:
            continue
        if filed_d < cutoff:
            continue
        sh = _shares_from_filing(f)
        if sh > 0:
            insider_shares += sh
            owners += 1

    shares_out, shares_as_of = _shares_outstanding_as_of(ticker, as_of)
    empty["insider_shares"] = insider_shares if owners else None
    empty["insider_owner_count"] = owners
    empty["shares_outstanding"] = shares_out

    if shares_out and shares_out > 0 and owners:
        pct = 100.0 * insider_shares / shares_out
        empty["percent_insiders"] = min(pct, 100.0)
        notes.append(
            f"Form4 officers+directors; shares_out EODHD {shares_as_of}"
            + (f"; raw_insider_pct={pct:.1f} capped" if pct > 100 else "")
        )
    elif owners and not shares_out:
        notes.append("have insider shares but no EODHD shares outstanding as-of")
    elif not filings:
        notes.append("no Form 3/4/5 filings on/before as_of")
    else:
        notes.append("no active officer/director Form 4 common positions within lookback")

    # --- Institutions (13F) ---
    inst = _institutional_from_13f(ticker, as_of)
    empty["institutional_shares"] = inst.get("institutional_shares")
    empty["institutional_holder_count"] = inst.get("institutional_holder_count") or 0
    empty["institutional_period"] = inst.get("institutional_period")
    if inst.get("note"):
        notes.append(inst["note"])

    inst_sh = empty["institutional_shares"]
    if shares_out and shares_out > 0 and inst_sh is not None:
        inst_pct = 100.0 * float(inst_sh) / shares_out
        # Cap at 100 — 13F can double-count across managers.
        empty["percent_institutions"] = min(inst_pct, 100.0)
        empty["percent_non_institutions"] = max(0.0, 100.0 - empty["percent_institutions"])
        if inst_pct > 100:
            notes.append(f"raw_inst_pct={inst_pct:.1f} capped at 100")

    empty["note"] = "; ".join(notes) if notes else None
    return empty


def _ownership_error_row(ticker: str, deletion_date: str, err: Exception) -> dict:
    return {
        "ticker": ticker,
        "deletion_date": deletion_date,
        "as_of_date": deletion_date,
        "percent_insiders": None,
        "percent_institutions": None,
        "percent_non_institutions": None,
        "insider_shares": None,
        "institutional_shares": None,
        "shares_outstanding": None,
        "shares_float": None,
        "insider_owner_count": 0,
        "institutional_holder_count": 0,
        "filings_considered": 0,
        "institutional_period": None,
        "source": "sec_api_form4_and_13f",
        "note": f"error: {err}",
    }

@dg.asset(group_name="insider_ownership")
def insider_ownership(
    context: AssetExecutionContext,
    screening_candidates: pd.DataFrame,
    db: DBResource,
) -> pd.DataFrame:
    """Live path: today's EODHD ownership for PB-screened candidates."""
    existing = db.read_table_if_exists(TABLE)
    already = set(existing["ticker"]) if not existing.empty else set()
    tickers = (
        screening_candidates["ticker"].dropna().astype(str).unique().tolist()
        if not screening_candidates.empty
        else []
    )
    todo = [t for t in tickers if t not in already]
    new_rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(fetch_eodhd_ownership_current, t): t for t in todo}
        for fut in as_completed(futures):
            ticker = futures[fut]
            try:
                new_rows.append(fut.result())
            except Exception as e:
                context.log.error(f"EODHD ownership failed for {ticker}: {e}")

    new_df = pd.DataFrame(new_rows, columns=COLUMNS)
    if not new_df.empty:
        new_df.to_sql(TABLE, db.get_engine(), schema="stock_data", if_exists="append", index=False)

    combined = pd.concat([existing, new_df], ignore_index=True) if not existing.empty else new_df
    for col in COLUMNS:
        if col not in combined.columns:
            combined[col] = None
    combined = combined[COLUMNS] if not combined.empty else pd.DataFrame(columns=COLUMNS)
    context.add_output_metadata({"num_rows": len(combined), "num_new": len(new_df)})
    return combined


@dg.asset(group_name="backtest")
def backtest_insider_ownership(
    context: AssetExecutionContext,
    historical_deletions: pd.DataFrame,
    db: DBResource,
) -> pd.DataFrame:
    """Backtest: Form 4 insiders + 13F institutions / non-institutions as-of deletion."""
    existing = db.read_table_if_exists(BACKTEST_TABLE)
    # Schema upgrade: old Form4-only rows lack percent_non_institutions — rebuild those.
    if not existing.empty and "percent_non_institutions" not in existing.columns:
        existing = pd.DataFrame(columns=COLUMNS)

    already: set[tuple[str, str]] = set()
    if not existing.empty and "deletion_date" in existing.columns:
        have = existing[existing["percent_non_institutions"].notna()] if len(existing) else existing
        already = set(
            zip(have["ticker"].astype(str), have["deletion_date"].astype(str).str[:10])
        )

    if historical_deletions.empty:
        empty = pd.DataFrame(columns=COLUMNS)
        empty.to_sql(BACKTEST_TABLE, db.get_engine(), schema="stock_data", if_exists="replace", index=False)
        return empty

    pairs = (
        historical_deletions[["ticker", "reconstitution_date"]]
        .dropna()
        .assign(
            ticker=lambda d: d["ticker"].astype(str),
            reconstitution_date=lambda d: d["reconstitution_date"].astype(str).str[:10],
        )
        .drop_duplicates()
    )
    todo = [
        (r.ticker, r.reconstitution_date)
        for r in pairs.itertuples()
        if (r.ticker, r.reconstitution_date) not in already
    ]
    context.log.info(f"ownership todo={len(todo)} with_non_inst={len(already)}")

    new_rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {
            pool.submit(ownership_as_of_deletion, t, d): (t, d) for t, d in todo
        }
        for fut in as_completed(futures):
            t, d = futures[fut]
            try:
                new_rows.append(fut.result())
            except Exception as e:
                context.log.error(f"ownership failed for {t} @{d}: {e}")
                new_rows.append(_ownership_error_row(t, d, e))

    new_df = pd.DataFrame(new_rows, columns=COLUMNS)
    if not existing.empty and not new_df.empty:
        keys = set(zip(new_df["ticker"].astype(str), new_df["deletion_date"].astype(str).str[:10]))
        mask = ~existing.apply(
            lambda r: (str(r["ticker"]), str(r["deletion_date"])[:10]) in keys, axis=1
        )
        existing = existing.loc[mask]

    combined = pd.concat([existing, new_df], ignore_index=True) if not existing.empty else new_df
    for col in COLUMNS:
        if col not in combined.columns:
            combined[col] = None
    combined = combined[COLUMNS] if not combined.empty else pd.DataFrame(columns=COLUMNS)
    combined.to_sql(BACKTEST_TABLE, db.get_engine(), schema="stock_data", if_exists="replace", index=False)
    context.add_output_metadata(
        {
            "num_rows": len(combined),
            "num_new": len(new_df),
            "num_with_insiders": int(combined["percent_insiders"].notna().sum()) if len(combined) else 0,
            "num_with_non_inst": int(combined["percent_non_institutions"].notna().sum())
            if len(combined)
            else 0,
        }
    )
    return combined
