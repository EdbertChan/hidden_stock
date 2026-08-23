"""Filing-date EOD×shares estimates for 13G names (not portfolio SoT).

Never write these into ``market_value_usd``. Cost basis is estimated at
``action=new``; ``mark_at_filing_est_usd`` tracks each period. Both stamp
``value_estimate=eod_at_filing`` + ``excluded_from_portfolio_mv``.
"""

from __future__ import annotations

from typing import Any, Callable

from .composition import append_composition_note, is_hk_residual_ticker
from .mtm import fetch_eodhd_price

_EXCHANGE_SUFFIX_ONLY = frozenset({"US", "HK", "KS", "JP", "CH", "LN", "SS", "SZ"})

_ESTIMATE_NOTE = (
    "value_estimate=eod_at_filing; not_fv_allocation; excluded_from_portfolio_mv"
)


def _f(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if x != x:  # NaN
        return None
    return x


def _ticker(row: dict) -> str:
    return str(row.get("investee_ticker") or "").strip().upper()


def _is_public_priceable(ticker: str) -> bool:
    if not ticker or ticker in _EXCHANGE_SUFFIX_ONLY:
        return False
    if ticker.startswith("PRIV_"):
        return False
    if is_hk_residual_ticker(ticker):
        return False
    return True


def fetch_close_on_or_before(
    ticker: str,
    as_of: str,
    *,
    fetch_eodhd: Callable[..., dict] | None = None,
) -> dict[str, Any]:
    """EODHD first; yfinance fallback. Soft-fail → null price (never invent)."""
    pe = str(as_of or "")[:10]
    if not ticker or not pe:
        return {"market_price": None, "price_as_of": None, "price_source": None}
    fetcher = fetch_eodhd or fetch_eodhd_price
    meta = fetcher(ticker, pe)
    px = _f(meta.get("market_price"))
    if px is not None and px > 0:
        return {
            "market_price": px,
            "price_as_of": str(meta.get("price_as_of") or pe)[:10],
            "price_source": meta.get("price_source") or "eodhd",
        }
    # yfinance fallback
    try:
        from datetime import date, timedelta

        import yfinance as yf

        symbol = ticker if "." in ticker else ticker
        d0 = date.fromisoformat(pe)
        start = (d0 - timedelta(days=21)).isoformat()
        end = (d0 + timedelta(days=2)).isoformat()
        hist = yf.Ticker(symbol).history(start=start, end=end, auto_adjust=True)
        if hist is not None and not hist.empty:
            dates = [str(d)[:10] for d in hist.index]
            eligible = [
                (d, float(hist["Close"].iloc[i]))
                for i, d in enumerate(dates)
                if d <= pe
            ]
            if not eligible and dates:
                eligible = [(dates[0], float(hist["Close"].iloc[0]))]
            if eligible:
                d, c = eligible[-1]
                if c > 0:
                    return {
                        "market_price": c,
                        "price_as_of": d,
                        "price_source": "yfinance",
                    }
    except Exception:
        pass
    return {
        "market_price": None,
        "price_as_of": None,
        "price_source": meta.get("price_source"),
    }


def apply_filing_mark_estimates(
    history_rows: list[dict],
    *,
    fetch_price: Callable[[str, str], dict[str, Any]] | None = None,
) -> list[dict]:
    """Fill cost_basis_est_* / mark_at_filing_est_usd; never touch market_value_usd.

    Eligibility: public ticker, positive shares, null disclosed/broker ``market_value_usd``.
    Cost basis set on ``action=new`` and carried on later holds; mark every eligible row.
    """
    if not history_rows:
        return history_rows

    price_fn = fetch_price or (
        lambda t, d: fetch_close_on_or_before(t, d)
    )
    # Cache (ticker, date) → price meta
    cache: dict[tuple[str, str], dict[str, Any]] = {}
    basis_by_ticker: dict[str, dict[str, Any]] = {}

    ordered = sorted(
        enumerate(history_rows),
        key=lambda it: (
            _ticker(it[1]),
            str(it[1].get("period_end") or "")[:10],
            it[0],
        ),
    )
    out = [dict(r) for r in history_rows]

    for orig_i, _src in ordered:
        row = out[orig_i]
        t = _ticker(row)
        action = str(row.get("action") or "").lower()
        if action == "exit" or not _is_public_priceable(t):
            continue
        shares = _f(row.get("shares_held"))
        if shares is None or shares <= 0:
            continue
        def _carry_basis() -> None:
            if t not in basis_by_ticker:
                return
            b = basis_by_ticker[t]
            row["cost_basis_est_usd"] = b.get("cost_basis_est_usd")
            row["cost_basis_est_price"] = b.get("cost_basis_est_price")
            row["cost_basis_est_as_of"] = b.get("cost_basis_est_as_of")
            row["cost_basis_est_source"] = b.get("cost_basis_est_source")
            if "excluded_from_portfolio_mv" not in str(row.get("note") or ""):
                row["note"] = append_composition_note(
                    row.get("note"),
                    f"{_ESTIMATE_NOTE}; estimate_role=cost_basis; "
                    f"cost_basis_est_source={row.get('cost_basis_est_source')}",
                )

        mv = _f(row.get("market_value_usd"))
        if mv is not None:
            # Disclosed / broker $ present — still carry prior cost basis if any.
            _carry_basis()
            continue

        pe = str(row.get("period_end") or row.get("filing_date") or "")[:10]
        if not pe:
            continue
        key = (t, pe)
        if key not in cache:
            cache[key] = price_fn(t, pe)
        meta = cache[key]
        px = _f(meta.get("market_price"))
        if px is None or px <= 0:
            _carry_basis()
            continue

        mark = shares * px
        row["mark_at_filing_est_usd"] = mark
        src = str(meta.get("price_source") or "eod")
        as_of = str(meta.get("price_as_of") or pe)[:10]

        if action == "new" or t not in basis_by_ticker:
            row["cost_basis_est_usd"] = mark
            row["cost_basis_est_price"] = px
            row["cost_basis_est_as_of"] = as_of
            row["cost_basis_est_source"] = src
            basis_by_ticker[t] = {
                "cost_basis_est_usd": mark,
                "cost_basis_est_price": px,
                "cost_basis_est_as_of": as_of,
                "cost_basis_est_source": src,
            }
            role = "cost_basis"
        else:
            b = basis_by_ticker[t]
            row["cost_basis_est_usd"] = b.get("cost_basis_est_usd")
            row["cost_basis_est_price"] = b.get("cost_basis_est_price")
            row["cost_basis_est_as_of"] = b.get("cost_basis_est_as_of")
            row["cost_basis_est_source"] = b.get("cost_basis_est_source")
            role = "mark_at_filing"

        row["note"] = append_composition_note(
            row.get("note"),
            f"{_ESTIMATE_NOTE}; estimate_role={role}; "
            f"cost_basis_est_source={row.get('cost_basis_est_source')}",
        )
        out[orig_i] = row

    return out
