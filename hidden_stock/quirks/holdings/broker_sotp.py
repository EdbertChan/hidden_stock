"""Curated sell-side PDF ingest for holdings (broker SOTP).

LlamaIndex (PDFReader) extracts page text; domain parsers turn known table
layouts into rows that ``apply_broker_overlay`` merges into live/history.
Provenance lives on ``note`` (``value_source=broker_sotp``) — no separate table.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Literal
from urllib.request import Request, urlopen

import yaml

# Internal parse shape (not a public DB/export schema).
BROKER_SOTP_COLUMNS = [
    "parent_ticker",
    "series_id",
    "investee_name",
    "investee_ticker",
    "investee_ticker_raw",
    "ownership_pct",
    "mkt_cap_usd_mn",
    "value_to_parent_hkd_mn",
    "as_of",
    "broker",
    "report_id",
    "source_url",
    "citation",
    "value_source",
    "parser",
    "note",
]

_CATALOG_PATH = Path(__file__).resolve().parent / "data" / "broker_sotp_catalog.yaml"
_FX_CACHE: dict[str, float] = {}
_FALLBACK_HKD_PER_USD = 7.8

_LISTED_AGG_TICKERS = frozenset(
    {
        "PRIV_HK_LISTED_INVESTEES_FV",
        "PRIV_HK_LISTED_ASSOCIATES",
    }
)

# CMBIGM Figure: Name | Ticker | stake% | Mkt cap US$mn | Value to Tencent HK$mn
# Numeric codes are 3–6 digits (HK 4-digit, CN A-share 6-digit, KS 6-digit).
_CMBIGM_ROW = re.compile(
    r"^(?P<name>.+?)\s+"
    r"(?P<ticker>(?:\d{3,6}|[A-Z][A-Z0-9.]{0,9})"
    r"(?:\s+(?:US|HK|KS|JP|CH|LN|SS|SZ))?)\s+"
    r"(?P<stake>\d+(?:\.\d+)?)\s+"
    r"(?P<mcap>[\d,]+)\s+"
    r"(?P<value>[\d,]+)\s*$"
)

_STOP_PREFIXES = (
    "listed investments",
    "unlisted investments",
    "total investment",
    "holdco discount",
    "valuation of strategic",
    "other listed entities",
    "source:",
    "note:",
    "figure ",
)

# Name-only fallbacks when ticker is HK/CN numeric (aliases.yaml style).
_NAME_TICKER: dict[str, str] = {
    "meituan": "3690.HK",
    "jd.com inc": "JD",
    "jd.com": "JD",
    "kuaishou technology": "1024.HK",
    "pdd holdings inc": "PDD",
    "sea ltd": "SE",
    "spotify technology sa": "SPOT",
    "futu holdings ltd": "FUTU",
    "ke holdings inc": "BEKE",
    "reddit inc": "RDDT",
    "snap inc": "SNAP",
    "bilibili inc": "BILI",
    "vipshop holdings ltd": "VIPS",
    "kanzhun ltd": "BZ",
    "warner music group corp": "WMG",
    "tongcheng travel holdings ltd": "0780.HK",
    "yixin group ltd": "2858.HK",
    "j&t global express ltd": "1519.HK",
    "xtalpi holdings ltd": "2228.HK",
    "tuhu car inc": "9690.HK",
    "krafton inc": "259960.KS",
    "netmarble corp": "251270.KS",
    "kadokawa corp": "9468.T",
    "zhejiang century huatong group": "002602.SZ",
}


def _citation_for(series: dict[str, Any], report: dict[str, Any]) -> str:
    if report.get("citation"):
        return str(report["citation"])
    tmpl = series.get("citation_template") or "{broker}; as_of {as_of}"
    return str(tmpl).format(
        broker=series.get("broker") or report.get("broker") or "broker",
        as_of=report.get("as_of") or "",
        published=report.get("published") or "",
        title=report.get("title") or series.get("title") or "",
    )


def flatten_series(raw: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Expand series→reports into flat entries (one PDF each).

    Finding one report implies loading *all* enabled vintages in that series so
    stake % can be confirmed across years — never a single-quarter orphan.
    """
    raw = raw or {}
    out: list[dict[str, Any]] = []
    for series in raw.get("series") or []:
        if not series.get("enabled", True):
            continue
        for report in series.get("reports") or []:
            if not report.get("enabled", True):
                continue
            entry = {
                "id": report.get("id"),
                "series_id": series.get("id"),
                "parent_ticker": series.get("parent_ticker"),
                "broker": series.get("broker"),
                "parser": series.get("parser") or report.get("parser"),
                "table_marker": series.get("table_marker") or report.get("table_marker"),
                "title": report.get("title") or series.get("title"),
                "as_of": report.get("as_of"),
                "published": report.get("published"),
                "url": report.get("url"),
                "citation": _citation_for(series, report),
                "enabled": True,
            }
            out.append(entry)
    # Legacy flat entries (single PDF, auto series_id = id).
    for e in raw.get("entries") or []:
        if not e.get("enabled", True):
            continue
        entry = dict(e)
        entry.setdefault("series_id", e.get("series_id") or e.get("id"))
        out.append(entry)
    return out


def load_catalog(path: Path | None = None) -> list[dict[str, Any]]:
    p = path or _CATALOG_PATH
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return flatten_series(raw)


def catalog_for_parent(parent: str, path: Path | None = None) -> list[dict[str, Any]]:
    want = str(parent or "").strip().upper()
    return [e for e in load_catalog(path) if str(e.get("parent_ticker") or "").upper() == want]


def series_mates(entry: dict[str, Any], path: Path | None = None) -> list[dict[str, Any]]:
    """All enabled reports in the same broker×parent series (backfill set)."""
    sid = str(entry.get("series_id") or entry.get("id") or "")
    if not sid:
        return [entry]
    mates = [e for e in load_catalog(path) if str(e.get("series_id") or "") == sid]
    return mates or [entry]


def default_cache_dir() -> Path:
    import os

    base = Path(os.environ.get("BROKER_SOTP_CACHE_DIR", "exports/broker_pdfs"))
    base.mkdir(parents=True, exist_ok=True)
    return base


def download_pdf(url: str, cache_dir: Path | None = None, *, timeout: int = 60) -> Path:
    """Fetch curated PDF once; cache by URL hash."""
    cache = cache_dir or default_cache_dir()
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    out = cache / f"{digest}.pdf"
    if out.is_file() and out.stat().st_size > 1000:
        return out
    req = Request(url, headers={"User-Agent": "hidden_stock-broker-sotp/1.0"})
    with urlopen(req, timeout=timeout) as resp:  # noqa: S310 — curated catalog URLs only
        data = resp.read()
    if len(data) < 1000 or not data.startswith(b"%PDF"):
        raise RuntimeError(f"broker PDF download failed or not a PDF: {url[:80]}")
    out.write_bytes(data)
    return out


def extract_pdf_text(path: Path) -> str:
    """General PDF text via LlamaIndex PDFReader; pdfplumber fallback."""
    path = Path(path)
    try:
        from llama_index.readers.file import PDFReader

        docs = PDFReader().load_data(file=str(path))
        text = "\n\n".join(getattr(d, "text", "") or "" for d in docs)
        if text.strip():
            return text
    except Exception:
        pass
    import pdfplumber

    parts: list[str] = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            parts.append(page.extract_text() or "")
    return "\n\n".join(parts)


def _normalize_ticker(name: str, raw_ticker: str) -> str:
    raw = re.sub(r"\s+", " ", (raw_ticker or "").strip())
    parts = raw.split()
    # Bare exchange suffix is never a ticker (parser ate the numeric code into name).
    if raw.upper() in {"US", "HK", "KS", "JP", "CH", "LN", "SS", "SZ"}:
        key = re.sub(r"\s+", " ", (name or "").strip().lower())
        if key in _NAME_TICKER:
            return _NAME_TICKER[key]
        # Recover trailing "002602 CH" / "259960 KS" stuck on the name.
        m = re.search(
            r"(\d{3,6})\s+(US|HK|KS|JP|CH|LN|SS|SZ)\s*$",
            name or "",
            re.I,
        )
        if m:
            return _normalize_ticker("", f"{m.group(1)} {m.group(2).upper()}")
        return raw.upper()
    if len(parts) == 2 and parts[1] in {"US", "HK", "KS", "JP", "CH", "LN", "SS", "SZ"}:
        code, exch = parts[0], parts[1]
        if exch == "US":
            return code.upper()
        if exch == "HK":
            return f"{code.zfill(4)}.HK" if code.isdigit() else f"{code}.HK"
        if exch == "KS":
            return f"{code}.KS"
        if exch == "JP":
            return f"{code}.T"
        if exch in {"CH", "SZ", "SS"}:
            return f"{code}.SZ" if code.startswith("0") else f"{code}.SS"
        return f"{code}.{exch}"
    key = re.sub(r"\s+", " ", (name or "").strip().lower())
    if key in _NAME_TICKER:
        return _NAME_TICKER[key]
    # Strip trailing exchange token leaked into name before name lookup.
    name_clean = re.sub(
        r"\s+\d{3,6}\s+(?:US|HK|KS|JP|CH|LN|SS|SZ)\s*$",
        "",
        name or "",
        flags=re.I,
    ).strip()
    key2 = re.sub(r"\s+", " ", name_clean.lower())
    if key2 in _NAME_TICKER:
        return _NAME_TICKER[key2]
    return raw.replace(" ", ".")


# Collapsed single-line tables (common after PDF extract).
_CMBIGM_INLINE = re.compile(
    r"(?P<name>(?:[A-Z][\w&.'/-]+(?:\s+[A-Z][\w&.'/-]+){0,6}|Meituan|JD\.com Inc))"
    r"\s+(?P<ticker>(?:\d{3,6}|[A-Z][A-Z0-9.]{0,9})"
    r"(?:\s+(?:US|HK|KS|JP|CH|LN|SS|SZ))?)"
    r"\s+(?P<stake>\d+(?:\.\d+)?)"
    r"\s+(?P<mcap>[\d,]+)"
    r"\s+(?P<value>[\d,]+)"
)


def _row_from_match(m: re.Match[str]) -> dict[str, Any] | None:
    name = m.group("name").strip()
    if name.lower().startswith("name") or "stake" in name.lower():
        return None
    if any(name.lower().startswith(p) for p in _STOP_PREFIXES):
        return None
    raw_t = m.group("ticker").strip()
    # If ticker group is bare exchange, peel trailing code/ticker off the name.
    if raw_t.upper() in {"US", "HK", "KS", "JP", "CH", "LN", "SS", "SZ"}:
        m_code = re.search(
            r"^(?P<nm>.+?)\s+(?P<code>\d{3,6}|[A-Z][A-Z0-9.]{0,9})$",
            name,
            re.I,
        )
        if m_code:
            name = m_code.group("nm").strip()
            raw_t = f"{m_code.group('code')} {raw_t.upper()}"
    ticker = _normalize_ticker(name, raw_t)
    # Strip leaked code+exch from display name when recovered.
    name = re.sub(
        r"\s+(?:\d{3,6}|[A-Z][A-Z0-9.]{0,9})\s+(?:US|HK|KS|JP|CH|LN|SS|SZ)\s*$",
        "",
        name,
        flags=re.I,
    ).strip()
    return {
        "investee_name": name,
        "investee_ticker_raw": raw_t,
        "investee_ticker": ticker,
        "ownership_pct": float(m.group("stake")),
        "mkt_cap_usd_mn": float(m.group("mcap").replace(",", "")),
        "value_to_parent_hkd_mn": float(m.group("value").replace(",", "")),
    }


def parse_cmbigm_strategic_investments(text: str) -> list[dict[str, Any]]:
    """Parse CMBIGM 'valuation of strategic investments' named stake table."""
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in (text or "").splitlines()]
    lines = [ln for ln in lines if ln]

    start = -1
    for i, ln in enumerate(lines):
        low = ln.lower()
        if "strategic investment" in low and ("stake" in low or "mkt cap" in low or "ticker" in low):
            start = i
            break
        if "tencent's stake" in low and "mkt cap" in low:
            start = i
            break
    if start < 0:
        for i, ln in enumerate(lines):
            if _CMBIGM_ROW.match(ln) and "pdd" in ln.lower():
                start = max(0, i - 1)
                break

    rows: list[dict[str, Any]] = []
    if start >= 0:
        for ln in lines[start + 1 :]:
            low = ln.lower()
            if any(low.startswith(p) for p in _STOP_PREFIXES):
                if rows:
                    break
                continue
            m = _CMBIGM_ROW.match(ln)
            if not m:
                continue
            parsed = _row_from_match(m)
            if parsed:
                rows.append(parsed)

    if len(rows) >= 3:
        return rows

    # Fallback: scan whole document for inline stake×mcap tuples.
    blob = re.sub(r"\s+", " ", text or "")
    # Prefer window around the figure if present.
    low = blob.lower()
    idx = low.find("valuation of strategic investments")
    window = blob[idx : idx + 8000] if idx >= 0 else blob
    seen: set[str] = set()
    inline: list[dict[str, Any]] = []
    for m in _CMBIGM_INLINE.finditer(window):
        parsed = _row_from_match(m)
        if not parsed:
            continue
        key = parsed["investee_ticker"]
        if key in seen:
            continue
        seen.add(key)
        inline.append(parsed)
    return inline if len(inline) > len(rows) else rows


_PARSERS = {
    "cmbigm_strategic_investments": parse_cmbigm_strategic_investments,
}


def rows_from_entry(
    entry: dict[str, Any],
    *,
    text: str | None = None,
    pdf_path: Path | None = None,
    cache_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """Materialize broker SOTP rows for one catalog entry."""
    parser_name = str(entry.get("parser") or "")
    parse_fn = _PARSERS.get(parser_name)
    if parse_fn is None:
        raise ValueError(f"unknown broker SOTP parser: {parser_name}")

    body = text
    if body is None:
        path = pdf_path or download_pdf(str(entry["url"]), cache_dir=cache_dir)
        body = extract_pdf_text(path)

    parsed = parse_fn(body)
    out: list[dict[str, Any]] = []
    cite = str(entry.get("citation") or entry.get("broker") or "broker")
    series_id = str(entry.get("series_id") or entry.get("id") or "")
    for p in parsed:
        out.append(
            {
                "parent_ticker": str(entry.get("parent_ticker") or "").upper(),
                "series_id": series_id,
                "investee_name": p["investee_name"],
                "investee_ticker": p["investee_ticker"],
                "investee_ticker_raw": p["investee_ticker_raw"],
                "ownership_pct": p["ownership_pct"],
                "mkt_cap_usd_mn": p["mkt_cap_usd_mn"],
                "value_to_parent_hkd_mn": p["value_to_parent_hkd_mn"],
                "as_of": entry.get("as_of"),
                "broker": entry.get("broker"),
                "report_id": entry.get("id"),
                "source_url": entry.get("url"),
                "citation": cite,
                "value_source": "broker_sotp",
                "parser": parser_name,
                "note": (
                    f"value_source=broker_sotp; "
                    f"series_id={series_id}; broker={entry.get('broker')}; "
                    f"report_id={entry.get('id')}; cite={cite}"
                ),
            }
        )
    return out


def materialize_broker_sotp(
    parents: list[str] | None = None,
    *,
    catalog_path: Path | None = None,
    cache_dir: Path | None = None,
    expand_series: bool = True,
) -> list[dict[str, Any]]:
    """Run catalog entries; by default expand each hit to its full series backfill."""
    entries = load_catalog(catalog_path)
    if parents:
        want = {str(p).upper() for p in parents}
        entries = [e for e in entries if str(e.get("parent_ticker") or "").upper() in want]
    if expand_series:
        by_id: dict[str, dict[str, Any]] = {}
        for e in entries:
            for mate in series_mates(e, catalog_path):
                by_id[str(mate.get("id"))] = mate
        entries = sorted(
            by_id.values(),
            key=lambda e: (str(e.get("series_id") or ""), str(e.get("as_of") or "")),
        )
    all_rows: list[dict[str, Any]] = []
    errors: list[str] = []
    for e in entries:
        try:
            all_rows.extend(rows_from_entry(e, cache_dir=cache_dir))
        except Exception as exc:  # noqa: BLE001 — one bad PDF must not kill the series
            errors.append(f"{e.get('id')}: {exc}")
    if errors and not all_rows:
        raise RuntimeError("broker SOTP series failed: " + "; ".join(errors[:5]))
    return all_rows


def hkd_mn_to_usd(hkd_mn: float, as_of: str | None) -> tuple[float, float]:
    """Convert HK$ millions → USD. Returns (usd, hkd_per_usd_fx)."""
    as_key = str(as_of or "")[:10] or "latest"
    fx = _FX_CACHE.get(as_key)
    if fx is None:
        fx = _FALLBACK_HKD_PER_USD
        try:
            import yfinance as yf

            # USDHKD=X ≈ HKD per 1 USD
            t = yf.Ticker("USDHKD=X")
            if as_key and as_key != "latest":
                hist = t.history(start=as_key, end=None, period=None)
                if hist is None or hist.empty:
                    hist = t.history(period="5d")
                else:
                    # nearest on/before as_of: history starts at as_of
                    pass
                if hist is not None and not hist.empty:
                    fx = float(hist["Close"].iloc[0])
            else:
                hist = t.history(period="5d")
                if hist is not None and not hist.empty:
                    fx = float(hist["Close"].iloc[-1])
        except Exception:
            fx = _FALLBACK_HKD_PER_USD
        if not fx or fx <= 0:
            fx = _FALLBACK_HKD_PER_USD
        _FX_CACHE[as_key] = fx
    usd = float(hkd_mn) * 1_000_000.0 / float(fx)
    return usd, float(fx)


def _append_note(note: str | None, fragment: str) -> str:
    base = str(note or "").strip()
    if fragment in base:
        return base
    return f"{base}; {fragment}" if base else fragment


def _mv_null(row: dict) -> bool:
    v = row.get("market_value_usd")
    return v is None or v == ""


def _has_listed_aggregate(rows: list[dict], period: str, *, mode: str) -> bool:
    """True when Note 22 listed aggregates are present (broker $ must not co-sum)."""
    del period, mode
    return any(
        str(r.get("investee_ticker") or "").strip().upper() in _LISTED_AGG_TICKERS for r in rows
    )


def _broker_stamp(b: dict, *, fx: float, exclude: bool) -> str:
    parts = [
        "value_source=broker_sotp",
        f"report_id={b.get('report_id')}",
        f"cite={b.get('citation')}",
        f"as_of={b.get('as_of')}",
        f"fx_hkd_per_usd={fx:.4f}",
        "not_fv_allocation",
    ]
    if exclude:
        parts.append("excluded_from_portfolio_mv")
    return "; ".join(parts)


def apply_broker_overlay(
    rows: list[dict],
    broker_rows: list[dict] | None,
    *,
    mode: Literal["history", "live"] = "history",
    parent_ticker: str = "TCEHY",
) -> list[dict]:
    """Merge published broker stake % + value-to-parent into holdings rows.

    - 13G/D keeps ownership_pct / shares_held when present.
    - Broker fills market_value_usd only when filing $ is null.
    - Missing names are added (null shares).
    - When HK listed aggregates exist, broker $ stamped excluded_from_portfolio_mv.
    """
    if not broker_rows:
        return rows

    out = [dict(r) for r in rows]
    parent = str(parent_ticker or "TCEHY").upper()

    # Live: only latest broker vintage per ticker.
    usable = list(broker_rows)
    if mode == "live":
        latest = max((str(b.get("as_of") or "")[:10] for b in usable), default="")
        if latest:
            usable = [b for b in usable if str(b.get("as_of") or "")[:10] == latest]

    def _period_key(r: dict) -> str:
        if mode == "live":
            return str(r.get("as_of_date") or "")[:10]
        return str(r.get("period_end") or "")[:10]

    # Index existing named rows by (period, ticker).
    by_pt: dict[tuple[str, str], list[int]] = {}
    for i, r in enumerate(out):
        t = str(r.get("investee_ticker") or "").strip().upper()
        if not t or t.startswith("PRIV_"):
            continue
        by_pt.setdefault((_period_key(r), t), []).append(i)

    for b in usable:
        t = str(b.get("investee_ticker") or "").strip().upper()
        pe = str(b.get("as_of") or "")[:10]
        if not t or not pe:
            continue
        hkd = b.get("value_to_parent_hkd_mn")
        try:
            hkd_f = float(hkd) if hkd is not None else None
        except (TypeError, ValueError):
            hkd_f = None
        usd = None
        fx = _FALLBACK_HKD_PER_USD
        if hkd_f is not None:
            usd, fx = hkd_mn_to_usd(hkd_f, pe)
        exclude = _has_listed_aggregate(out, pe, mode=mode)
        stamp = _broker_stamp(b, fx=fx, exclude=exclude)

        idxs = by_pt.get((pe, t), [])
        if mode == "live" and not idxs:
            # Match any live row with same ticker (single snapshot).
            idxs = [
                i
                for i, r in enumerate(out)
                if str(r.get("investee_ticker") or "").strip().upper() == t
                and not str(t).startswith("PRIV_")
            ]

        if idxs:
            for i in idxs:
                r = out[i]
                # Prefer 13G %/shares — only fill ownership if missing.
                if r.get("ownership_pct") is None and b.get("ownership_pct") is not None:
                    r["ownership_pct"] = b.get("ownership_pct")
                if usd is not None and _mv_null(r):
                    r["market_value_usd"] = usd
                    r["note"] = _append_note(r.get("note"), stamp)
                elif "value_source=broker_sotp" not in str(r.get("note") or ""):
                    # Still record cite when we only confirm % continuity.
                    if b.get("ownership_pct") is not None and r.get("ownership_pct") is None:
                        r["note"] = _append_note(r.get("note"), stamp)
            continue

        # New name from broker.
        if mode == "history":
            new = {
                "parent_ticker": parent,
                "period_end": pe,
                "filing_date": pe,
                "accession_no": None,
                "filing_url": b.get("source_url"),
                "investee_name": b.get("investee_name"),
                "investee_ticker": t,
                "cusip": None,
                "shares_held": None,
                "ownership_pct": b.get("ownership_pct"),
                "market_value_usd": usd,
                "shares_prev": None,
                "shares_delta": None,
                "value_prev": None,
                "value_delta": None,
                "action": "hold",
                "first_seen_period": pe,
                "exited_period": None,
                "note": stamp,
            }
        else:
            from .schema import empty_holding_row

            new = empty_holding_row(
                parent_ticker=parent,
                as_of_date=pe,
                investee_name=b.get("investee_name"),
                investee_ticker=t,
                ownership_pct=b.get("ownership_pct"),
                market_value_usd=usd,
                note=stamp,
                price_source="broker_sotp",
            )
        out.append(new)
        by_pt.setdefault((pe, t), []).append(len(out) - 1)

    return out


def stake_history_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Long stake continuity helper (tests / debug only)."""
    out: list[dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "parent_ticker": r.get("parent_ticker"),
                "series_id": r.get("series_id"),
                "broker": r.get("broker"),
                "investee_ticker": r.get("investee_ticker"),
                "investee_name": r.get("investee_name"),
                "as_of": r.get("as_of"),
                "ownership_pct": r.get("ownership_pct"),
                "value_to_parent_hkd_mn": r.get("value_to_parent_hkd_mn"),
                "report_id": r.get("report_id"),
                "source_url": r.get("source_url"),
                "citation": r.get("citation"),
            }
        )
    out.sort(
        key=lambda x: (
            str(x.get("series_id") or ""),
            str(x.get("investee_ticker") or ""),
            str(x.get("as_of") or ""),
        )
    )
    return out


def stake_history_wide(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Wide stake % by as_of (tests / debug only)."""
    long = stake_history_rows(rows)
    if not long:
        return []
    as_ofs = sorted({str(r["as_of"]) for r in long if r.get("as_of")})
    keys: dict[tuple[str, str, str], dict[str, Any]] = {}
    for r in long:
        key = (
            str(r.get("series_id") or ""),
            str(r.get("investee_ticker") or ""),
            str(r.get("broker") or ""),
        )
        slot = keys.setdefault(
            key,
            {
                "series_id": r.get("series_id"),
                "broker": r.get("broker"),
                "parent_ticker": r.get("parent_ticker"),
                "investee_ticker": r.get("investee_ticker"),
                "investee_name": r.get("investee_name"),
                "n_vintages": 0,
            },
        )
        as_of = str(r.get("as_of") or "")
        if as_of:
            slot[f"stake_pct_{as_of}"] = r.get("ownership_pct")
    for slot in keys.values():
        slot["n_vintages"] = sum(1 for a in as_ofs if slot.get(f"stake_pct_{a}") is not None)
    return sorted(
        keys.values(),
        key=lambda x: (-int(x.get("n_vintages") or 0), str(x.get("investee_ticker") or "")),
    )
