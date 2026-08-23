"""HK annual aggregate → named 13G children (composition overlay).

Not a form parser. Stamps ``composition_parent=`` onto existing 13G history
rows and adds residual private_note rows. Portfolio ``$`` stays on PRIV_HK_*
aggregates; children keep null ``market_value_usd`` on the 13G path unless
broker SOTP overlay fills name-level ``$`` (composition CSV joins that in).
"""

from __future__ import annotations

import re
from typing import Any

# Valued / carrying parents emitted by parse_hk_annual.
PARENT_LISTED_INVESTEES = "PRIV_HK_LISTED_INVESTEES_FV"
PARENT_UNLISTED_INVESTEES = "PRIV_HK_UNLISTED_INVESTEES"
PARENT_LISTED_ASSOCIATES = "PRIV_HK_LISTED_ASSOCIATES"
PARENT_UNLISTED_ASSOCIATES = "PRIV_HK_UNLISTED_ASSOCIATES"

HK_AGGREGATE_TICKERS = frozenset(
    {
        PARENT_LISTED_INVESTEES,
        PARENT_UNLISTED_INVESTEES,
        PARENT_LISTED_ASSOCIATES,
        PARENT_UNLISTED_ASSOCIATES,
    }
)


def _clean_field(v: Any) -> Any:
    """Treat pandas/CSV NaN sentinels as missing (float nan is truthy in Python)."""
    if v is None:
        return None
    try:
        if v != v:  # NaN
            return None
    except Exception:
        pass
    s = str(v).strip()
    if not s or s.lower() in {"nan", "none", "null", "<na>"}:
        return None
    return v


def is_hk_residual_ticker(ticker: str | None) -> bool:
    t = str(ticker or "").strip().upper()
    return t.endswith("_RESIDUAL") and t.startswith("PRIV_HK_")


def excluded_from_portfolio_mv(row: dict) -> bool:
    """Residuals mirror parent $ for composition UX — never double-count."""
    if is_hk_residual_ticker(row.get("investee_ticker")):
        return True
    note = str(row.get("note") or "")
    return "excluded_from_portfolio_mv" in note or "residual_of_aggregate" in note


def _ticker(row: dict) -> str:
    return str(row.get("investee_ticker") or "").strip().upper()


def _is_hk_aggregate_row(row: dict) -> bool:
    return _ticker(row) in HK_AGGREGATE_TICKERS


def _is_composition_child_candidate(row: dict) -> bool:
    t = _ticker(row)
    if not t or t in HK_AGGREGATE_TICKERS or is_hk_residual_ticker(t):
        return False
    if str(row.get("action") or "") == "exit":
        return False
    return True


def composition_parent_for(ticker: str) -> str:
    """Bucket public names under listed investees; PRIV_* under unlisted investees."""
    t = str(ticker or "").strip().upper()
    if t.startswith("PRIV_"):
        return PARENT_UNLISTED_INVESTEES
    return PARENT_LISTED_INVESTEES


def append_composition_note(note: str | None, fragment: str) -> str:
    base = str(note or "").strip()
    if fragment in base:
        return base
    return f"{base}; {fragment}" if base else fragment


def _composition_parent_for(ticker: str) -> str:
    return composition_parent_for(ticker)


def _append_note(note: str | None, fragment: str) -> str:
    return append_composition_note(note, fragment)


def _hk_year_ends(history_rows: list[dict]) -> list[str]:
    ends = sorted(
        {
            str(r.get("period_end") or "")[:10]
            for r in history_rows
            if _is_hk_aggregate_row(r) and str(r.get("period_end") or "")[:10]
        }
    )
    return ends


def _parent_mv_by_period_ticker(
    history_rows: list[dict],
) -> dict[tuple[str, str], float]:
    """Parent portfolio $ (or carrying fallback) keyed by (period_end, parent_ticker)."""
    out: dict[tuple[str, str], float] = {}
    for r in history_rows:
        t = _ticker(r)
        if t not in HK_AGGREGATE_TICKERS:
            continue
        pe = str(r.get("period_end") or "")[:10]
        if not pe:
            continue
        mv = r.get("market_value_usd")
        if mv is None:
            mv = r.get("fair_value_disclosed_usd")
        if mv is None:
            mv = r.get("carrying_usd")
        if mv is None:
            continue
        try:
            out[(pe, t)] = float(mv)
        except (TypeError, ValueError):
            continue
    return out


def stamp_hk_composition_parents(history_rows: list[dict]) -> list[dict]:
    """Stamp composition_parent on 13G rows; append residual rows per valued parent.

    For each HK year-end, the latest non-exit child row with ``period_end`` ≤
    that year-end receives ``composition_parent`` / ``composition_as_of`` as
    first-class columns (and mirrored in ``note``). Residuals land on history
    with ``excluded_from_portfolio_mv`` so portfolio MV does not double-count.
    Does not invent child ``market_value_usd``.
    """
    if not history_rows:
        return history_rows

    year_ends = _hk_year_ends(history_rows)
    if not year_ends:
        return history_rows

    # Work on a shallow copy of rows (mutate notes in place on copies).
    rows = [dict(r) for r in history_rows]
    # Drop prior residual rows so re-stamp is idempotent.
    rows = [r for r in rows if not is_hk_residual_ticker(r.get("investee_ticker"))]
    parent_mv = _parent_mv_by_period_ticker(rows)

    # Index candidate children by ticker → sorted by period_end.
    by_ticker: dict[str, list[int]] = {}
    for i, r in enumerate(rows):
        if not _is_composition_child_candidate(r):
            continue
        t = _ticker(r)
        by_ticker.setdefault(t, []).append(i)
    for t in by_ticker:
        by_ticker[t].sort(key=lambda i: str(rows[i].get("period_end") or "")[:10])

    for ye in year_ends:
        # Prefer listed-investees parent when present that year; else associates.
        parents_present = {t for (pe, t) in parent_mv if pe == ye}
        for t, idxs in by_ticker.items():
            # Latest row at or before year-end.
            eligible = [
                i
                for i in idxs
                if str(rows[i].get("period_end") or "")[:10] <= ye
            ]
            if not eligible:
                continue
            i = eligible[-1]
            parent = _composition_parent_for(t)
            # If preferred parent missing this year, still stamp the bucket name
            # (composition view can show orphan); prefer present parent when only
            # associates exist for early years.
            if parent not in parents_present:
                if PARENT_LISTED_ASSOCIATES in parents_present and not t.startswith(
                    "PRIV_"
                ):
                    parent = PARENT_LISTED_ASSOCIATES
                elif PARENT_UNLISTED_ASSOCIATES in parents_present and t.startswith(
                    "PRIV_"
                ):
                    parent = PARENT_UNLISTED_ASSOCIATES
            agg = parent_mv.get((ye, parent))
            rows[i]["composition_parent"] = parent
            rows[i]["composition_as_of"] = ye
            rows[i]["parent_aggregate_market_value_usd"] = agg
            rows[i]["note"] = _append_note(
                rows[i].get("note"),
                (
                    f"composition_parent={parent}; composition_as_of={ye}; "
                    "composition=13g_overlay; not_fv_allocation"
                ),
            )

    # Residuals on history (excluded from portfolio MV) — one sheet SoT.
    parent_ticker = ""
    for r in rows:
        if r.get("parent_ticker"):
            parent_ticker = str(r["parent_ticker"])
            break
    named_with_mv = any(
        r.get("market_value_usd") not in (None, "")
        and r.get("composition_parent")
        and not is_hk_residual_ticker(r.get("investee_ticker"))
        for r in rows
    )
    residual_note = (
        "residual_of_aggregate; excluded_from_portfolio_mv; "
        "parent_aggregate_market_value_usd is Note 22 SoT — not a child mark"
    )
    residual_note += (
        "; some_named_children_have_broker_or_filing_$"
        if named_with_mv
        else "; named_children_have_null_$"
    )
    for (pe, parent), mv in sorted(parent_mv.items()):
        rows.append(
            {
                "parent_ticker": parent_ticker,
                "period_end": pe,
                "filing_date": pe,
                "accession_no": None,
                "filing_url": None,
                "investee_name": f"Residual of {parent} (aggregate SoT mirror)",
                "investee_ticker": f"{parent}_RESIDUAL",
                "cusip": None,
                "shares_held": None,
                "ownership_pct": None,
                "market_value_usd": mv,
                "shares_prev": None,
                "shares_delta": None,
                "value_prev": None,
                "value_delta": None,
                "action": "hold",
                "first_seen_period": pe,
                "exited_period": None,
                "note": residual_note,
                "composition_parent": parent,
                "composition_as_of": pe,
                "child_value_source": "residual_of_aggregate",
                "parent_aggregate_market_value_usd": mv,
            }
        )

    rows.sort(
        key=lambda r: (
            str(r.get("period_end") or ""),
            str(r.get("investee_ticker") or ""),
            str(r.get("investee_name") or ""),
        )
    )
    return rows


def finalize_composition_columns(history_rows: list[dict]) -> list[dict]:
    """After broker overlay: set child_value_source from row $ / note."""
    if not history_rows:
        return history_rows
    out: list[dict] = []
    for r in history_rows:
        row = dict(r)
        if is_hk_residual_ticker(row.get("investee_ticker")):
            row.setdefault("child_value_source", "residual_of_aggregate")
            out.append(row)
            continue
        parent = row.get("composition_parent")
        if not parent:
            note = str(row.get("note") or "")
            m = re.search(r"composition_parent=([A-Z0-9_]+)", note)
            if m:
                parent = m.group(1)
                row["composition_parent"] = parent
            m_asof = re.search(r"composition_as_of=(\d{4}-\d{2}-\d{2})", note)
            if m_asof and not row.get("composition_as_of"):
                row["composition_as_of"] = m_asof.group(1)
        # Provenance for any row with $ (broker/filing), not only composition-stamped.
        if row.get("market_value_usd") not in (None, ""):
            src = _infer_value_source(row.get("note")) or row.get("child_value_source") or "row"
            row["child_value_source"] = src
        out.append(row)
    return out


def live_holdings_from_history(history_rows: list[dict]) -> list[dict]:
    """Open positions = latest non-exit row per investee_ticker (QoQ SoT slice).

    Maps into ``HOLDINGS_COLUMNS`` for the live table / current_holdings sheet.
    Residuals and HK aggregate-only rows are included when they are the latest
    for that ticker (aggregates are the portfolio SoT).
    """
    from .schema import HOLDINGS_COLUMNS, empty_holding_row

    if not history_rows:
        return []
    # Chronological so later exits clear earlier holds.
    ordered = sorted(
        history_rows,
        key=lambda r: (
            str(r.get("period_end") or "")[:10],
            str(r.get("investee_ticker") or ""),
        ),
    )
    by_t: dict[str, dict] = {}
    for r in ordered:
        t = _ticker(r)
        if not t:
            continue
        if str(r.get("action") or "") == "exit":
            # Exit clears the live map for that name.
            by_t.pop(t, None)
            continue
        pe = str(r.get("period_end") or "")[:10]
        prev = by_t.get(t)
        if prev is None or pe >= str(prev.get("period_end") or "")[:10]:
            by_t[t] = r
    live: list[dict] = []
    for r in by_t.values():
        as_of = str(r.get("period_end") or r.get("composition_as_of") or "")[:10]
        row = empty_holding_row(
            parent_ticker=r.get("parent_ticker"),
            as_of_date=as_of or None,
            as_of_accession_no=r.get("accession_no"),
            investee_name=r.get("investee_name"),
            investee_ticker=r.get("investee_ticker"),
            ownership_pct=r.get("ownership_pct"),
            shares_held=r.get("shares_held"),
            market_value_usd=r.get("market_value_usd"),
            first_filing_date=r.get("first_seen_period") or as_of,
            first_accession_no=r.get("accession_no"),
            note=r.get("note"),
            confidence="history_slice",
            cost_basis_est_usd=_clean_field(r.get("cost_basis_est_usd")),
            cost_basis_est_price=_clean_field(r.get("cost_basis_est_price")),
            cost_basis_est_as_of=_clean_field(r.get("cost_basis_est_as_of")),
            cost_basis_est_source=_clean_field(r.get("cost_basis_est_source")),
        )
        # Preserve composition provenance on live for sheet readers.
        extra_note_bits = []
        cp = _clean_field(r.get("composition_parent"))
        ca = _clean_field(r.get("composition_as_of"))
        cvs = _clean_field(r.get("child_value_source"))
        pagg = _clean_field(r.get("parent_aggregate_market_value_usd"))
        if cp:
            extra_note_bits.append(f"composition_parent={cp}")
        if ca:
            extra_note_bits.append(f"composition_as_of={ca}")
        if cvs:
            extra_note_bits.append(f"child_value_source={cvs}")
        if pagg is not None:
            extra_note_bits.append(f"parent_aggregate_market_value_usd={pagg}")
        cbe = _clean_field(r.get("cost_basis_est_usd"))
        if cbe is not None:
            extra_note_bits.append(f"cost_basis_est_usd={cbe}")
            extra_note_bits.append(
                "value_estimate=eod_at_filing; estimate_role=cost_basis; "
                "not_fv_allocation; excluded_from_portfolio_mv"
            )
        for bit in extra_note_bits:
            row["note"] = append_composition_note(row.get("note"), bit)
        live.append({c: row.get(c) for c in HOLDINGS_COLUMNS})
    live.sort(
        key=lambda x: (
            str(x.get("investee_ticker") or ""),
            str(x.get("investee_name") or ""),
        )
    )
    return live


def _infer_value_source(note: str | None) -> str | None:
    n = str(note or "")
    if "value_source=broker_sotp" in n:
        return "broker_sotp"
    if "value_source=hk_annual_note22" in n or "source=hk_annual" in n:
        return "hk_annual_note22"
    if "value_source=13f" in n or "source=13f" in n:
        return "13f"
    if "source=13g" in n or "form=SC 13" in n or "SCHEDULE 13" in n:
        return None  # 13G is not a $ source
    if "value_source=" in n:
        m = re.search(r"value_source=([a-z0-9_]+)", n)
        return m.group(1) if m else None
    return None


def _broker_mv_by_ticker(
    history_rows: list[dict],
) -> dict[str, list[tuple[str, float]]]:
    """investee_ticker → [(period_end, market_value_usd), ...] for broker-stamped rows."""
    out: dict[str, list[tuple[str, float]]] = {}
    for r in history_rows:
        if "value_source=broker_sotp" not in str(r.get("note") or ""):
            continue
        t = _ticker(r)
        if not t or t in HK_AGGREGATE_TICKERS or is_hk_residual_ticker(t):
            continue
        pe = str(r.get("period_end") or "")[:10]
        mv = r.get("market_value_usd")
        if not pe or mv is None or mv == "":
            continue
        try:
            out.setdefault(t, []).append((pe, float(mv)))
        except (TypeError, ValueError):
            continue
    for t in out:
        out[t].sort(key=lambda x: x[0])
    return out


def _child_mv_for_composition(
    *,
    ticker: str,
    as_of: str,
    row_mv: Any,
    row_note: str | None,
    broker_by_ticker: dict[str, list[tuple[str, float]]],
) -> tuple[float | None, str | None]:
    """Prefer row $; else latest broker_sotp $ with period_end ≤ composition_as_of."""
    if row_mv is not None and row_mv != "":
        try:
            mv_f = float(row_mv)
            if mv_f == mv_f:  # not NaN
                src = _infer_value_source(row_note) or "row"
                return mv_f, src
        except (TypeError, ValueError):
            pass
    cands = broker_by_ticker.get(ticker) or []
    # Prefer period_end ≤ composition_as_of. If none (broker only after stamp
    # date), use any broker vintage for that name so child $ is not left null
    # when a later stamp exists — still never invent OTC×shares.
    eligible = [(pe, mv) for pe, mv in cands if pe <= as_of]
    if not eligible and cands:
        eligible = list(cands)
    if not eligible:
        return None, None
    pe, mv = eligible[-1]
    return mv, "broker_sotp"


def hk_composition_frame(history_rows: list[dict]) -> list[dict[str, Any]]:
    """Narrow composition view over QoQ rows (tests / validators).

    Prefers first-class ``composition_parent`` / ``composition_as_of`` columns;
    falls back to note regex. Residuals already on history are passed through.
    """
    parent_mv = _parent_mv_by_period_ticker(history_rows)
    broker_by_ticker = _broker_mv_by_ticker(history_rows)
    out: list[dict[str, Any]] = []
    seen_residual: set[tuple[str, str]] = set()
    for r in history_rows:
        parent = _clean_field(r.get("composition_parent"))
        note = str(r.get("note") or "")
        if not parent:
            m = re.search(r"composition_parent=([A-Z0-9_]+)", note)
            if not m:
                continue
            parent = m.group(1)
        as_of = _clean_field(r.get("composition_as_of"))
        as_of = str(as_of)[:10] if as_of else ""
        if not as_of or as_of.lower() == "nan":
            m_asof = re.search(r"composition_as_of=(\d{4}-\d{2}-\d{2})", note)
            as_of = m_asof.group(1) if m_asof else str(_clean_field(r.get("period_end")) or "")[:10]
        if not as_of or as_of.lower() == "nan":
            continue
        child_t = _ticker(r)
        if is_hk_residual_ticker(child_t):
            mv = r.get("market_value_usd")
            if mv is None:
                mv = r.get("parent_aggregate_market_value_usd")
            out.append(
                {
                    "period_end": as_of or str(r.get("period_end") or "")[:10],
                    "composition_parent": parent,
                    "child_ticker": child_t,
                    "child_name": r.get("investee_name"),
                    "ownership_pct": None,
                    "shares_held": None,
                    "child_market_value_usd": mv,
                    "child_value_source": r.get("child_value_source")
                    or "residual_of_aggregate",
                    "parent_aggregate_market_value_usd": r.get(
                        "parent_aggregate_market_value_usd", mv
                    ),
                    "note": note or "residual_of_aggregate; excluded_from_portfolio_mv",
                }
            )
            seen_residual.add((str(as_of), str(parent)))
            continue
        child_mv, child_src = _child_mv_for_composition(
            ticker=child_t,
            as_of=as_of,
            row_mv=r.get("market_value_usd"),
            row_note=note,
            broker_by_ticker=broker_by_ticker,
        )
        child_src_col = _clean_field(r.get("child_value_source"))
        if child_src_col:
            child_src = child_src_col
        agg = _clean_field(r.get("parent_aggregate_market_value_usd"))
        if agg is None:
            agg = parent_mv.get((as_of, parent))
        out.append(
            {
                "period_end": as_of,
                "composition_parent": parent,
                "child_ticker": child_t,
                "child_name": r.get("investee_name"),
                "ownership_pct": r.get("ownership_pct"),
                "shares_held": r.get("shares_held"),
                "child_market_value_usd": child_mv,
                "child_value_source": child_src,
                "parent_aggregate_market_value_usd": agg,
                "note": (
                    "composition=13g_overlay; not_fv_allocation"
                    + (f"; child_value_source={child_src}" if child_src else "")
                ),
            }
        )
    # Backfill residuals for parents not yet on history (legacy exports).
    for (pe, parent), mv in sorted(parent_mv.items()):
        if (pe, parent) in seen_residual:
            continue
        if any(
            str(x.get("period_end")) == pe
            and str(x.get("composition_parent")) == parent
            and is_hk_residual_ticker(x.get("child_ticker"))
            for x in out
        ):
            continue
        out.append(
            {
                "period_end": pe,
                "composition_parent": parent,
                "child_ticker": f"{parent}_RESIDUAL",
                "child_name": f"Residual of {parent} (aggregate SoT mirror)",
                "ownership_pct": None,
                "shares_held": None,
                "child_market_value_usd": mv,
                "child_value_source": "residual_of_aggregate",
                "parent_aggregate_market_value_usd": mv,
                "note": (
                    "residual_of_aggregate; excluded_from_portfolio_mv; "
                    "parent_aggregate_market_value_usd is Note 22 SoT — not a child mark"
                ),
            }
        )
    out.sort(
        key=lambda x: (
            str(x.get("period_end") or ""),
            str(x.get("composition_parent") or ""),
            str(x.get("child_ticker") or ""),
        )
    )
    return out
