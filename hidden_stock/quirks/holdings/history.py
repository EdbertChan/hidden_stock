"""Quarter-over-quarter holdings history (13F + annual 20-F/10-K notes)."""

from __future__ import annotations

import re
import time
from typing import Any
from xml.etree import ElementTree as ET

from .parse_notes import parse_investment_notes
from .sec_api_13f import collect_filer_13f_periods

HISTORY_COLUMNS = [
    "parent_ticker",
    "period_end",
    "filing_date",
    "accession_no",
    "filing_url",
    "investee_name",
    "investee_ticker",
    "cusip",
    "shares_held",
    "ownership_pct",
    "market_value_usd",
    "shares_prev",
    "shares_delta",
    "value_prev",
    "value_delta",
    "action",  # new | buy | sell | exit | hold | ratio_adj
    "first_seen_period",
    "exited_period",
    "note",
]


def _local(tag: str) -> str:
    return tag.split("}")[-1] if "}" in tag else tag


def parse_13f_period_end(primary_doc_xml: str) -> str | None:
    """Return periodOfReport as YYYY-MM-DD."""
    if not primary_doc_xml:
        return None
    try:
        root = ET.fromstring(primary_doc_xml.strip())
    except ET.ParseError:
        return None
    for el in root.iter():
        name = _local(el.tag).lower()
        if name in {"periodofreport", "reportcalendarorquarter"} and el.text:
            raw = el.text.strip()
            # MM-DD-YYYY
            m = re.match(r"(\d{1,2})-(\d{1,2})-(\d{4})", raw)
            if m:
                mm, dd, yyyy = m.groups()
                return f"{yyyy}-{int(mm):02d}-{int(dd):02d}"
            m = re.match(r"(\d{4})-(\d{2})-(\d{2})", raw)
            if m:
                return raw[:10]
    return None


def _key(row: dict) -> str:
    """Identity for QoQ diffs: ticker first, then CUSIP, then name.

    Ticker-first matches live ``merge_raw_holdings`` and prevents 13F drop +
    continuing 13G from emitting exit(c:CUSIP)+new(t:TICKER) for the same name
    (Codex grade FAIL on SERV @ 2026-06-30).
    """
    from .lookback import normalize_ticker

    t = normalize_ticker(row.get("investee_ticker")) or ""
    if t:
        return f"t:{t}"
    c = (row.get("_cusip") or row.get("cusip") or "").strip().upper()
    if c:
        return f"c:{c}"
    name = re.sub(r"[^a-z0-9]+", "", (row.get("investee_name") or "").lower())
    return f"n:{name}"


def classify_action(shares_prev: float | None, shares: float | None) -> str:
    prev = float(shares_prev or 0)
    cur = float(shares or 0)
    if prev <= 0 and cur > 0:
        return "new"
    if prev > 0 and cur <= 0:
        return "exit"
    if cur > prev:
        return "buy"
    if cur < prev:
        return "sell"
    return "hold"


def _integer_share_ratio(prev: float | None, cur: float | None) -> int | None:
    """Return consolidation factor R≥2 when prev/cur is an exact integer ratio."""
    try:
        p = float(prev or 0.0)
        c = float(cur or 0.0)
    except (TypeError, ValueError):
        return None
    if p <= 0 or c <= 0 or p <= c:
        return None
    ratio = p / c
    r = int(round(ratio))
    if r < 2:
        return None
    if abs(ratio - r) > 1e-6 * max(1.0, ratio):
        return None
    if abs(p - r * c) > max(1e-3, 1e-9 * p):
        return None
    return r


def _cusip_issuer(cusip: str | None) -> str:
    return str(cusip or "").strip().upper()[:6]


def reclassify_ads_ratio_adjustments(history: list[dict]) -> list[dict]:
    """ADS ratio / reverse-split restatements are not disposals.

    Exact integer share consolidation (R≥2) plus issuer-CUSIP change on this
    period or the next period ⇒ ``action=ratio_adj`` (lots scale; no realized).
    """
    from collections import defaultdict

    from .lookback import normalize_ticker

    by_t: dict[str, list[dict]] = defaultdict(list)
    for row in history:
        t = normalize_ticker(row.get("investee_ticker"))
        if t:
            by_t[t].append(row)
    for rows in by_t.values():
        rows.sort(key=lambda r: str(r.get("period_end") or ""))

    for rows in by_t.values():
        for i, row in enumerate(rows):
            if str(row.get("action") or "") != "sell":
                continue
            r = _integer_share_ratio(row.get("shares_prev"), row.get("shares_held"))
            if r is None:
                continue
            cur_c = str(row.get("cusip") or "").strip().upper()
            prev_c = ""
            for j in range(i - 1, -1, -1):
                if str(rows[j].get("action") or "") == "exit":
                    continue
                prev_c = str(rows[j].get("cusip") or "").strip().upper()
                if prev_c:
                    break
            next_c = ""
            if i + 1 < len(rows):
                next_c = str(rows[i + 1].get("cusip") or "").strip().upper()

            cusip_changed = False
            if (
                prev_c
                and cur_c
                and _cusip_issuer(prev_c) == _cusip_issuer(cur_c)
                and prev_c != cur_c
            ):
                cusip_changed = True
            elif (
                prev_c
                and next_c
                and _cusip_issuer(prev_c) == _cusip_issuer(next_c)
                and prev_c != next_c
            ):
                cusip_changed = True
            elif (
                cur_c
                and next_c
                and _cusip_issuer(cur_c) == _cusip_issuer(next_c)
                and cur_c != next_c
            ):
                cusip_changed = True
            if not cusip_changed:
                continue

            row["action"] = "ratio_adj"
            note = str(row.get("note") or "")
            stamp = f"ads_ratio={r}:1; restatement_not_disposal"
            if stamp not in note:
                row["note"] = f"{note}; {stamp}".strip("; ")
    return history


def _continuity_qty(row: dict | None) -> float:
    """Quantity for QoQ action classify — real shares, else ownership_% (not exported as shares)."""
    if not row:
        return 0.0
    if row.get("shares_held") is not None:
        try:
            return float(row["shares_held"])
        except (TypeError, ValueError):
            return 0.0
    pct = row.get("ownership_pct")
    if pct is not None:
        try:
            return float(pct)
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def _private_note_ticker(name: str | None) -> str:
    """Stable non-exchange identity for private 20-F note names (not a real ticker)."""
    slug = re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")
    slug = re.sub(r"_+", "_", slug)[:48]
    return f"PRIV_{slug.upper()}" if slug else "PRIV_UNKNOWN"


def note_as_position(raw: dict) -> dict:
    """Map a 20-F/10-K note holding into a QoQ snapshot row.

    Ownership % is QoQ continuity via ``ownership_pct`` only — never written into
    ``shares_held``. Chart/portfolio ``$`` comes only from Investments-table
    disclosed FV (``*_investments_table``), never from narrative cost/carrying.
    """
    pct = raw.get("ownership_pct")
    fv = raw.get("fair_value_disclosed_usd")
    carrying = raw.get("carrying_usd")
    src = str(raw.get("_source") or "20f_note")
    note = str(raw.get("note") or "")
    is_investments_table = "investments_table" in src.lower() or "investments_table" in note.lower()
    # Valuation order: only Investments-table (or explicit FV stamp) may set $.
    if is_investments_table:
        market = fv if fv is not None else carrying
    else:
        market = None
    # Real share count only — never invent % or presence into shares_held.
    if raw.get("shares_held") is not None:
        shares: float | None = float(raw["shares_held"])
    else:
        shares = None
        if pct is not None and "qoq_continuity=ownership_pct" not in note:
            note = f"{note}; qoq_continuity=ownership_pct".strip("; ")
    # Ticker: aliases first; else PRIV_<slug> so uniqueness works without fake symbols.
    from .extract import load_investee_aliases, resolve_investee_ticker
    from .lookback import normalize_ticker

    ticker = normalize_ticker(raw.get("investee_ticker"))
    if not ticker:
        ticker = resolve_investee_ticker(raw.get("investee_name"), None, load_investee_aliases())
    if not ticker:
        ticker = _private_note_ticker(raw.get("investee_name"))
        if "ticker=private_note" not in note:
            note = f"{note}; ticker=private_note".strip("; ")
    if not note:
        note = f"source={src}"
    return {
        "investee_name": raw.get("investee_name"),
        "investee_ticker": ticker,
        "shares_held": shares,
        "market_value_usd": float(market) if market is not None else None,
        "ownership_pct": pct,
        "as_of_date": raw.get("as_of_date"),
        "as_of_accession_no": raw.get("as_of_accession_no"),
        "_cusip": None,
        "cusip": None,
        "_source": src,
        "note": note,
    }


def diff_snapshots(
    parent_ticker: str,
    ordered_periods: list[tuple[str, str, str, list[dict]]],
) -> list[dict]:
    """ordered_periods: list of (period_end, filing_date, accession, rows) oldest→newest."""
    history: list[dict] = []
    prev_map: dict[str, dict] = {}
    first_seen: dict[str, str] = {}

    for period_end, filing_date, accession, rows in ordered_periods:
        cur_map = {_key(r): r for r in rows if _key(r) not in {"t:", "c:", "n:"}}
        keys = set(prev_map) | set(cur_map)

        for k in sorted(keys):
            prev = prev_map.get(k)
            cur = cur_map.get(k)
            qty_prev = _continuity_qty(prev)
            qty = _continuity_qty(cur)
            value_prev = (
                float(prev["market_value_usd"]) if prev and prev.get("market_value_usd") is not None else None
            )
            value = float(cur["market_value_usd"]) if cur and cur.get("market_value_usd") is not None else None
            # Exported shares_held: real counts only — never ownership_%.
            shares_prev = (
                float(prev["shares_held"]) if prev and prev.get("shares_held") is not None else None
            )
            shares = float(cur["shares_held"]) if cur and cur.get("shares_held") is not None else None

            if prev is None and cur is None:
                continue
            if k not in first_seen and qty > 0:
                first_seen[k] = period_end

            action = classify_action(qty_prev if prev else 0.0, qty if cur else 0.0)
            if prev is None and (cur is None or qty <= 0):
                continue
            if prev is None and cur is not None:
                action = "new"

            name = (cur or prev or {}).get("investee_name")
            ticker = (cur or prev or {}).get("investee_ticker")
            cusip = (cur or prev or {}).get("_cusip") or (cur or prev or {}).get("cusip")
            src_note = (cur or prev or {}).get("note")
            # Exits cite the period grid filing (when the name disappeared), not the prior hold.
            if action == "exit":
                support_acc = accession or (prev or {}).get("as_of_accession_no")
            else:
                support_acc = (
                    (cur or {}).get("as_of_accession_no")
                    or (prev or {}).get("as_of_accession_no")
                    or accession
                )

            exited_period = period_end if action == "exit" else None
            out_shares = 0.0 if action == "exit" else shares
            out_shares_prev = shares_prev if prev is not None else None
            if action == "exit":
                shares_delta = 0.0 - (shares_prev or 0.0)
            elif shares is not None or shares_prev is not None:
                shares_delta = (shares or 0.0) - (shares_prev or 0.0)
            else:
                shares_delta = None
            # 13G/note overlays keep their own as_of filing_date (not the 13F grid date).
            src_l = str(src_note or "").lower()
            is_overlay = any(
                x in src_l for x in ("source=13g", "20f_note", "10k_note", "10q_note", "ticker=private_note")
            )
            row_filing = filing_date
            if is_overlay and action != "exit":
                row_filing = (
                    (cur or {}).get("as_of_date")
                    or (prev or {}).get("as_of_date")
                    or filing_date
                )
            history.append(
                {
                    "parent_ticker": parent_ticker,
                    "period_end": period_end,
                    "filing_date": row_filing,
                    "accession_no": support_acc,
                    "filing_url": None,  # stamped later when parent CIK is known
                    "investee_name": name,
                    "investee_ticker": ticker,
                    "cusip": cusip,
                    "shares_held": out_shares,
                    "market_value_usd": value if action != "exit" else 0.0,
                    "shares_prev": out_shares_prev,
                    "shares_delta": shares_delta,
                    "value_prev": value_prev,
                    "value_delta": (
                        None
                        if value is None and value_prev is None
                        else (0.0 if action == "exit" else (value or 0.0)) - (value_prev or 0.0)
                    ),
                    "action": action,
                    "first_seen_period": first_seen.get(k),
                    "exited_period": exited_period,
                    "note": src_note or f"QoQ {period_end}",
                    # Exit means zero economic interest — never carry prior ownership_%.
                    "ownership_pct": (
                        0.0
                        if action == "exit"
                        else (cur or prev or {}).get("ownership_pct")
                    ),
                }
            )

        prev_map = cur_map

    return reclassify_ads_ratio_adjustments(coalesce_history_by_period_ticker(history))


def coalesce_history_by_period_ticker(history: list[dict]) -> list[dict]:
    """Enforce ≤1 row per (period_end, investee_ticker). Prefer non-exit survivors.

    Safety net when CUSIP-only and ticker keys still collide across sources.
    """
    from .lookback import normalize_ticker

    if not history:
        return history
    buckets: dict[tuple[str, str], list[dict]] = {}
    passthrough: list[dict] = []
    for row in history:
        t = normalize_ticker(row.get("investee_ticker"))
        pe = str(row.get("period_end") or "")
        if not t or not pe:
            passthrough.append(row)
            continue
        buckets.setdefault((pe, t), []).append(row)

    out: list[dict] = list(passthrough)
    for (_pe, _t), rows in buckets.items():
        if len(rows) == 1:
            out.append(rows[0])
            continue
        non_exit = [r for r in rows if str(r.get("action") or "") != "exit"]
        candidates = non_exit or rows

        def _score(r: dict) -> tuple:
            has_mv = 1 if r.get("market_value_usd") is not None else 0
            has_shares = 1 if (r.get("shares_held") or 0) not in (None, 0, 0.0) else 0
            # Prefer continuing positions over exits; disclosed $ over null
            return (has_shares, has_mv, 0 if str(r.get("action")) == "exit" else 1)

        best = max(candidates, key=_score)
        # Fill gaps from siblings (e.g. CUSIP on exit row)
        for other in rows:
            if other is best:
                continue
            for k in ("cusip", "ownership_pct", "market_value_usd", "note"):
                if best.get(k) is None and other.get(k) is not None:
                    if k == "market_value_usd" and str(best.get("action")) == "exit":
                        continue
                    if k == "note" and other.get("note"):
                        best["note"] = f"{best.get('note') or ''}; coalesced".strip("; ")
                    elif k != "note":
                        best[k] = other[k]
        out.append(best)

    out.sort(
        key=lambda r: (
            str(r.get("period_end") or ""),
            str(r.get("investee_ticker") or ""),
            str(r.get("action") or ""),
        )
    )
    return out


def assert_unique_period_ticker(history: list[dict], *, context: str = "history") -> None:
    """Hard-fail when any ticker appears twice in one period_end."""
    from .lookback import normalize_ticker

    counts: dict[tuple[str, str], int] = {}
    for row in history:
        t = normalize_ticker(row.get("investee_ticker"))
        pe = str(row.get("period_end") or "")
        if not t or not pe or pe.upper() == "NAN":
            continue
        if str(row.get("action") or "") == "exit" and (row.get("shares_held") in (0, 0.0, None)):
            # Exits still count — duplicates including exit are the SERV failure mode
            pass
        counts[(pe, t)] = counts.get((pe, t), 0) + 1
    dups = [f"{pe}/{t}×{n}" for (pe, t), n in sorted(counts.items()) if n > 1]
    if dups:
        raise ValueError(
            f"{context}: duplicate investee_ticker in same period_end "
            f"(invariant: one row per ticker per period): {', '.join(dups[:20])}"
        )


def _collect_13f_periods(
    *,
    parent_ticker: str,
    edgar,
    as_of: str | None,
    max_filings: int,
    lookback_start: str | None = None,
) -> tuple[list[tuple[str, str, str, list[dict]]], dict[str, Any]]:
    """Collect 13F periods via sec-api Form 13F holdings (filer CIK)."""
    from .runner import PARENT_CIK_OVERRIDES

    meta: dict[str, Any] = {
        "parent_ticker": parent_ticker,
        "num_filings": 0,
        "num_periods": 0,
        "error": None,
        "source": "sec_api_13f",
    }
    cik = PARENT_CIK_OVERRIDES.get(parent_ticker) or edgar.get_cik(parent_ticker)
    if not cik:
        meta["error"] = "no CIK"
        return [], meta

    ordered, api_meta = collect_filer_13f_periods(
        parent_ticker=parent_ticker,
        cik=cik,
        as_of=as_of,
        max_filings=max_filings,
        lookback_start=lookback_start,
    )
    meta.update({k: v for k, v in api_meta.items() if k != "parent_ticker"})
    return ordered, meta


def collect_note_snapshots(
    *,
    parent_ticker: str,
    edgar,
    as_of: str | None = None,
    max_filings: int = 12,
    lookback_start: str | None = None,
) -> tuple[list[tuple[str, str, str, list[dict]]], dict[str, Any]]:
    """Oldest→newest annual/interim note snapshots."""
    from .lookback import date_on_or_after

    meta: dict[str, Any] = {"num_annual_filings": 0, "num_note_snapshots": 0, "error": None}
    cik = edgar.get_cik(parent_ticker)
    if not cik:
        meta["error"] = "no CIK"
        return [], meta

    filings = edgar.list_filings(
        cik, form_types=("20-F", "10-K", "10-Q"), as_of=as_of, limit=max_filings
    )
    if lookback_start:
        filings = [f for f in filings if date_on_or_after(f.get("filing_date"), lookback_start)]
    filings = list(reversed(filings))
    meta["num_annual_filings"] = len(filings)

    snaps: list[tuple[str, str, str, list[dict]]] = []
    for f in filings:
        html = edgar.fetch_filing_document(cik, f["accession_no"], f["primary_document"])
        text = edgar.get_filing_text(html, max_chars=1_200_000)
        raw = parse_investment_notes(
            text,
            parent_ticker=parent_ticker,
            form=f.get("form"),
            accession_no=f["accession_no"],
            filing_date=f["filing_date"],
        )
        if not raw:
            time.sleep(0.05)
            continue
        positions = [note_as_position(r) for r in raw]
        snaps.append((f["filing_date"], f["filing_date"], f["accession_no"], positions))
        time.sleep(0.05)

    meta["num_note_snapshots"] = len(snaps)
    return snaps, meta


def _notes_as_of(
    note_snaps: list[tuple[str, str, str, list[dict]]],
    as_of: str,
    *,
    period_end: str | None = None,
    window: int = 3,
) -> list[dict]:
    """Note positions as-of date: union of up to ``window`` filings (period-aware FV).

    Investments-table rows carry ``as_of_date`` = column date. Prefer the column
    matching ``period_end`` (or the latest column ≤ period_end).
    """
    eligible = [s for s in note_snaps if s[1] <= as_of]
    use = eligible[-window:] if eligible else []
    by_key: dict[str, dict] = {}
    pe = (period_end or "")[:10]

    def _value_date(r: dict) -> str:
        return str(r.get("as_of_date") or "")[:10]

    def _prefer(prev: dict | None, cur: dict) -> dict:
        if prev is None:
            return cur
        if not pe:
            return cur  # later filing wins
        prev_vd = _value_date(prev)
        cur_vd = _value_date(cur)
        # Drop future column values relative to the 13F period
        if cur_vd and cur_vd > pe:
            return prev
        if prev_vd and prev_vd > pe and (not cur_vd or cur_vd <= pe):
            return cur
        if cur_vd == pe and prev_vd != pe:
            return cur
        if prev_vd == pe and cur_vd != pe:
            return prev
        if cur_vd and prev_vd:
            # Closest on-or-before period_end
            if cur_vd <= pe and prev_vd <= pe:
                return cur if cur_vd >= prev_vd else prev
            return cur if cur_vd <= pe else prev
        # Prefer row that already has disclosed dollars
        if prev.get("market_value_usd") is None and cur.get("market_value_usd") is not None:
            return cur
        return cur

    for _snap_as_of, _filing_date, _acc, rows in use:
        for r in rows:
            vd = _value_date(r)
            if pe and vd and vd > pe:
                continue
            k = _key(r)
            if k in {"t:", "c:", "n:"}:
                continue
            by_key[k] = _prefer(by_key.get(k), r)
    return list(by_key.values())


def _is_disclosed_dollar_source(label: str) -> bool:
    s = str(label or "").lower()
    return any(
        m in s
        for m in (
            "investments_table",
            "20f_note",
            "10k_note",
            "10q_note",
            "filing_note",
        )
    )


def _record_value_provenance(dst: dict, src: dict) -> None:
    """When $ is filled from Investments/notes, stamp note — 13G alone is not $ provenance."""
    src_label = str(src.get("_source") or "")
    src_note = str(src.get("note") or "")
    if not (
        _is_disclosed_dollar_source(src_label) or _is_disclosed_dollar_source(src_note)
    ):
        return
    label = src_label or "disclosed_fv"
    prev = str(dst.get("_source") or "")
    if label and label not in prev.split("+"):
        dst["_source"] = f"{prev}+{label}" if prev and prev != label else label
    note = str(dst.get("note") or "")
    # Prefer explicit investments_table token Codex/mechanical can grep
    stamp = src_note if _is_disclosed_dollar_source(src_note) else f"value_source={label}"
    if "investments_table" not in note and "_note" not in note and "value_source=" not in note:
        dst["note"] = f"{note}; {stamp}".strip("; ") if note else stamp
    elif stamp and stamp not in note and "investments_table" in stamp:
        dst["note"] = f"{note}; {stamp}".strip("; ")
    # URL should open the Investments/notes filing, not the 13G identity filing
    if src.get("as_of_accession_no"):
        dst["as_of_accession_no"] = src["as_of_accession_no"]


def _fill_missing(dst: dict, src: dict) -> None:
    for k, v in src.items():
        if v is None or k in {"_source", "note"}:
            continue
        if dst.get(k) is None:
            dst[k] = v
    # Disclosed FV / table $ fills null market_value even when key was first from 13G
    filled_mv = False
    if dst.get("market_value_usd") is None and src.get("market_value_usd") is not None:
        dst["market_value_usd"] = src["market_value_usd"]
        filled_mv = True
    if dst.get("ownership_pct") is None and src.get("ownership_pct") is not None:
        dst["ownership_pct"] = src["ownership_pct"]
    if filled_mv or (
        src.get("market_value_usd") is not None
        and dst.get("market_value_usd") == src.get("market_value_usd")
        and _is_disclosed_dollar_source(str(src.get("_source") or src.get("note") or ""))
    ):
        _record_value_provenance(dst, src)


def _merge_period_rows(*row_groups: list[dict]) -> list[dict]:
    """Merge period snapshots: collide on CUSIP/ticker/name; ticker dedupes 13F+13G.

    First group (13F) wins share/$ fields. Later groups fill ownership and
    disclosed FV onto existing rows. Same ticker under a different key (e.g. 13F
    CUSIP vs 13G ticker-only) must not create a second row.
    """
    from .lookback import stamp_missing_tickers

    out: list[dict] = []
    seen_keys: set[str] = set()
    ticker_idx: dict[str, int] = {}

    stamped_groups = [stamp_missing_tickers(list(g or [])) for g in row_groups]
    for group in stamped_groups:
        for r in group:
            k = _key(r)
            if k in {"t:", "c:", "n:"}:
                continue
            t = (r.get("investee_ticker") or "").strip().upper()

            if k in seen_keys:
                # Same identity key — fill gaps on the existing row
                for i, existing in enumerate(out):
                    if _key(existing) == k:
                        _fill_missing(existing, r)
                        break
                continue

            if t and t in ticker_idx:
                existing = out[ticker_idx[t]]
                _fill_missing(existing, r)
                # Also remember this key so later exact-key hits fill the same row
                seen_keys.add(k)
                continue

            row = dict(r)
            out.append(row)
            seen_keys.add(k)
            if t:
                ticker_idx[t] = len(out) - 1
    return out


def _f(v) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def price_history_rows(history: list[dict]) -> list[dict]:
    """Normalize QoQ rows for export — never invent stake $ via OTC×shares.

    Valuation order is enforced upstream: 13F market_value → Investments-table
    FV → else leave null (omit from chart). Exits are forced to 0.
    """
    from .lookback import coalesce_period_ticker

    out: list[dict] = []
    for r in coalesce_period_ticker(history):
        row = dict(r)
        if str(row.get("action") or "") == "exit":
            row["market_value_usd"] = 0.0
        out.append(row)
    return out


def reconcile_13f_vs_investments(
    rows_13f: list[dict],
    rows_investments: list[dict],
    *,
    tolerance_pct: float = 0.05,
    tolerance_usd: float = 50_000_000.0,
) -> list[dict]:
    """Cross-check tickers present on both 13F and Investments table.

    Returns mismatch dicts. Names only on Investments (e.g. DIDIY) are not
    mismatches — they must use table FV, not 13F.
    """
    def by_ticker(rows: list[dict]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for r in rows:
            t = (r.get("investee_ticker") or "").strip().upper()
            if not t:
                continue
            out[t] = r
        return out

    f13 = by_ticker(rows_13f)
    inv = by_ticker(rows_investments)
    reports: list[dict] = []
    for t in sorted(set(f13) & set(inv)):
        a = _f(f13[t].get("market_value_usd"))
        b = _f(inv[t].get("market_value_usd") or inv[t].get("fair_value_disclosed_usd"))
        if a is None or b is None:
            continue
        diff = abs(a - b)
        denom = max(abs(a), abs(b), 1.0)
        ok = diff <= tolerance_usd or (diff / denom) <= tolerance_pct
        if not ok:
            reports.append(
                {
                    "ticker": t,
                    "13f_usd": a,
                    "investments_usd": b,
                    "abs_diff": diff,
                    "ok": False,
                }
            )
    return reports


def build_13f_history(
    *,
    parent_ticker: str,
    edgar,
    as_of: str | None = None,
    max_filings: int = 80,
    lookback_years: int = 5,
) -> tuple[list[dict], dict[str, Any]]:
    """Fetch historical 13F-HR snapshots and emit QoQ position rows (13F only)."""
    from .edgar_urls import stamp_filing_urls
    from .lookback import lookback_start_date
    from .runner import PARENT_CIK_OVERRIDES

    start = lookback_start_date(as_of=as_of, lookback_years=lookback_years)
    ordered, meta = _collect_13f_periods(
        parent_ticker=parent_ticker,
        edgar=edgar,
        as_of=as_of,
        max_filings=max_filings,
        lookback_start=start,
    )
    meta["lookback_start"] = start
    meta["lookback_years"] = lookback_years
    if meta.get("error"):
        return [], meta
    cik = PARENT_CIK_OVERRIDES.get(parent_ticker) or edgar.get_cik(parent_ticker) or meta.get("cik")
    history = stamp_filing_urls(diff_snapshots(parent_ticker, ordered), cik=cik)
    return history, meta


def build_holdings_history(
    *,
    parent_ticker: str,
    edgar,
    as_of: str | None = None,
    max_filings: int = 80,
    max_annual_filings: int | None = None,
    lookback_years: int = 5,
) -> tuple[list[dict], dict[str, Any]]:
    """Fan-out QoQ: 13F periods (or 13G dates) + overlay 13G/D + hardened notes."""
    from .edgar_urls import stamp_filing_urls
    from .lookback import date_on_or_after, lookback_start_date
    from .parents import history_strategy, normalize_parent, uses_hk_aggregates
    from .runner import PARENT_CIK_OVERRIDES
    from .sec_13g import collect_13g_period_snapshots, exited_tickers_as_of, positions_as_of

    parent = normalize_parent(parent_ticker)
    strategy = history_strategy(parent)
    ua = getattr(edgar, "user_agent", None) or ""
    cik = PARENT_CIK_OVERRIDES.get(parent) or edgar.get_cik(parent)
    start = lookback_start_date(as_of=as_of, lookback_years=lookback_years)
    if max_annual_filings is None:
        max_annual_filings = max(12, int(lookback_years) * 4)

    # HK aggregate parents: 13G/D timeline (+ existing Tencent builder for live; history = 13G)
    if uses_hk_aggregates(parent):
        from .tencent import build_13g_reporter_history

        hist, meta = build_13g_reporter_history(
            parent_ticker=parent,
            user_agent=ua,
            max_filings=max(max_filings, 80),
            lookback_start=start,
        )
        meta["strategy"] = strategy
        meta["lookback_start"] = start
        meta["lookback_years"] = lookback_years
        return stamp_filing_urls(price_history_rows(hist), cik=cik), meta

    ordered, meta = _collect_13f_periods(
        parent_ticker=parent,
        edgar=edgar,
        as_of=as_of,
        max_filings=max_filings,
        lookback_start=start,
    )
    meta["strategy"] = strategy
    meta["lookback_start"] = start
    meta["lookback_years"] = lookback_years

    # Drop periods before the year window (defense in depth)
    ordered = [
        t for t in ordered if date_on_or_after(t[0], start) or date_on_or_after(t[1], start)
    ]

    # Sparse inception backfill: edge-held names at window cut → earlier 13Fs.
    from .inception import (
        collect_prewindow_edge_periods,
        edge_held_tickers,
        stamp_truncated_notes,
    )

    truncated: set[str] = set()
    pre_periods: list[tuple[str, str, str, list[dict]]] = []
    if ordered and lookback_years > 0:
        edge = edge_held_tickers(ordered[0][3])
        meta["edge_held_tickers"] = sorted(edge)
        if edge:
            pre_periods, truncated, pre_meta = collect_prewindow_edge_periods(
                parent_ticker=parent,
                edgar=edgar,
                window_start=start,
                edge=edge,
                as_of=as_of,
                max_filings=max(max_filings, 80),
            )
            meta["num_prewindow_periods"] = pre_meta.get("num_prewindow_periods")
            meta["truncated_tickers"] = sorted(truncated)
            if pre_meta.get("error"):
                meta["inception_error"] = pre_meta["error"]

    g13_snaps: list[tuple[str, str, str, list[dict]]] = []
    exited_by_date: dict[str, list[str]] = {}
    if cik:
        g13_snaps, g_meta = collect_13g_period_snapshots(
            cik=cik,
            parent_ticker=parent,
            user_agent=ua,
            max_filings=max(max_filings, 80),
            lookback_start=start,
        )
        meta["num_13g_filings"] = g_meta.get("num_filings")
        meta["num_13g_periods"] = g_meta.get("num_periods")
        exited_by_date = dict(g_meta.get("exited_by_date") or {})
        if g_meta.get("error"):
            meta["13g_error"] = g_meta["error"]

    g13_snaps = [t for t in g13_snaps if date_on_or_after(t[0], start) or date_on_or_after(t[1], start)]

    # If no 13F periods, use 13G filing dates as the grid
    if not ordered:
        if g13_snaps:
            history = stamp_filing_urls(
                price_history_rows(diff_snapshots(parent, g13_snaps)), cik=cik
            )
            meta["grid"] = "13g"
            meta["num_periods"] = len(g13_snaps)
            return history, meta
        meta["error"] = meta.get("error") or "no 13F or 13G periods"
        return [], meta

    note_snaps, note_meta = collect_note_snapshots(
        parent_ticker=parent,
        edgar=edgar,
        as_of=as_of,
        max_filings=max_annual_filings,
        lookback_start=start,
    )
    meta["num_annual_filings"] = note_meta.get("num_annual_filings")
    meta["num_note_snapshots"] = note_meta.get("num_note_snapshots")

    # Pre-window: 13F edge rows only (no 13G/note overlays — cost/inception only).
    enriched: list[tuple[str, str, str, list[dict]]] = list(pre_periods)

    for period_end, filing_date, accession, rows in ordered:
        as_of_d = filing_date or period_end
        g13 = positions_as_of(g13_snaps, as_of_d)
        notes = _notes_as_of(note_snaps, as_of_d, period_end=period_end, window=8)
        # 13G/D cessation must not be resurrected by a stale 20-F/% note (BILI class).
        exited = exited_tickers_as_of(exited_by_date, as_of_d)
        if exited:
            notes = [
                n
                for n in notes
                if (n.get("investee_ticker") or "").strip().upper() not in exited
            ]
        # 13F first (wins collisions), then 13G, then notes (FV fill for DIDIY etc.)
        enriched.append(
            (period_end, filing_date, accession, _merge_period_rows(rows, g13, notes))
        )

    meta["num_periods"] = len(enriched)
    meta["num_window_periods"] = len(ordered)
    history = stamp_filing_urls(price_history_rows(diff_snapshots(parent, enriched)), cik=cik)
    if truncated:
        history = stamp_truncated_notes(history, truncated)
    meta["grid"] = "13f"
    meta["num_note_history_rows"] = sum(
        1
        for r in history
        if any(x in str(r.get("note") or "") for x in ("20f_note", "10k_note", "10q_note"))
    )
    meta["num_13g_history_rows"] = sum(
        1 for r in history if "source=13g" in str(r.get("note") or "")
    )
    meta["num_priced_rows"] = sum(
        1 for r in history if "priced=" in str(r.get("note") or "")
    )
    return history, meta
