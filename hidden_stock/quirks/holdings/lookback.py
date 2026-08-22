"""Calendar lookback helpers for holdings history fan-out."""

from __future__ import annotations

import math
from datetime import date


def lookback_start_date(*, as_of: str | None = None, lookback_years: int = 5) -> str:
    """ISO date ``(as_of or today) − lookback_years`` (handles Feb 29).

    ``lookback_years=0`` means no calendar cut (full book from 2000-01-01).
    """
    years = max(0, int(lookback_years))
    if years == 0:
        return "2000-01-01"
    if as_of:
        end = date.fromisoformat(str(as_of)[:10])
    else:
        end = date.today()
    try:
        start = end.replace(year=end.year - years)
    except ValueError:
        start = end.replace(year=end.year - years, day=28)
    return start.isoformat()


def date_on_or_after(value: str | None, start: str) -> bool:
    if not value:
        return False
    return str(value)[:10] >= str(start)[:10]


def normalize_ticker(value) -> str | None:
    """Coerce investee_ticker to uppercase str, or None.

    Pandas turns missing tickers into ``float('nan')``; that is truthy, so
    ``(ticker or "").strip()`` raises AttributeError (BABA export crash).
    """
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    try:
        import pandas as pd

        if pd.isna(value):
            return None
    except Exception:
        pass
    s = str(value).strip()
    if not s or s.lower() in {"nan", "none", "null"}:
        return None
    return s.upper()


def stamp_missing_tickers(rows: list[dict]) -> list[dict]:
    """Fill blank investee_ticker from CUSIP map then investee name aliases.

    Ensures history/export uniqueness is on real tickers (not all-NaN collisions)
    and 13G XPeng merges onto 13F XPEV.
    """
    from .extract import load_investee_aliases, resolve_investee_ticker
    from .parse_13f import load_cusip_tickers

    cusip_map = load_cusip_tickers()
    aliases = load_investee_aliases()
    out: list[dict] = []
    for r in rows:
        row = dict(r)
        t = normalize_ticker(row.get("investee_ticker"))
        if not t:
            cusip = str(row.get("_cusip") or row.get("cusip") or "").strip().upper()
            if cusip and cusip in cusip_map:
                t = cusip_map[cusip]
            else:
                t = resolve_investee_ticker(row.get("investee_name"), None, aliases)
            if t:
                row["investee_ticker"] = t
        else:
            row["investee_ticker"] = t
        out.append(row)
    return out


def _mv_rank(row: dict) -> float:
    try:
        v = float(row.get("market_value_usd"))
        return v if v == v else -1.0
    except (TypeError, ValueError):
        return -1.0


def coalesce_period_ticker(rows: list[dict]) -> list[dict]:
    """One row per (period_end, ticker): prefer finite $, else denser note/shares.

    Safety net after stamp_missing_tickers so 13F+13G alias collisions cannot
    ship as duplicate period×ticker rows.
    """
    stamped = stamp_missing_tickers(rows)
    buckets: dict[tuple[str, str], list[dict]] = {}
    passthrough: list[dict] = []
    for r in stamped:
        pe = str(r.get("period_end") or "")
        t = normalize_ticker(r.get("investee_ticker"))
        if not pe or not t:
            passthrough.append(r)
            continue
        buckets.setdefault((pe, t), []).append(r)

    out: list[dict] = list(passthrough)
    for (_pe, _t), group in buckets.items():
        if len(group) == 1:
            out.append(group[0])
            continue
        # Prefer row with market value; then non-exit; then longer note
        def score(row: dict) -> tuple:
            note = str(row.get("note") or "")
            has_13f = 1 if "13f" in note.lower() else 0
            action = str(row.get("action") or "")
            non_exit = 0 if action == "exit" else 1
            return (_mv_rank(row), has_13f, non_exit, len(note))

        group_sorted = sorted(group, key=score, reverse=True)
        winner = dict(group_sorted[0])
        for other in group_sorted[1:]:
            for k, v in other.items():
                if v is None or v == "" or (isinstance(v, float) and v != v):
                    continue
                cur = winner.get(k)
                if cur is None or cur == "" or (isinstance(cur, float) and cur != cur):
                    winner[k] = v
            # Prefer stamping combined provenance
            n0 = str(winner.get("note") or "")
            n1 = str(other.get("note") or "")
            if n1 and n1 not in n0:
                winner["note"] = f"{n0}; {n1}".strip("; ") if n0 else n1
        out.append(winner)
    return out
