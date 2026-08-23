"""Refuse-to-ship checks for equity holdings (live + history)."""

from __future__ import annotations


def _f(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if x != x:  # NaN
        return None
    return x


def looks_like_ownership_pct_as_shares(shares_held, ownership_pct) -> bool:
    """True when shares_held is just ownership_% copied into the shares column.

    Neutron Holdings shipped as shares_held=22.87 (== ownership_pct). Real share
    counts are >> 100; ownership % is in (0, 100].
    """
    sh = _f(shares_held)
    pct = _f(ownership_pct)
    if sh is None or pct is None:
        return False
    if not (0 < sh <= 100 and 0 < pct <= 100):
        return False
    return abs(sh - pct) < 1e-6


def scrub_live_pct_as_shares(rows: list[dict]) -> list[dict]:
    """Null out Neutron-class fake share counts on live/current rows."""
    out: list[dict] = []
    for raw in rows:
        row = dict(raw)
        if looks_like_ownership_pct_as_shares(row.get("shares_held"), row.get("ownership_pct")):
            row["shares_held"] = None
            note = str(row.get("note") or "")
            if "scrubbed_shares_eq_ownership_pct" not in note:
                row["note"] = (
                    f"{note}; scrubbed_shares_eq_ownership_pct".strip("; ")
                    if note
                    else "scrubbed_shares_eq_ownership_pct"
                )
        out.append(row)
    return out


def assert_live_shares_held_sane(rows: list[dict], *, context: str = "live") -> None:
    """Dagster/export hard-fail: live shares_held must not equal ownership_%."""
    bad: list[str] = []
    for r in rows:
        if looks_like_ownership_pct_as_shares(r.get("shares_held"), r.get("ownership_pct")):
            name = r.get("investee_name") or r.get("investee_ticker") or "?"
            bad.append(
                f"{name}: shares_held={r.get('shares_held')} == ownership_pct={r.get('ownership_pct')}"
            )
    if bad:
        raise ValueError(
            f"{context}: ownership_% stuffed into shares_held "
            f"(Neutron-class; leave shares null, keep ownership_pct): "
            + "; ".join(bad[:10])
        )


def assert_estimates_not_in_market_value(rows: list[dict], *, context: str = "") -> None:
    """Refuse-to-ship: EOD filing estimates must not pollute market_value_usd."""
    bad: list[dict] = []
    for r in rows:
        note = str(r.get("note") or "")
        has_est_cols = (
            _f(r.get("cost_basis_est_usd")) is not None
            or _f(r.get("mark_at_filing_est_usd")) is not None
        )
        if "value_estimate=eod_at_filing" not in note and not has_est_cols:
            continue
        if has_est_cols and "excluded_from_portfolio_mv" not in note:
            bad.append(
                {
                    "ticker": r.get("investee_ticker"),
                    "period_end": r.get("period_end") or r.get("as_of_date"),
                    "issue": "estimate_missing_excluded_from_portfolio_mv",
                }
            )
        mv = _f(r.get("market_value_usd"))
        if mv is None:
            continue
        if "value_estimate=eod_at_filing" not in note:
            continue
        has_real = any(
            x in note
            for x in (
                "value_source=broker_sotp",
                "value_source=13f",
                "value_source=hk_annual_note22",
                "source=13f",
                "investments_table",
            )
        )
        if not has_real:
            bad.append(
                {
                    "ticker": r.get("investee_ticker"),
                    "period_end": r.get("period_end") or r.get("as_of_date"),
                    "market_value_usd": mv,
                    "issue": "eod_estimate_as_market_value",
                }
            )
    if bad:
        raise AssertionError(
            f"EOD filing estimates leaked into market_value_usd / missing exclusion "
            f"({context}): {bad[:8]}"
        )
