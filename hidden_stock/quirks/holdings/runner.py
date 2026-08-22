"""End-to-end: deterministic 13F + 20-F/10-K notes → holdings rows."""

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
from .mtm import apply_gaap_and_adj, enrich_holding_mtm
from .parse_13f import parse_13f_infotable_xml
from .parse_notes import parse_investment_notes
from .schema import HOLDINGS_COLUMNS, empty_holding_row

PARENT_CIK_OVERRIDES: dict[str, str] = {
    "TCEHY": "0001293451",
    "TCTZF": "0001293451",
}


def _norm_key(row: dict) -> str:
    ticker = (row.get("investee_ticker") or "").strip().upper()
    if ticker:
        return f"t:{ticker}"
    cusip = (row.get("_cusip") or "").strip().upper()
    if cusip:
        return f"c:{cusip}"
    name = re.sub(r"[^a-z0-9]+", "", (row.get("investee_name") or "").lower())
    return f"n:{name}"


def merge_raw_holdings(parts: list[list[dict]]) -> list[dict]:
    """Dedupe by ticker/CUSIP/name; prefer 13F market fields when both exist."""
    merged: dict[str, dict] = {}
    priority = {"13f": 3, "20f_note": 2, "10k_note": 2, "filing_note": 1, "13g": 2, "hk_annual": 1}

    for group in parts:
        for raw in group:
            key = _norm_key(raw)
            if not key or key in {"t:", "c:", "n:"}:
                continue
            src = raw.get("_source") or ""
            prev = merged.get(key)
            if prev is None:
                merged[key] = dict(raw)
                continue
            out = dict(prev)
            # Fill missing fields from new row
            for k, v in raw.items():
                if v is None:
                    continue
                if out.get(k) is None:
                    out[k] = v
            # Prefer 13F for market/shares
            if src == "13f":
                for k in (
                    "shares_held",
                    "market_value_usd",
                    "market_price",
                    "fair_value_disclosed_usd",
                    "investee_ticker",
                    "price_source",
                    "price_as_of",
                    "_cusip",
                ):
                    if raw.get(k) is not None:
                        out[k] = raw[k]
                out["_source"] = "13f+note" if (prev.get("_source") or "").endswith("note") else "13f"
                note_prev = prev.get("note") or ""
                out["note"] = f"{note_prev}; merged 13f".strip("; ")
            else:
                # Prefer note ownership / carrying when 13F lacked them
                for k in ("ownership_pct", "carrying_usd", "influence_disclosed", "filing_gaap_hint"):
                    if raw.get(k) is not None and out.get(k) is None:
                        out[k] = raw[k]
                if priority.get(src, 0) >= priority.get(str(prev.get("_source")), 0):
                    if raw.get("source_quote"):
                        out["source_quote"] = raw["source_quote"]
            merged[key] = out
    return list(merged.values())


def _finalize_rows(raw_rows: list[dict], *, skip_eodhd_for_13f: bool = True) -> list[dict]:
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
        src = raw.get("_source") or ""
        # 13F already has market value from the filing — skip network MTM.
        if skip_eodhd_for_13f and src.startswith("13f") and base.get("market_value_usd") is not None:
            enriched = apply_gaap_and_adj(base)
        else:
            enriched = enrich_holding_mtm(base, as_of=base.get("as_of_date"))
        # Provenance in note
        if raw.get("_source") and enriched.get("note"):
            if f"source={raw['_source']}" not in str(enriched["note"]):
                enriched["note"] = f"source={raw['_source']}; {enriched['note']}"
        elif raw.get("_source"):
            enriched["note"] = f"source={raw['_source']}"
        rows.append({c: enriched.get(c) for c in HOLDINGS_COLUMNS})
    return rows


def process_parent_holdings(
    *,
    parent_ticker: str,
    edgar,
    llm=None,
    as_of: str | None = None,
    price: float | None = None,
    book_value: float | None = None,
    shares: float | None = None,
    form_types: tuple[str, ...] = ("10-K", "20-F"),
    use_llm_fallback: bool = False,
) -> tuple[list[dict], dict[str, Any]]:
    """Return (holding_rows, meta). Deterministic 13F + annual notes by default."""
    parent = parent_ticker.upper().replace(".", "-")
    if parent in {"TCEHY", "TCTZF", "0700"}:
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
        "num_notes": 0,
        "used_llm": False,
    }
    cik = PARENT_CIK_OVERRIDES.get(parent) or edgar.get_cik(parent)
    if not cik:
        meta["error"] = "no CIK"
        return [], meta

    aliases = load_investee_aliases()
    raw_13f: list[dict] = []
    raw_notes: list[dict] = []

    # --- 13F (US-listed stakes) ---
    f13 = edgar.get_latest_filing(cik, form_types=("13F-HR",), as_of=as_of)
    if f13:
        xml = edgar.fetch_13f_infotable(cik, f13["accession_no"])
        if xml:
            raw_13f = parse_13f_infotable_xml(
                xml,
                parent_ticker=parent,
                accession_no=f13["accession_no"],
                filing_date=f13["filing_date"],
                aliases=aliases,
            )
            meta["num_13f"] = len(raw_13f)
            meta["13f_accession"] = f13["accession_no"]
            meta["13f_filing_date"] = f13["filing_date"]

    # --- Annual investment notes (scan recent 10-K/20-F so newer filings
    # without a note do not hide Moonshot-style disclosures from prior year) ---
    annual_types = tuple(t for t in form_types if t in {"10-K", "20-F"}) or ("10-K", "20-F")
    annuals = edgar.list_filings(cik, form_types=annual_types, as_of=as_of, limit=3)
    if not annuals:
        annuals = edgar.list_filings(cik, form_types=("10-Q",), as_of=as_of, limit=1)

    filing = annuals[0] if annuals else None
    if filing:
        meta["accession_no"] = filing["accession_no"]
        meta["filing_date"] = filing["filing_date"]

    for ann in annuals:
        html = edgar.fetch_filing_document(cik, ann["accession_no"], ann["primary_document"])
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

    merged = merge_raw_holdings([raw_13f, raw_notes])
    meta["num_raw"] = len(merged)

    # Optional LLM fallback only when nothing deterministic found
    text = ""
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

    if not filing and not raw_13f:
        meta["error"] = meta.get("error") or "no filing"

    rows = _finalize_rows(merged)
    return rows, meta
