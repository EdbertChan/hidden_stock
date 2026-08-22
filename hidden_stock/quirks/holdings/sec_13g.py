"""Schedule 13D/G holdings for any filer CIK (XML + HTML)."""

from __future__ import annotations

import re
import time
from typing import Any
from xml.etree import ElementTree as ET

import requests

# Shared issuer name → ticker hints (Tencent + common US strategic stakes).
ISSUER_TICKER_HINTS: dict[str, str | None] = {
    "kanzhun": "BZ",
    "ke holdings": "BEKE",
    "reddit": "RDDT",
    "cango": "CANG",
    "horizon quantum": None,
    "pinduoduo": "PDD",
    "pdd holdings": "PDD",
    "sea limited": "SE",
    "spotify": "SPOT",
    "meituan": "3690.HK",
    "vipshop": "VIPS",
    "nio": "NIO",
    "bilibili": "BILI",
    "tencent music": "TME",
    "huya": "HUYA",
    "cheetah": "CMCM",
    "global blue": "GB",
    "cheche": "CCG",
    "didi": "DIDIY",
    "didi global": "DIDIY",
    "aurora innovation": "AUR",
    "grab holdings": "GRAB",
    "lucid group": "LCID",
    "weride": "WRD",
    "serve robotics": "SERV",
    "neutron holdings": None,  # Lime — private
    "xpeng": "XPEV",
    "perfect corp": "PERF",
    "baozun": "BZUN",
    "momo": "MOMO",
    "hello group": "MOMO",
    "groupon": "GRPN",
    "smart share": "EM",
    "mariadb": "MRDB",
    "best inc": "BEST",
    "joby aviation": "JOBY",
    "marqeta": "MQ",
    "rivian": "RIVN",
}

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


def _local(tag: str) -> str:
    return tag.split("}")[-1]


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


def resolve_issuer_ticker(name: str | None, ticker: str | None) -> str | None:
    if ticker:
        return str(ticker).strip().upper()
    if not name:
        return None
    from .extract import load_investee_aliases, resolve_investee_ticker

    resolved = resolve_investee_ticker(name, None, load_investee_aliases())
    if resolved:
        return resolved
    key = name.strip().lower()
    for alias, sym in ISSUER_TICKER_HINTS.items():
        if alias in key:
            return sym
    return None


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
        r"[^0-9]{0,40}(?P<sh>[\d,]+)",
        text,
        re.I,
    )
    if not m:
        m = re.search(
            r"Sole Voting Power[^0-9]{0,40}(?P<sh>[\d,]{4,})",
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

    # Cessation / exit: 0%/0 shares, Item 5 language, or Item 4 "no longer owns".
    pct = out.get("ownership_pct")
    shares = out.get("shares")
    cessation = bool(
        re.search(
            r"ceased to be the beneficial owner of more than five percent",
            text,
            re.I,
        )
        or re.search(
            r"no longer owns?\s+any|no longer beneficial owners?",
            text,
            re.I,
        )
    )
    if cessation or (pct is not None and float(pct) <= 0) or (shares is not None and float(shares) <= 0):
        out["exit"] = True

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
    if is_self_issuer(parsed, parent_ticker=parent_ticker):
        return None
    ticker = resolve_issuer_ticker(name, parsed.get("ticker"))
    pct = parsed.get("ownership_pct")
    cusip = (parsed.get("cusip") or "").strip().upper() or None
    shares = parsed.get("shares")
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
        "as_of_accession_no": acc,
        "first_filing_date": filing_date,
        "first_accession_no": acc,
        "source_quote": f"{form} {acc}: {name} {pct}%",
        "confidence": "medium",
        "note": f"source=13g form={form} cik={cik}",
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
    is_exit = bool(parsed.get("exit")) or (
        pct is not None and float(pct) <= 0
    ) or (shares is not None and float(shares) <= 0)

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
    """Oldest→newest running issuer map from Schedule 13D/G amendments."""
    from .history import _key
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

    running: dict[str, dict] = {}
    exited_tickers: set[str] = set()
    exited_by_date: dict[str, list[str]] = {}
    by_period: dict[str, tuple[str, str, str, list[dict]]] = {}

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
        if is_exit:
            running.pop(k, None)
            if ticker:
                exited_tickers.add(ticker)
        else:
            running[k] = pos
            if ticker:
                exited_tickers.discard(ticker)
        exited_by_date[filing_date] = sorted(exited_tickers)
        by_period[filing_date] = (
            filing_date,
            filing_date,
            acc,
            [dict(v) for v in running.values()],
        )

    ordered = [by_period[k] for k in sorted(by_period.keys())]
    meta["num_periods"] = len(ordered)
    meta["exited_by_date"] = exited_by_date
    return ordered, meta


def exited_tickers_as_of(
    exited_by_date: dict[str, list[str]] | None,
    as_of: str,
) -> set[str]:
    """Cumulative 13G/D exit tickers with filing_date <= as_of."""
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
) -> list[dict]:
    """Latest running snapshot with filing_date <= as_of."""
    eligible = [s for s in note_snaps if s[1] <= as_of]
    if not eligible:
        return []
    return list(eligible[-1][3])
