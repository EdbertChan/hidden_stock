"""Refuse-to-ship checks for equity holdings (live + history)."""

from __future__ import annotations


def _f(v) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


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
