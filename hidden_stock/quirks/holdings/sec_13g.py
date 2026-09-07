"""Schedule 13D/G holdings for any filer CIK (XML + HTML)."""

from __future__ import annotations

import datetime as _dt
import logging
import re
import time
from typing import Any
from xml.etree import ElementTree as ET

import requests

from .identity import (
    ISSUER_TICKER_HINTS,
    assert_pct_domain,
    clean_issuer_name,
    holding_key,
    resolve_issuer_ticker,
)

_log = logging.getLogger(__name__)

# Re-export identity helpers for existing imports.
__all__ = [
    "ISSUER_TICKER_HINTS",
    "clean_issuer_name",
    "resolve_issuer_ticker",
    "forms_ok",
    "parse_13g_html",
    "parse_13g_xml",
    "parse_filing_body",
    "raw_to_position",
    "raw_to_live_row",
    "collect_13g_period_snapshots",
    "fetch_latest_13g_holdings",
    "exited_tickers_as_of",
    "positions_as_of",
    "event_date_for",
    "parse_date_token",
]

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


_MONTH_NUM = {
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

_DATE_TOKEN_RE = re.compile(
    r"(?P<mdy>(?P<mon>[A-Za-z]{3,9})\.?\s+(?P<d>\d{1,2}),?\s+(?P<y>\d{4}))"
    r"|(?P<dmy>(?P<d2>\d{1,2})\s+(?P<mon2>[A-Za-z]{3,9})\.?,?\s+(?P<y2>\d{4}))"
    r"|(?P<iso>(?P<yi>\d{4})-(?P<mi>\d{2})-(?P<di>\d{2}))"
    r"|(?P<slash>(?P<ms>\d{1,2})/(?P<ds>\d{1,2})/(?P<ys>\d{4}))"
)


def _local(tag: str) -> str:
    return tag.split("}")[-1]


def _month_num(name: str) -> int | None:
    low = (name or "").strip().lower().rstrip(".")
    for full, num in _MONTH_NUM.items():
        if low == full or (len(low) >= 3 and full.startswith(low)):
            return num
    return None


def _iso_date(m: re.Match) -> str | None:
    """Normalise one ``_DATE_TOKEN_RE`` match to YYYY-MM-DD (None if not a real date)."""
    try:
        if m.group("mdy"):
            mon = _month_num(m.group("mon"))
            y, d = int(m.group("y")), int(m.group("d"))
        elif m.group("dmy"):
            mon = _month_num(m.group("mon2"))
            y, d = int(m.group("y2")), int(m.group("d2"))
        elif m.group("iso"):
            mon, y, d = int(m.group("mi")), int(m.group("yi")), int(m.group("di"))
        else:
            mon, y, d = int(m.group("ms")), int(m.group("ys")), int(m.group("ds"))
        if mon is None:
            return None
        return _dt.date(y, mon, d).isoformat()
    except (TypeError, ValueError):
        return None


def parse_date_token(text: str | None) -> str | None:
    """First recognisable date in ``text`` as YYYY-MM-DD, else None."""
    for m in _DATE_TOKEN_RE.finditer(str(text or "")):
        iso = _iso_date(m)
        if iso:
            return iso
    return None


_EVENT_COVER_RE = re.compile(
    r"(?P<date>(?:[A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{4})|(?:\d{1,2}\s+[A-Za-z]{3,9}\.?,?\s+\d{4})"
    r"|(?:\d{4}-\d{2}-\d{2})|(?:\d{1,2}/\d{1,2}/\d{4}))"
    r"[\s)\]]{0,6}\(?\s*Date of Event\s+which\s+Requires\s+Filing\s+"
    r"(?:of\s+this\s+Statement|on\s+Schedule\s+13\s*[DG])",
    re.I,
)


def _cover_event_date(text: str) -> str | None:
    m = _EVENT_COVER_RE.search(text)
    if not m:
        return None
    return parse_date_token(m.group("date"))


_ITEM5_HEAD_RE = re.compile(r"Item\s*5\b", re.I)
_ITEM5C_BODY_RE = re.compile(
    r"\(\s*c\s*\)(?P<body>.*?)(?=\(\s*d\s*\)|Item\s*6\b|$)",
    re.I | re.S,
)


def _item5c_transaction_dates(text: str) -> list[str]:
    """Sorted unique YYYY-MM-DD dates mentioned in Schedule 13D Item 5(c).

    Item 5(a)/(b) can run well past a few hundred characters, so scan each
    "Item 5" heading's next 8000 characters for the "(c)" sub-item and take the
    first that mentions any date.
    """
    for head in _ITEM5_HEAD_RE.finditer(text):
        window = text[head.end(): head.end() + 8000]
        m = _ITEM5C_BODY_RE.search(window)
        if not m:
            continue
        body = m.group("body")[:4000]
        found = {iso for iso in (_iso_date(x) for x in _DATE_TOKEN_RE.finditer(body)) if iso}
        if found:
            return sorted(found)
    return []


def event_date_for(parsed: dict, filing_date: str) -> str:
    """Cover event date, else latest Item 5(c) transaction date, else ``filing_date``.

    A candidate after ``filing_date`` is parser garbage and is ignored.
    """
    fd = str(filing_date or "")[:10]
    candidates: list[str] = []
    cover = parse_date_token(parsed.get("event_date"))
    if cover:
        candidates.append(cover)
    tx = [d for d in (parsed.get("transaction_dates") or []) if d]
    if tx:
        candidates.append(max(tx))
    for c in candidates:
        if not fd or c <= fd:
            return c
        _log.warning("sec_13g: event_date %s after filing_date %s; using filing_date", c, fd)
    return fd


def forms_ok() -> set[str]:
    return {
        "SC 13G",
        "SC 13G/A",
        "SC 13D",
        "SC 13D/A",
        "SCHEDULE 13G",
        "SCHEDULE 13G/A",
        "SCHEDULE 13D",
        "SCHEDULE 13D/A",
    }


def _apply_exit_flags(out: dict, text: str = "") -> None:
    """Stamp ``exit`` only when quantity confirms — never Item 5 form boilerplate alone."""
    pct = out.get("ownership_pct")
    shares = out.get("shares")
    positive_stake = (shares is not None and float(shares) > 0) and (
        pct is not None and float(pct) > 0
    )
    zero_qty = (pct is not None and float(pct) <= 0) or (
        shares is not None and float(shares) <= 0
    )
    disposal_language = bool(
        text
        and re.search(
            r"no longer owns?\s+any|are no longer beneficial owners?",
            text,
            re.I,
        )
    )
    item5_checked = bool(
        text
        and re.search(
            r"ceased to be the beneficial owner of more than five percent"
            r"[^☒\[\]xX]{0,120}(?:☒|\[[\sxX]\])",
            text,
            re.I | re.S,
        )
    )
    if positive_stake:
        out.pop("exit", None)
        return
    if zero_qty or disposal_language or item5_checked:
        out["exit"] = True


def parse_13g_xml(xml_text: str) -> dict:
    root = ET.fromstring(xml_text)
    out: dict = {}
    for el in root.iter():
        name = _local(el.tag)
        text = (el.text or "").strip()
        if not text:
            continue
        key = name.lower()
        if key in {"issuername", "nameofissuer"} and "issuer_name" not in out:
            out["issuer_name"] = text
        elif key in {"issuercusipnumber", "cusip"} and "cusip" not in out:
            out["cusip"] = text
        elif key in {"classpercent", "percentofclass"} and "ownership_pct" not in out:
            try:
                out["ownership_pct"] = float(re.sub(r"[^0-9.]", "", text.split()[0]))
            except (TypeError, ValueError, IndexError):
                pass
        elif key in {"issuertradingsymbol", "tradingsymbol"} and "ticker" not in out:
            out["ticker"] = text.upper()
        elif key in {"sharesbeneficiallyowned", "aggregateamountowned"} and "shares" not in out:
            try:
                out["shares"] = float(re.sub(r"[^0-9.]", "", text))
            except (TypeError, ValueError):
                pass
        elif ("eventdate" in key or key == "dateofevent") and "event_date" not in out:
            iso = parse_date_token(text)
            if iso:
                out["event_date"] = iso
    _apply_exit_flags(out)
    return out


def _strip_html(html: str) -> str:
    text = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", html)
    text = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&#\d+;", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def parse_13g_html(html_text: str) -> dict:
    """Cover-page style extraction from HTML Schedule 13D/G."""
    text = _strip_html(html_text)
    out: dict = {}

    m = re.search(
        r"(?P<name>[A-Za-z0-9][A-Za-z0-9 .,&'\-]{1,120}?)\s*\(\s*Name of Issuer\s*\)",
        text,
        re.I,
    )
    if m:
        out["issuer_name"] = m.group("name").strip()

    m = re.search(r"\b([0-9A-Z]{9})\s*\(\s*CUSIP", text, re.I)
    if m:
        out["cusip"] = m.group(1).upper()

    # Optional colon: real SC 13G/A exits often print "Row (9) 0%" with no ":".
    m = re.search(
        r"Percent of Class Represented by Amount in Row\s*\(\s*\d+\s*\)\s*:?\s*"
        r"(?P<pct>\d+(?:\.\d+)?)\s*%",
        text,
        re.I,
    )
    if not m:
        m = re.search(
            r"Percent of Class Represented[^0-9%]{0,80}:?\s*(?P<pct>\d+(?:\.\d+)?)\s*%",
            text,
            re.I,
        )
    if m:
        out["ownership_pct"] = float(m.group("pct"))

    # Allow Aggregate Amount 0 (exit). Take the first numeric token after the label
    # so "...Person 0 10 Check Box" yields 0, not row number 10.
    m = re.search(
        r"Aggregate Amount Beneficially Owned by Each Reporting Person"
        r"[^0-9]{0,40}(?P<sh>\d[\d,]*)",
        text,
        re.I,
    )
    if not m:
        m = re.search(
            r"Sole Voting Power[^0-9]{0,40}(?P<sh>\d[\d,]{3,})",
            text,
            re.I,
        )
    if m:
        try:
            out["shares"] = float(m.group("sh").replace(",", ""))
        except ValueError:
            pass

    m = re.search(r"Trading Symbol[^A-Z]{0,20}(?P<sym>[A-Z]{1,6})\b", text)
    if m:
        out["ticker"] = m.group("sym")

    cover = _cover_event_date(text)
    if cover:
        out["event_date"] = cover
    tx = _item5c_transaction_dates(text)
    if tx:
        out["transaction_dates"] = tx

    _apply_exit_flags(out, text)
    return out


def is_self_issuer(
    parsed: dict,
    *,
    parent_ticker: str,
    parent_name_hints: list[str] | None = None,
) -> bool:
    """True when the filing's issuer is the parent (third-party 13G on the parent)."""
    parent = (parent_ticker or "").strip().upper()
    ticker = (parsed.get("ticker") or "").strip().upper()
    if parent and ticker and ticker == parent:
        return True
    name = (parsed.get("issuer_name") or "").strip().lower()
    if not name:
        return False
    hints = [h.lower() for h in (parent_name_hints or []) if h]
    # Default: ticker as word + common expansions
    hints.extend([parent.lower(), parent.replace("-", " ").lower()])
    if parent == "UBER":
        hints.append("uber technologies")
    if parent in {"BRK-B", "BRK.B", "BRKB"}:
        hints.append("berkshire hathaway")
    if parent == "BABA":
        hints.append("alibaba")
    for h in hints:
        if h and h in name:
            return True
    return False


def raw_to_live_row(
    parsed: dict,
    *,
    parent_ticker: str,
    form: str,
    acc: str,
    filing_date: str,
    cik: str,
) -> dict | None:
    name = parsed.get("issuer_name")
    if not name:
        return None
    name = clean_issuer_name(name) or name
    if is_self_issuer(parsed, parent_ticker=parent_ticker):
        return None
    cusip = (parsed.get("cusip") or "").strip().upper() or None
    ticker = resolve_issuer_ticker(name, parsed.get("ticker"), cusip=cusip)
    pct = parsed.get("ownership_pct")
    shares = parsed.get("shares")
    # 0% is a 13G exit (Item 5 / Aggregate Amount 0); anything else must be
    # a real stake in (0, 100]. Skip the row, never emit >100.
    if pct is not None and float(pct) != 0.0:
        try:
            pct = assert_pct_domain(pct, field="ownership_pct", context=f"13g {form} {acc} {name}")
        except ValueError as e:
            _log.warning("sec_13g: skipping row (%s): %s", e, parsed)
            return None
    event_date = event_date_for(parsed, filing_date)
    note = f"source=13g form={form} cik={cik} event_date={event_date}"
    if ticker and str(ticker).startswith("PRIV_") and "ticker=private_note" not in note:
        note = f"{note}; ticker=private_note"
    return {
        "parent_ticker": parent_ticker,
        "investee_name": name,
        "investee_ticker": ticker,
        "ownership_pct": pct,
        # Never stuff ownership_% into shares_held — that shipped Neutron at 22.87 "shares".
        "shares_held": float(shares) if shares is not None else None,
        "carrying_usd": None,
        "market_value_usd": None,
        "as_of_date": filing_date,
        "event_date": event_date,
        "as_of_accession_no": acc,
        "first_filing_date": filing_date,
        "first_accession_no": acc,
        "source_quote": f"{form} {acc}: {name} {pct}%",
        "confidence": "medium",
        "note": note,
        "filing_gaap_hint": "fv_ni" if (pct or 0) < 20 else None,
        "influence_disclosed": bool((pct or 0) >= 20),
        "_source": "13g",
        "_cusip": cusip,
        "cusip": cusip,
    }


def raw_to_position(
    parsed: dict,
    *,
    parent_ticker: str,
    form: str,
    acc: str,
    filing_date: str,
    cik: str,
) -> dict | None:
    """QoQ snapshot row shape.

    Never invent ``shares_proxy=presence``. Shares are: parsed count, ownership_%
    QoQ proxy, 0 on exit, or the row is dropped (identity-only garbage).
    """
    live = raw_to_live_row(
        parsed,
        parent_ticker=parent_ticker,
        form=form,
        acc=acc,
        filing_date=filing_date,
        cik=cik,
    )
    if not live:
        return None
    pct = live.get("ownership_pct")
    shares = live.get("shares_held")
    note = str(live.get("note") or "")
    # Defense in depth: positive Aggregate Amount + % never becomes an exit
    # even if HTML heuristics misfire (Item 5 boilerplate class).
    positive_stake = (shares is not None and float(shares) > 0) and (
        pct is not None and float(pct) > 0
    )
    is_exit = (not positive_stake) and (
        bool(parsed.get("exit"))
        or (pct is not None and float(pct) <= 0)
        or (shares is not None and float(shares) <= 0)
    )

    # Never write ownership_% into shares_held (Neutron / EM / ANT class).
    # QoQ continuity uses ownership_pct via diff_snapshots._continuity_qty.
    if shares is None and pct is not None and not is_exit:
        if "qoq_continuity=ownership_pct" not in note:
            note = f"{note}; qoq_continuity=ownership_pct".strip("; ")
    elif shares is None and is_exit:
        shares = 0.0
        pct = 0.0 if pct is None else pct
        if "13g_exit=1" not in note:
            note = f"{note}; 13g_exit=1".strip("; ")
    elif shares is None:
        # No quantitative signal and not an exit — do not invent presence=1.
        return None

    if is_exit:
        shares = 0.0
        if pct is None:
            pct = 0.0
        if "13g_exit=1" not in note:
            note = f"{note}; 13g_exit=1".strip("; ")

    return {
        "investee_name": live["investee_name"],
        "investee_ticker": live.get("investee_ticker"),
        "shares_held": float(shares) if shares is not None else None,
        "market_value_usd": None,
        "_cusip": live.get("_cusip"),
        "cusip": live.get("cusip"),
        "ownership_pct": pct,
        "note": note,
        "_source": "13g",
        "as_of_date": filing_date,
        "event_date": live.get("event_date") or filing_date,
        "as_of_accession_no": acc,
    }


def fetch_filing_text(
    session: requests.Session, cik: str, acc: str, primary: str
) -> tuple[str | None, str]:
    """Return (text, kind) where kind is xml|html|none."""
    nodash = acc.replace("-", "")
    primary = primary or "primary_doc.xml"
    candidates: list[str] = []
    low = primary.lower()
    if low.endswith((".htm", ".html")):
        candidates.append(primary.split("/")[-1] if "/" in primary else primary)
    elif "primary_doc.xml" in low or low.endswith(".xml"):
        candidates.append("primary_doc.xml")
        leaf = primary.split("/")[-1]
        if leaf not in candidates:
            candidates.append(leaf)
    else:
        candidates.extend([primary.split("/")[-1], "primary_doc.xml"])

    base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{nodash}"
    for doc in candidates:
        try:
            resp = session.get(f"{base}/{doc}", timeout=20)
            if resp.status_code != 200 or not resp.text:
                continue
            body = resp.text
            if doc.lower().endswith((".htm", ".html")) or "<html" in body[:500].lower():
                return body, "html"
            if body.lstrip().startswith("<") or "<?xml" in body[:80]:
                return body, "xml"
            return body, "html"
        except Exception:
            continue
    return None, "none"


def parse_filing_body(body: str, kind: str) -> dict:
    if kind == "xml":
        return parse_13g_xml(body)
    return parse_13g_html(body)


def _list_13g_items(session: requests.Session, cik: str, max_filings: int) -> list[tuple]:
    padded = str(cik).zfill(10)
    sub = session.get(f"https://data.sec.gov/submissions/CIK{padded}.json", timeout=30)
    sub.raise_for_status()
    recent = sub.json()["filings"]["recent"]
    ok = forms_ok()
    items: list[tuple] = []
    for i, form in enumerate(recent["form"]):
        if form not in ok:
            continue
        items.append(
            (
                recent["filingDate"][i],
                form,
                recent["accessionNumber"][i],
                recent["primaryDocument"][i],
            )
        )
        if len(items) >= max_filings:
            break
    return items


def fetch_latest_13g_holdings(
    *,
    cik: str,
    parent_ticker: str,
    user_agent: str,
    max_filings: int = 40,
) -> tuple[list[dict], dict[str, Any]]:
    """Latest Schedule 13D/G subjects for filer CIK (issuer ≠ parent)."""
    meta: dict[str, Any] = {
        "cik": cik,
        "num_filings_scanned": 0,
        "num_parsed": 0,
        "error": None,
    }
    session = requests.Session()
    session.headers.update({"User-Agent": user_agent or "hidden_stock research"})
    try:
        items = _list_13g_items(session, cik, max_filings)
    except Exception as e:
        meta["error"] = str(e)
        return [], meta

    meta["num_filings_scanned"] = len(items)
    by_issuer: dict[str, dict] = {}
    # Newest-first scan: once an issuer exits, ignore older filings that would re-add it.
    exited_issuers: set[str] = set()
    for filing_date, form, acc, primary in items:
        time.sleep(0.08)
        body, kind = fetch_filing_text(session, cik, acc, primary)
        if not body:
            continue
        try:
            parsed = parse_filing_body(body, kind)
        except Exception:
            continue
        meta["num_parsed"] += 1
        row = raw_to_live_row(
            parsed,
            parent_ticker=parent_ticker,
            form=form,
            acc=acc,
            filing_date=filing_date,
            cik=cik,
        )
        if not row:
            continue
        key = (row.get("investee_name") or "").strip().lower()
        ticker = (row.get("investee_ticker") or "").strip().upper()
        exit_keys = {k for k in (key, ticker.lower() if ticker else "") if k}
        # Cessation amendments: drop issuer and block older re-adds.
        if parsed.get("exit") or (
            row.get("ownership_pct") is not None and float(row["ownership_pct"]) <= 0
        ) or (row.get("shares_held") is not None and float(row["shares_held"]) <= 0):
            exited_issuers |= exit_keys
            by_issuer.pop(key, None)
            if ticker:
                for k, v in list(by_issuer.items()):
                    if (v.get("investee_ticker") or "").strip().upper() == ticker:
                        by_issuer.pop(k, None)
            continue
        if key in exited_issuers or (ticker and ticker.lower() in exited_issuers):
            continue
        prev = by_issuer.get(key)
        if prev is None or str(row["as_of_date"]) >= str(prev["as_of_date"]):
            by_issuer[key] = row
    return list(by_issuer.values()), meta


def collect_13g_period_snapshots(
    *,
    cik: str,
    parent_ticker: str,
    user_agent: str,
    max_filings: int = 80,
    lookback_start: str | None = None,
) -> tuple[list[tuple[str, str, str, list[dict]]], dict[str, Any]]:
    """Oldest→newest running issuer map from Schedule 13D/G amendments.

    Each tuple is ``(event_date, filing_date, accession, positions)``. Filings
    are applied in ``(event_date, filing_date)`` order, where ``event_date`` is
    the cover "Date of Event Which Requires Filing" (fallback: filing_date), so
    a 13D filed inside the 45-day window after a quarter end sits in the
    quarter its event belongs to. ``meta["exited_by_date"]`` is keyed the same
    way.
    """
    _key = holding_key
    from .lookback import date_on_or_after

    meta: dict[str, Any] = {
        "num_filings": 0,
        "num_periods": 0,
        "error": None,
        "cik": cik,
        "parent_ticker": parent_ticker,
        "lookback_start": lookback_start,
    }
    session = requests.Session()
    session.headers.update({"User-Agent": user_agent or "hidden_stock research"})
    try:
        items = _list_13g_items(session, cik, max_filings)
    except Exception as e:
        meta["error"] = str(e)
        return [], meta

    if lookback_start:
        items = [it for it in items if date_on_or_after(it[0], lookback_start)]

    items = list(reversed(items))
    meta["num_filings"] = len(items)

    parsed_filings: list[tuple[str, str, str, dict]] = []
    for filing_date, form, acc, primary in items:
        time.sleep(0.08)
        body, kind = fetch_filing_text(session, cik, acc, primary)
        if not body:
            continue
        try:
            parsed = parse_filing_body(body, kind)
        except Exception:
            continue
        pos = raw_to_position(
            parsed,
            parent_ticker=parent_ticker,
            form=form,
            acc=acc,
            filing_date=filing_date,
            cik=cik,
        )
        if not pos:
            continue
        parsed_filings.append((str(pos.get("event_date") or filing_date)[:10], filing_date, acc, pos))

    parsed_filings.sort(key=lambda t: (t[0], t[1]))

    running: dict[str, dict] = {}
    exited_tickers: set[str] = set()
    exited_by_date: dict[str, list[str]] = {}
    exit_events: dict[str, dict[str, str]] = {}
    by_period: dict[str, tuple[str, str, str, list[dict]]] = {}

    for event_date, filing_date, acc, pos in parsed_filings:
        pct = pos.get("ownership_pct")
        shares = pos.get("shares_held")
        note = str(pos.get("note") or "")
        k = _key(pos)
        ticker = (pos.get("investee_ticker") or "").strip().upper()
        is_exit = (
            "13g_exit=1" in note
            or (pct is not None and float(pct) <= 0)
            or (shares is not None and float(shares) <= 0)
        )
        # Positive stake can never be an exit (defense vs stale notes).
        if (
            shares is not None
            and float(shares) > 0
            and pct is not None
            and float(pct) > 0
        ):
            is_exit = False
        if is_exit:
            running.pop(k, None)
            if ticker:
                exited_tickers.add(ticker)
                exit_events[ticker] = {
                    "accession": acc,
                    "filing_date": filing_date,
                    "event_date": event_date,
                }
        else:
            running[k] = pos
            if ticker:
                exited_tickers.discard(ticker)
        exited_by_date[event_date] = sorted(exited_tickers)
        by_period[event_date] = (
            event_date,
            filing_date,
            acc,
            [dict(v) for v in running.values()],
        )

    ordered = [by_period[k] for k in sorted(by_period.keys())]
    meta["num_periods"] = len(ordered)
    meta["exited_by_date"] = exited_by_date
    meta["exit_events"] = exit_events
    return ordered, meta


def exited_tickers_as_of(
    exited_by_date: dict[str, list[str]] | None,
    as_of: str,
) -> set[str]:
    """Cumulative 13G/D exit tickers whose event_date <= as_of.

    ``exited_by_date`` comes from ``collect_13g_period_snapshots`` and is keyed
    by event_date; pass a period_end to get the exits effective in that period.
    """
    if not exited_by_date:
        return set()
    best: set[str] = set()
    as_of_s = str(as_of or "")[:10]
    for d in sorted(exited_by_date.keys()):
        if str(d)[:10] <= as_of_s:
            best = {t.upper() for t in exited_by_date[d] if t}
    return best


def positions_as_of(
    note_snaps: list[tuple[str, str, str, list[dict]]],
    as_of: str,
    *,
    by: str = "event",
) -> list[dict]:
    """Latest running snapshot dated <= ``as_of``.

    ``by="event"`` (default) compares the tuple's event_date (slot 0) — pass a
    period_end so a 13D filed after the quarter closed but reporting an event
    inside it lands in that quarter. ``by="filing"`` compares filing_date
    (slot 1) for point-of-disclosure views.
    """
    if by not in {"event", "filing"}:
        raise ValueError(f"positions_as_of: by must be 'event' or 'filing', got {by!r}")
    slot = 0 if by == "event" else 1
    as_of_s = str(as_of or "")[:10]
    eligible = [s for s in note_snaps if str(s[slot] or "")[:10] <= as_of_s]
    if not eligible:
        return []
    return list(eligible[-1][3])
