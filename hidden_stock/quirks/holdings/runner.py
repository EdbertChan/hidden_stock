"""End-to-end: 13F + Schedule 13D/G + 10-K/20-F/10-Q notes → holdings rows."""

from __future__ import annotations

import re
from typing import Any

from .extract import (
    EXTRACT_HOLDINGS_PROMPT_TEMPLATE,
    HOLDINGS_KEYWORDS,
    HOLDINGS_PRIORITY_KEYWORDS,
    load_investee_aliases,
    ownership_disclosure_slices,
    resolve_investee_ticker,
)
from .mtm import apply_gaap_and_adj
from .parse_notes import parse_investment_notes
from .parents import normalize_parent, uses_hk_aggregates
from .schema import HOLDINGS_COLUMNS, empty_holding_row
from .sec_13g import fetch_latest_13g_holdings
from .sec_api_13f import latest_filer_13f_rows

PARENT_CIK_OVERRIDES: dict[str, str] = {
    "TCEHY": "0001293451",
    "TCTZF": "0001293451",
}


def _norm_key(row: dict) -> str:
    ticker = (row.get("investee_ticker") or "").strip().upper()
    if ticker:
        return f"t:{ticker}"
    cusip = (row.get("_cusip") or row.get("cusip") or "").strip().upper()
    if cusip:
        return f"c:{cusip}"
    name = re.sub(r"[^a-z0-9]+", "", (row.get("investee_name") or "").lower())
    return f"n:{name}"


def _is_13f_source(src: str) -> bool:
    return src in {"13f", "sec_api_13f"} or str(src).startswith("13f") or str(src).startswith(
        "sec_api_13f"
    )


def _source_allows_chart_dollars(src: str | None, note: str | None = None) -> bool:
    """Only Investments-table (or 13F) may populate market_value from note fields."""
    blob = f"{src or ''} {note or ''}".lower()
    if "13f" in blob and "investments_table" not in blob:
        # 13F market_value is set directly; this gate is for note→$ mapping
        return False
    return "investments_table" in blob


def _apply_disclosed_fv(row: dict) -> None:
    """Map disclosed FV onto market_value when still null — never narrative cost alone."""
    if row.get("market_value_usd") is not None:
        return
    src = str(row.get("_source") or "")
    note = str(row.get("note") or "")
    if not _source_allows_chart_dollars(src, note):
        return
    fv = row.get("fair_value_disclosed_usd")
    if fv is None:
        fv = row.get("carrying_usd")
    if fv is not None:
        row["market_value_usd"] = fv


def merge_raw_holdings(parts: list[list[dict]]) -> list[dict]:
    """Dedupe by ticker/CUSIP/name; prefer 13F market fields when both exist.

    Collide on ticker so 13G cannot double-count a 13F name (AUR/GRAB). Map
    Investments-table disclosed FV onto market_value when still null — not
    narrative amount-invested / equity-method carrying alone.
    """
    merged: dict[str, dict] = {}
    ticker_key: dict[str, str] = {}
    priority = {
        "13f": 3,
        "sec_api_13f": 3,
        "20f_note": 2,
        "10k_note": 2,
        "10q_note": 2,
        "20f_investments_table": 2,
        "10k_investments_table": 2,
        "10q_investments_table": 2,
        "investments_table": 2,
        "filing_note": 1,
        "13g": 2,
        "hk_annual": 1,
    }

    for group in parts:
        for raw in group:
            key = _norm_key(raw)
            if not key or key in {"t:", "c:", "n:"}:
                continue
            src = str(raw.get("_source") or "")
            t = (raw.get("investee_ticker") or "").strip().upper()

            # Ticker collision across different keys (13F CUSIP vs 13G ticker)
            if t and t in ticker_key and ticker_key[t] != key:
                key = ticker_key[t]

            prev = merged.get(key)
            if prev is None:
                row = dict(raw)
                _apply_disclosed_fv(row)
                merged[key] = row
                if t:
                    ticker_key[t] = key
                continue
            out = dict(prev)
            prev_had_ownership_pct = prev.get("ownership_pct") is not None
            for k, v in raw.items():
                if v is None:
                    continue
                if out.get(k) is None:
                    out[k] = v
            if _is_13f_source(src):
                for k in (
                    "shares_held",
                    "market_value_usd",
                    "market_price",
                    "fair_value_disclosed_usd",
                    "investee_ticker",
                    "price_source",
                    "price_as_of",
                    "_cusip",
                    "cusip",
                ):
                    if raw.get(k) is not None:
                        out[k] = raw[k]
                prev_src = str(prev.get("_source") or "")
                out["_source"] = f"{src}+{prev_src}" if prev_src and prev_src != src else src
                note_prev = prev.get("note") or ""
                out["note"] = f"{note_prev}; merged 13f".strip("; ")
            else:
                for k in (
                    "ownership_pct",
                    "carrying_usd",
                    "fair_value_disclosed_usd",
                    "influence_disclosed",
                    "filing_gaap_hint",
                    "shares_held",
                ):
                    if raw.get(k) is not None and out.get(k) is None:
                        out[k] = raw[k]
                filled_ownership_pct = (
                    not prev_had_ownership_pct and raw.get("ownership_pct") is not None
                )
                if filled_ownership_pct and src:
                    # ownership_pct came from a non-13F source (e.g. 13G) merged
                    # onto a 13F-primary row — the note must not pretend it came
                    # from 13F alone (P5: provenance stamps travel with the value).
                    note = str(out.get("note") or "")
                    stamp = str(raw.get("note") or f"ownership_pct_source={src}")
                    if stamp not in note:
                        out["note"] = f"{note}; {stamp}".strip("; ") if note else stamp
                # Investments-table disclosed FV fills null 13G dollars (not narrative cost)
                filled_disclosed_mv = False
                if out.get("market_value_usd") is None:
                    if raw.get("market_value_usd") is not None and _source_allows_chart_dollars(
                        src, raw.get("note")
                    ):
                        out["market_value_usd"] = raw["market_value_usd"]
                        filled_disclosed_mv = True
                    elif raw.get("fair_value_disclosed_usd") is not None and _source_allows_chart_dollars(
                        src, raw.get("note")
                    ):
                        out["market_value_usd"] = raw["fair_value_disclosed_usd"]
                        filled_disclosed_mv = True
                    elif raw.get("carrying_usd") is not None and _source_allows_chart_dollars(
                        src, raw.get("note")
                    ):
                        out["market_value_usd"] = raw["carrying_usd"]
                        filled_disclosed_mv = True
                if filled_disclosed_mv and src:
                    # 13G is identity only — stamp $ provenance onto _source/note
                    prev_src = str(prev.get("_source") or "")
                    if src not in prev_src.split("+"):
                        out["_source"] = (
                            f"{prev_src}+{src}" if prev_src and prev_src != src else src
                        )
                    note = str(out.get("note") or "")
                    stamp = str(raw.get("note") or f"value_source={src}")
                    if "investments_table" not in note and "value_source=" not in note:
                        out["note"] = f"{note}; {stamp}".strip("; ") if note else stamp
                if priority.get(src, 0) >= priority.get(str(prev.get("_source")), 0):
                    if raw.get("source_quote"):
                        out["source_quote"] = raw["source_quote"]
            _apply_disclosed_fv(out)
            merged[key] = out
            if t:
                ticker_key[t] = key
    return list(merged.values())


def _finalize_rows(raw_rows: list[dict], *, skip_eodhd_for_13f: bool = True) -> list[dict]:
    """Finalize holdings. Never invent stake $ via EODHD for any source."""
    _ = skip_eodhd_for_13f  # retained for call-site compatibility; invent path removed
    rows: list[dict] = []
    for raw in raw_rows:
        base = empty_holding_row()
        for k, v in raw.items():
            if k.startswith("_"):
                continue
            if k in base or k in {
                "consolidated_disclosed",
                "fair_value_through_earnings",
                "measurement_alternative",
                "filing_gaap_hint",
            }:
                base[k] = v
        if base.get("market_value_usd") is None:
            _apply_disclosed_fv(base)
        elif not _is_13f_source(str(raw.get("_source") or "")) and not _source_allows_chart_dollars(
            raw.get("_source"), base.get("note")
        ):
            # Narrative cost / equity-method carrying must not remain as chart $
            base["market_value_usd"] = None
        enriched = apply_gaap_and_adj(base)
        if raw.get("_source") and enriched.get("note"):
            if f"source={raw['_source']}" not in str(enriched["note"]):
                enriched["note"] = f"source={raw['_source']}; {enriched['note']}"
        elif raw.get("_source"):
            enriched["note"] = f"source={raw['_source']}"
        rows.append({c: enriched.get(c) for c in HOLDINGS_COLUMNS})
    from .validate import scrub_live_pct_as_shares

    return scrub_live_pct_as_shares(rows)


def process_parent_holdings(
    *,
    parent_ticker: str,
    edgar,
    llm=None,
    as_of: str | None = None,
    price: float | None = None,
    book_value: float | None = None,
    shares: float | None = None,
    form_types: tuple[str, ...] = ("10-K", "20-F", "10-Q"),
    use_llm_fallback: bool = False,
) -> tuple[list[dict], dict[str, Any]]:
    """Fan-out: 13F + Schedule 13D/G + annual/interim notes → merge."""
    parent = normalize_parent(parent_ticker)
    if uses_hk_aggregates(parent):
        from .tencent import build_tencent_holdings

        ua = getattr(edgar, "user_agent", None) or ""
        return build_tencent_holdings(user_agent=ua)

    meta: dict[str, Any] = {
        "parent_ticker": parent,
        "as_of": as_of,
        "accession_no": None,
        "filing_date": None,
        "error": None,
        "num_raw": 0,
        "num_13f": 0,
        "num_13g": 0,
        "num_notes": 0,
        "used_llm": False,
        "sources": "13f+13g+notes",
    }
    cik = PARENT_CIK_OVERRIDES.get(parent) or edgar.get_cik(parent)
    if not cik:
        meta["error"] = "no CIK"
        return [], meta

    aliases = load_investee_aliases()
    raw_13f: list[dict] = []
    raw_13g: list[dict] = []
    raw_notes: list[dict] = []

    # --- Form 13F ---
    try:
        raw_13f, f13_meta = latest_filer_13f_rows(
            parent_ticker=parent, cik=cik, as_of=as_of
        )
        meta["num_13f"] = f13_meta.get("num_13f") or len(raw_13f)
        meta["13f_accession"] = f13_meta.get("13f_accession")
        meta["13f_filing_date"] = f13_meta.get("13f_filing_date")
        meta["13f_period_end"] = f13_meta.get("13f_period_end")
        if f13_meta.get("error") and not raw_13f:
            meta["13f_error"] = f13_meta["error"]
    except Exception as e:
        meta["13f_error"] = str(e)

    # --- Schedule 13D/G ---
    try:
        ua = getattr(edgar, "user_agent", None) or ""
        raw_13g, g_meta = fetch_latest_13g_holdings(
            cik=cik, parent_ticker=parent, user_agent=ua, max_filings=40
        )
        meta["num_13g"] = len(raw_13g)
        meta["13g_filings_scanned"] = g_meta.get("num_filings_scanned")
        if g_meta.get("error"):
            meta["13g_error"] = g_meta["error"]
    except Exception as e:
        meta["13g_error"] = str(e)

    # --- 10-K / 20-F / 10-Q notes ---
    annual_types = tuple(t for t in form_types if t in {"10-K", "20-F", "10-Q"}) or (
        "10-K",
        "20-F",
        "10-Q",
    )
    # Prefer annuals first, then a recent 10-Q
    annuals = edgar.list_filings(
        cik, form_types=tuple(t for t in annual_types if t != "10-Q") or ("10-K", "20-F"),
        as_of=as_of,
        limit=3,
    )
    if "10-Q" in annual_types:
        qs = edgar.list_filings(cik, form_types=("10-Q",), as_of=as_of, limit=1)
        annuals = list(annuals) + list(qs)

    filing = annuals[0] if annuals else None
    if filing:
        meta["accession_no"] = filing["accession_no"]
        meta["filing_date"] = filing["filing_date"]
    elif meta.get("13f_accession"):
        meta["accession_no"] = meta["13f_accession"]
        meta["filing_date"] = meta.get("13f_filing_date")

    for ann in annuals:
        html = edgar.fetch_filing_document(
            cik, ann["accession_no"], ann["primary_document"]
        )
        text = edgar.get_filing_text(html, max_chars=1_200_000)
        part = parse_investment_notes(
            text,
            parent_ticker=parent,
            form=ann.get("form"),
            accession_no=ann["accession_no"],
            filing_date=ann["filing_date"],
            aliases=aliases,
        )
        raw_notes.extend(part)

    meta["num_notes"] = len(raw_notes)
    meta["notes_filings_scanned"] = len(annuals)

    merged = merge_raw_holdings([raw_13f, raw_13g, raw_notes])
    meta["num_raw"] = len(merged)

    if use_llm_fallback and not merged and filing and llm is not None:
        meta["used_llm"] = True
        html = edgar.fetch_filing_document(cik, filing["accession_no"], filing["primary_document"])
        text = edgar.get_filing_text(html, max_chars=1_200_000)
        excerpt = edgar.extract_keyword_windows(
            text,
            keywords=HOLDINGS_KEYWORDS,
            priority_keywords=HOLDINGS_PRIORITY_KEYWORDS,
            max_total_chars=36000,
            window_chars=3500,
        )
        owned = ownership_disclosure_slices(text, max_chars=20000)
        if owned:
            excerpt = (excerpt + "\n\n---\n\n" + owned)[:56000]
        if hasattr(llm, "extract_equity_holdings"):
            payload = llm.extract_equity_holdings(parent, excerpt)
        elif hasattr(llm, "_generate_json"):
            prompt = EXTRACT_HOLDINGS_PROMPT_TEMPLATE.format(ticker=parent, filing_text=excerpt)
            payload = llm._generate_json(prompt)
        else:
            payload = {"holdings": []}
        for raw in payload.get("holdings") or []:
            if not isinstance(raw, dict):
                continue
            name = raw.get("investee_name")
            merged.append(
                {
                    "parent_ticker": parent,
                    "investee_name": name,
                    "investee_ticker": resolve_investee_ticker(
                        name, raw.get("investee_ticker"), aliases
                    ),
                    "ownership_pct": raw.get("ownership_pct"),
                    "shares_held": raw.get("shares_held"),
                    "carrying_usd": raw.get("carrying_usd"),
                    "fair_value_disclosed_usd": raw.get("fair_value_disclosed_usd"),
                    "influence_disclosed": bool(raw.get("influence_disclosed")),
                    "consolidated_disclosed": bool(raw.get("consolidated_disclosed")),
                    "fair_value_through_earnings": bool(raw.get("fair_value_through_earnings")),
                    "measurement_alternative": bool(raw.get("measurement_alternative")),
                    "filing_gaap_hint": raw.get("filing_gaap_hint"),
                    "as_of_date": as_of or (filing or {}).get("filing_date"),
                    "as_of_accession_no": (filing or {}).get("accession_no"),
                    "first_filing_date": (filing or {}).get("filing_date"),
                    "first_accession_no": (filing or {}).get("accession_no"),
                    "source_quote": raw.get("source_quote"),
                    "confidence": raw.get("confidence") or "low",
                    "note": raw.get("note"),
                    "_source": "llm_fallback",
                }
            )

    if not raw_13f and not raw_13g and not raw_notes:
        meta["error"] = meta.get("error") or "no holdings from 13F/13G/notes"

    rows = _finalize_rows(merged)
    return rows, meta
