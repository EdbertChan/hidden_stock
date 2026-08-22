"""Deterministic 10-K / 20-F investment-note parsers (no LLM)."""

from __future__ import annotations

import re

from .extract import load_investee_aliases, ownership_disclosure_slices, resolve_investee_ticker


def strip_xbrl_member_soup(text: str) -> str:
    """Drop dense XBRL Member tag runs so prose notes are searchable."""
    if not text:
        return ""
    # Collapse long runs of ...Member tokens
    cleaned = re.sub(
        r"(?:[A-Za-z0-9:._-]*Member\s+){3,}",
        " ",
        text,
    )
    cleaned = re.sub(r"\s{3,}", "\n", cleaned)
    return cleaned


_INVESTMENT_IN_RE = re.compile(
    r"Investment\s+in\s+(?P<name>[A-Za-z0-9][A-Za-z0-9 .,&'\-]{1,120}?)"
    r"\s*\([“\"'](?P<short>[^”\"']+)[”\"']\)",
    re.IGNORECASE,
)

_PCT_EQUITY_RE = re.compile(
    r"approximately\s+(?P<pct>\d+(?:\.\d+)?)\s*%\s+equity\s+interest",
    re.IGNORECASE,
)

_USD_BILLION_RE = re.compile(
    r"(?:invested|investment).*?US\s*\$\s*(?P<amt>\d+(?:\.\d+)?)\s*billion",
    re.IGNORECASE | re.DOTALL,
)

_USD_MILLION_RE = re.compile(
    r"(?:invested|investment).*?US\s*\$\s*(?P<amt>\d+(?:\.\d+)?)\s*million",
    re.IGNORECASE | re.DOTALL,
)

_ANT_NEWLY_ISSUED_RE = re.compile(
    r"newly\s+issued\s+(?P<pct>\d+(?:\.\d+)?)\s*%\s+equity\s+interest"
    r"[^.]{0,120}?(?P<name>Ant\s+Group[^.;,]{0,40})",
    re.IGNORECASE | re.DOTALL,
)

_WE_HELD_ANT_RE = re.compile(
    r"we\s+held\s+(?P<pct>\d+(?:\.\d+)?)\s*%[^.]{0,120}?Ant\s+Group",
    re.IGNORECASE | re.DOTALL,
)


def _usd_from_window(window: str) -> float | None:
    m = _USD_BILLION_RE.search(window)
    if m:
        return float(m.group("amt")) * 1_000_000_000
    m = _USD_MILLION_RE.search(window)
    if m:
        return float(m.group("amt")) * 1_000_000
    return None


def _pct_from_window(window: str) -> float | None:
    m = _PCT_EQUITY_RE.search(window)
    if m:
        return float(m.group("pct"))
    return None


def parse_investment_notes(
    text: str,
    *,
    parent_ticker: str,
    form: str | None = None,
    accession_no: str | None = None,
    filing_date: str | None = None,
    aliases: dict[str, str] | None = None,
) -> list[dict]:
    """Extract named investment subsections and Ant-style ownership disclosures."""
    if not text:
        return []

    aliases = aliases if aliases is not None else load_investee_aliases()
    prose = strip_xbrl_member_soup(text)
    source_label = "20f_note" if (form or "").upper() == "20-F" else "10k_note"
    if form and form.upper() not in {"20-F", "10-K", "10-Q"}:
        source_label = "filing_note"

    by_key: dict[str, dict] = {}

    def add(row: dict) -> None:
        name = (row.get("investee_name") or "").strip()
        if not name:
            return
        key = name.lower()
        prev = by_key.get(key)
        if prev is None:
            by_key[key] = row
            return
        # Prefer row with ownership_pct and/or carrying
        score = lambda r: (r.get("ownership_pct") is not None) + (r.get("carrying_usd") is not None)
        if score(row) >= score(prev):
            by_key[key] = {**prev, **{k: v for k, v in row.items() if v is not None}}

    # (d) Investment in Moonshot AI Ltd ("Moonshot") …
    for m in _INVESTMENT_IN_RE.finditer(prose):
        start = m.start()
        window = prose[start : start + 900]
        name = m.group("name").strip()
        short = m.group("short").strip()
        full_name = name
        pct = _pct_from_window(window)
        usd = _usd_from_window(window)
        ticker = resolve_investee_ticker(full_name, None, aliases)
        if not ticker:
            ticker = resolve_investee_ticker(short, None, aliases)

        # Equity % >= 20 → influence; else often FV or cost — leave to GAAP defaults
        influence = bool(pct is not None and pct >= 20)
        add(
            {
                "parent_ticker": parent_ticker,
                "investee_name": full_name,
                "investee_ticker": ticker,
                "ownership_pct": pct,
                "carrying_usd": usd,
                "as_of_date": filing_date,
                "as_of_accession_no": accession_no,
                "first_filing_date": filing_date,
                "first_accession_no": accession_no,
                "influence_disclosed": influence,
                "filing_gaap_hint": "equity_method" if influence else None,
                "source_quote": re.sub(r"\s+", " ", window[:400]).strip(),
                "confidence": "high",
                "note": f"source={source_label} short={short}",
                "_source": source_label,
            }
        )

    # Ant Group / newly issued %
    for m in _ANT_NEWLY_ISSUED_RE.finditer(prose):
        name = re.sub(r"\s+", " ", m.group("name")).strip()
        # Normalize trailing junk
        name = re.split(r"\s+following|\s+in\s+", name, maxsplit=1)[0].strip()
        if "Ant" not in name and "ant" not in name.lower():
            name = "Ant Group"
        window = prose[m.start() : m.start() + 500]
        add(
            {
                "parent_ticker": parent_ticker,
                "investee_name": "Ant Group Co., Ltd." if "Ant" in name else name,
                "investee_ticker": None,
                "ownership_pct": float(m.group("pct")),
                "influence_disclosed": True,
                "filing_gaap_hint": "equity_method",
                "as_of_date": filing_date,
                "as_of_accession_no": accession_no,
                "first_filing_date": filing_date,
                "first_accession_no": accession_no,
                "source_quote": re.sub(r"\s+", " ", window[:350]).strip(),
                "confidence": "high",
                "note": f"source={source_label}",
                "_source": source_label,
            }
        )

    for m in _WE_HELD_ANT_RE.finditer(prose):
        window = prose[m.start() : m.start() + 400]
        add(
            {
                "parent_ticker": parent_ticker,
                "investee_name": "Ant Group Co., Ltd.",
                "investee_ticker": None,
                "ownership_pct": float(m.group("pct")),
                "influence_disclosed": True,
                "filing_gaap_hint": "equity_method",
                "as_of_date": filing_date,
                "as_of_accession_no": accession_no,
                "first_filing_date": filing_date,
                "first_accession_no": accession_no,
                "source_quote": re.sub(r"\s+", " ", window[:350]).strip(),
                "confidence": "high",
                "note": f"source={source_label}",
                "_source": source_label,
            }
        )

    # Also scan ownership disclosure slices for Ant-style language if full text missed
    if not any("ant" in k for k in by_key):
        owned = ownership_disclosure_slices(prose, max_chars=24000)
        if owned:
            for m in _ANT_NEWLY_ISSUED_RE.finditer(owned):
                add(
                    {
                        "parent_ticker": parent_ticker,
                        "investee_name": "Ant Group Co., Ltd.",
                        "ownership_pct": float(m.group("pct")),
                        "influence_disclosed": True,
                        "filing_gaap_hint": "equity_method",
                        "as_of_date": filing_date,
                        "as_of_accession_no": accession_no,
                        "first_filing_date": filing_date,
                        "first_accession_no": accession_no,
                        "source_quote": m.group(0)[:350],
                        "confidence": "high",
                        "note": f"source={source_label}",
                        "_source": source_label,
                    }
                )

    return list(by_key.values())
