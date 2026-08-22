"""LLM + EDGAR extraction of equity holdings from filings."""

from __future__ import annotations

from pathlib import Path

import yaml

HOLDINGS_KEYWORDS = (
    "equity method",
    "equity-method",
    "equity method investee",
    "significant influence",
    "fair value",
    "available-for-sale",
    "marketable securities",
    "investments in affiliates",
    "investments in equity",
    "equity securities and other investments",
    "schedule of investments",
    "consolidated subsidiaries",
    "ownership interest",
    "equity interest",
    "carrying value",
    "carrying amount",
    "cost method",
    "measurement alternative",
    "ant group",
    "kraft heinz",
    "share of results of equity method",
)

HOLDINGS_PRIORITY_KEYWORDS = (
    "equity method investee",
    "equity securities and other investments",
    "significant influence",
    "schedule of investments",
    "measurement alternative",
    "equity interest",
    "ant group",
    "kraft heinz",
    "carrying amount of",
)

EXTRACT_HOLDINGS_PROMPT_TEMPLATE = """You extract disclosed equity investments / affiliates from an SEC filing excerpt for {ticker}.

Return JSON only:
{{
  "holdings": [
    {{
      "investee_name": "<string>",
      "investee_ticker": "<string or null — only if clearly stated; never invent>",
      "investee_exchange": "<string or null>",
      "ownership_pct": <number or null — percent 0-100>,
      "shares_held": <number or null>,
      "acquired_date": "<YYYY-MM-DD or null>",
      "carrying_usd": <number or null — raw USD not millions>,
      "fair_value_disclosed_usd": <number or null>,
      "influence_disclosed": <true|false>,
      "consolidated_disclosed": <true|false>,
      "fair_value_through_earnings": <true|false>,
      "measurement_alternative": <true|false>,
      "filing_gaap_hint": "<fv_ni|equity_method|consolidated|cost|null>",
      "source_quote": "<short quote>",
      "confidence": "<high|medium|low>",
      "note": "<string or null>"
    }}
  ],
  "notes": "<string or null>"
}}

Rules:
- Include named equity investees even when only ownership % is disclosed (e.g. Ant Group 33%).
- Prefer material named investees over glossary definitions.
- Do not invent tickers or dollar amounts.
- ownership_pct is a percent (e.g. 33 for 33%), not a fraction.
- If amounts are in RMB millions / USD millions, convert to raw USD when the excerpt states the USD figure; otherwise leave null and note the units.
- If the excerpt has no investment disclosures, return {{"holdings": [], "notes": "none found"}}.

FILING EXCERPT:
{filing_text}
"""


OWNERSHIP_SLICE_PATTERNS = (
    # High-signal ownership-% narrative (Ant Group / 20-F style).
    r"newly issued \d+(?:\.\d+)?%\s+equity interest",
    r"we held\s+\d+(?:\.\d+)?%",
    r"held approximately\s+\d+(?:\.\d+)?%",
    r"ownership interest of approximately\s+\d+(?:\.\d+)?%",
    r"equity interest \(on a fully diluted basis\)",
)

OWNERSHIP_SLICE_FALLBACK_PATTERNS = (
    r"investments in equity method",
    r"equity method investees",
)


def ownership_disclosure_slices(text: str, window: int = 4500, max_chars: int = 20000) -> str:
    """Always keep windows around ownership-% disclosures (20-F narrative)."""
    import re

    if not text:
        return ""

    def collect(patterns: tuple[str, ...]) -> list[tuple[int, int]]:
        spans: list[tuple[int, int]] = []
        for pat in patterns:
            for m in re.finditer(pat, text, flags=re.IGNORECASE):
                spans.append((max(0, m.start() - window), min(len(text), m.end() + window)))
        return spans

    spans = collect(OWNERSHIP_SLICE_PATTERNS)
    if not spans:
        spans = collect(OWNERSHIP_SLICE_FALLBACK_PATTERNS)
    if not spans:
        return ""
    # Prefer later narrative notes over early glossary / VIE boilerplate:
    # sort by start descending for packing, then re-order for readability.
    spans = sorted(spans, key=lambda s: s[0], reverse=True)
    packed: list[tuple[int, int]] = []
    total = 0
    for lo, hi in spans:
        size = hi - lo
        if total + size > max_chars and packed:
            continue
        if total + size > max_chars:
            lo = hi - (max_chars - total)
            size = hi - lo
        packed.append((lo, hi))
        total += size
        if total >= max_chars:
            break
    packed.sort()
    merged: list[list[int]] = [list(packed[0])]
    for lo, hi in packed[1:]:
        if lo <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    return "\n\n---\n\n".join(text[lo:hi] for lo, hi in merged)


def load_investee_aliases(path: Path | None = None) -> dict[str, str]:
    """Map lowercase investee name → ticker (e.g. 'pinduoduo' → 'PDD')."""
    if path is None:
        path = Path(__file__).resolve().parent / "investee_aliases.yaml"
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    out: dict[str, str] = {}
    for name, ticker in (data.get("aliases") or {}).items():
        if ticker is None:
            continue
        out[str(name).strip().lower()] = str(ticker).strip().upper()
    return out


def resolve_investee_ticker(
    name: str | None, ticker: str | None, aliases: dict[str, str]
) -> str | None:
    if ticker:
        t = str(ticker).strip().upper()
        # Keep exchange suffixes like 3690.HK; normalize share-class dots.
        if t.endswith(".HK") or t.endswith(".US") or t.endswith(".L"):
            return t
        return t.replace(".", "-")
    if not name:
        return None
    key = str(name).strip().lower()
    if key in aliases:
        return aliases[key]
    for alias, sym in aliases.items():
        if alias in key or key in alias:
            return sym
    return None
