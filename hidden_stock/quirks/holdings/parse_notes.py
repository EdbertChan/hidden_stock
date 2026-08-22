"""Deterministic 10-K / 20-F / 10-Q investment-note parsers (no LLM)."""

from __future__ import annotations

import html as html_lib
import re

from .extract import load_investee_aliases, ownership_disclosure_slices, resolve_investee_ticker

_MONTH_NAMES = {
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
}

# Common Investments-table labels → ticker (OTC / listed). Extend via aliases.yaml.
_INVESTMENTS_TABLE_TICKERS: dict[str, str] = {
    "didi": "DIDIY",
    "grab": "GRAB",
    "aurora": "AUR",
    "delivery hero": "DHER.DE",
    "recursio": "RXRX",
    "recursion": "RXRX",
}

_MONTH_TO_NUM = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}

_AS_OF_DATES_RE = re.compile(
    r"(?:As\s+of\s+)?(?P<m1>January|February|March|April|May|June|July|August|"
    r"September|October|November|December)\s+(?P<d1>\d{1,2}),\s+(?P<y1>\d{4})"
    r"\s+(?P<m2>January|February|March|April|May|June|July|August|"
    r"September|October|November|December)\s+(?P<d2>\d{1,2}),\s+(?P<y2>\d{4})",
    re.IGNORECASE,
)

_INVESTMENTS_ROW_RE = re.compile(
    r"(?P<name>Didi|Grab|Aurora|Delivery\s+Hero|Recursion)"
    r"(?:\s*\([^)]*\))?"
    r"\s*\$?\s*(?P<a>[\d,]+(?:\.\d+)?)"
    r"\s*\$?\s*(?P<b>[\d,]+(?:\.\d+)?)",
    re.IGNORECASE,
)


def strip_xbrl_member_soup(text: str) -> str:
    """Drop dense XBRL Member tag runs so prose notes are searchable."""
    if not text:
        return ""
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


def _junk_short(short: str) -> bool:
    s = (short or "").strip()
    if len(s) <= 2:
        return True
    if s.lower() in _MONTH_NAMES:
        return True
    if re.fullmatch(r"[eE]\s*&?", s):
        return True
    return False


def _html_row_plain(row_html: str) -> str:
    plain = html_lib.unescape(re.sub(r"<[^>]+>", " ", row_html))
    return re.sub(r"\s+", " ", plain).strip()


def _parse_mdy_date(month: str, day: str, year: str) -> str:
    mm = _MONTH_TO_NUM[month.lower()]
    return f"{int(year):04d}-{mm:02d}-{int(day):02d}"


def _millions_to_usd(raw: str) -> float | None:
    try:
        return float(raw.replace(",", "")) * 1_000_000.0
    except (TypeError, ValueError):
        return None


def parse_investments_table(
    text: str,
    *,
    parent_ticker: str,
    form: str | None = None,
    accession_no: str | None = None,
    filing_date: str | None = None,
    aliases: dict[str, str] | None = None,
) -> list[dict]:
    """Parse Investments / non-marketable equity securities tables ($ millions).

    Returns one row per investee × column date with ``fair_value_disclosed_usd``
    in dollars (table figures are almost always in millions).
    """
    if not text:
        return []

    aliases = aliases if aliases is not None else load_investee_aliases()
    form_u = (form or "").upper()
    if form_u == "20-F":
        source_label = "20f_investments_table"
    elif form_u == "10-Q":
        source_label = "10q_investments_table"
    elif form_u == "10-K":
        source_label = "10k_investments_table"
    else:
        source_label = "investments_table"

    # Prefer HTML <tr> rows near an As-of two-date header; fall back to prose.
    rows_out: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def resolve_ticker(name: str) -> str | None:
        key = re.sub(r"\s+", " ", name.strip().lower())
        if key in _INVESTMENTS_TABLE_TICKERS:
            return _INVESTMENTS_TABLE_TICKERS[key]
        return resolve_investee_ticker(name, None, aliases)

    def add_pair(name: str, date_a: str, usd_a: float | None, date_b: str, usd_b: float | None, quote: str) -> None:
        ticker = resolve_ticker(name)
        for as_of, usd in ((date_a, usd_a), (date_b, usd_b)):
            if usd is None or not as_of:
                continue
            key = ((ticker or name).upper(), as_of)
            if key in seen:
                continue
            seen.add(key)
            rows_out.append(
                {
                    "parent_ticker": parent_ticker,
                    "investee_name": name.strip(),
                    "investee_ticker": ticker,
                    "ownership_pct": None,
                    "carrying_usd": usd,
                    "fair_value_disclosed_usd": usd,
                    "as_of_date": as_of,
                    "as_of_accession_no": accession_no,
                    "first_filing_date": filing_date,
                    "first_accession_no": accession_no,
                    "influence_disclosed": False,
                    "filing_gaap_hint": None,
                    "source_quote": quote[:400],
                    "confidence": "high",
                    "note": f"source={source_label} as_of={as_of} fv_usd={usd:.0f}",
                    "_source": source_label,
                }
            )

    # Scan tables: header dates then named investee rows.
    for table_m in re.finditer(r"<table\b[^>]*>(.*?)</table>", text, re.IGNORECASE | re.DOTALL):
        table_html = table_m.group(0)
        table_plain = _html_row_plain(table_html)
        if not re.search(
            r"Non-marketable equity securities|Investments\b|equity securities",
            table_plain,
            re.IGNORECASE,
        ):
            # Still accept if Didi/Grab/Aurora rows + As of dates appear
            if not re.search(r"\b(Didi|Grab|Aurora)\b", table_plain, re.IGNORECASE):
                continue
        dm = _AS_OF_DATES_RE.search(table_plain)
        if not dm:
            continue
        date_a = _parse_mdy_date(dm.group("m1"), dm.group("d1"), dm.group("y1"))
        date_b = _parse_mdy_date(dm.group("m2"), dm.group("d2"), dm.group("y2"))
        for tr in re.finditer(r"<tr\b[^>]*>(.*?)</tr>", table_html, re.IGNORECASE | re.DOTALL):
            plain = _html_row_plain(tr.group(1))
            rm = _INVESTMENTS_ROW_RE.search(plain)
            if not rm:
                continue
            add_pair(
                rm.group("name"),
                date_a,
                _millions_to_usd(rm.group("a")),
                date_b,
                _millions_to_usd(rm.group("b")),
                plain,
            )

    if rows_out:
        return rows_out

    # Fallback: stripped prose (markdown / plain)
    prose = strip_xbrl_member_soup(text)
    prose = html_lib.unescape(re.sub(r"<[^>]+>", " ", prose))
    prose = re.sub(r"\s+", " ", prose)
    dm = _AS_OF_DATES_RE.search(prose)
    if not dm:
        return []
    date_a = _parse_mdy_date(dm.group("m1"), dm.group("d1"), dm.group("y1"))
    date_b = _parse_mdy_date(dm.group("m2"), dm.group("d2"), dm.group("y2"))
    # Restrict search window after the As-of header
    window = prose[dm.start() : dm.start() + 4000]
    for rm in _INVESTMENTS_ROW_RE.finditer(window):
        add_pair(
            rm.group("name"),
            date_a,
            _millions_to_usd(rm.group("a")),
            date_b,
            _millions_to_usd(rm.group("b")),
            rm.group(0),
        )
    return rows_out


def parse_investment_notes(
    text: str,
    *,
    parent_ticker: str,
    form: str | None = None,
    accession_no: str | None = None,
    filing_date: str | None = None,
    aliases: dict[str, str] | None = None,
) -> list[dict]:
    """Extract named investment subsections and Ant-style ownership disclosures.

    ``Investment in X ("Short")`` hits require ownership % or USD amount in the
    nearby window, and reject calendar-month / junk short labels (APRIL false positives).
    """
    if not text:
        return []

    aliases = aliases if aliases is not None else load_investee_aliases()
    prose = strip_xbrl_member_soup(text)
    form_u = (form or "").upper()
    if form_u == "20-F":
        source_label = "20f_note"
    elif form_u == "10-Q":
        source_label = "10q_note"
    elif form_u == "10-K":
        source_label = "10k_note"
    else:
        source_label = "filing_note"

    by_key: dict[str, dict] = {}

    def add(row: dict) -> None:
        name = (row.get("investee_name") or "").strip()
        if not name:
            return
        # Require economic signal for note rows (%, carrying, or disclosed FV)
        if (
            row.get("ownership_pct") is None
            and row.get("carrying_usd") is None
            and row.get("fair_value_disclosed_usd") is None
        ):
            return
        # Investments-table rows are keyed by name + as_of (multi-period columns)
        as_of = (row.get("as_of_date") or "")[:10]
        key = (
            f"{name.lower()}|{as_of}"
            if as_of and "investments_table" in str(row.get("_source") or "")
            else name.lower()
        )
        prev = by_key.get(key)
        if prev is None:
            by_key[key] = row
            return
        score = lambda r: (
            (r.get("ownership_pct") is not None)
            + (r.get("carrying_usd") is not None)
            + (r.get("fair_value_disclosed_usd") is not None)
        )
        if score(row) >= score(prev):
            by_key[key] = {**prev, **{k: v for k, v in row.items() if v is not None}}

    for m in _INVESTMENT_IN_RE.finditer(prose):
        start = m.start()
        window = prose[start : start + 900]
        name = m.group("name").strip()
        short = m.group("short").strip()
        if _junk_short(short) or (name.split()[0].lower() in _MONTH_NAMES if name else False):
            continue
        pct = _pct_from_window(window)
        usd = _usd_from_window(window)
        if pct is None and usd is None:
            continue
        ticker = resolve_investee_ticker(name, None, aliases)
        if not ticker:
            ticker = resolve_investee_ticker(short, None, aliases)

        influence = bool(pct is not None and pct >= 20)
        add(
            {
                "parent_ticker": parent_ticker,
                "investee_name": name,
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

    for m in _ANT_NEWLY_ISSUED_RE.finditer(prose):
        name = re.sub(r"\s+", " ", m.group("name")).strip()
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

    for inv in parse_investments_table(
        text,
        parent_ticker=parent_ticker,
        form=form,
        accession_no=accession_no,
        filing_date=filing_date,
        aliases=aliases,
    ):
        add(inv)

    return list(by_key.values())
