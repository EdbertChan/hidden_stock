"""Independent reconciliation of exported holdings rows against EDGAR filings.

The history export cites an ``accession_no`` per row. This module re-fetches
that filing's primary document (or the 13F information table) straight from
EDGAR and asserts that the literal value string the sheet shows is present in
the document text. It deliberately does **not** re-run any parser: the proof
is "the number the sheet prints appears in the filing it cites", nothing more.

It also checks an optional hand-curated per-parent ``<parent>_anchors.yaml``
(gitignored; see ``data/anchors.example.yaml``): fixed external truths such as
"UBER DIDIY 2026-06-30 = $1,900M" that the sheet must match.

Values are never modified.
"""

from __future__ import annotations

import csv
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

_DATA_DIR = Path(__file__).resolve().parent / "data"
_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CACHE_DIR = _REPO_ROOT / ".cache" / "reconcile"

STATUS_ANCHORED = "anchored"
STATUS_NOT_FOUND = "not_found"
STATUS_FETCH_ERROR = "fetch_error"
STATUS_NO_CITATION = "no_citation"
STATUS_SKIPPED_NULL = "skipped_null"
STATUS_ANCHOR_MATCH = "anchor_match"
STATUS_ANCHOR_MISMATCH = "anchor_mismatch"
STATUS_ANCHOR_MISSING_ROW = "anchor_missing_row"

FAILING_STATUSES = frozenset(
    {STATUS_NOT_FOUND, STATUS_ANCHOR_MISMATCH, STATUS_ANCHOR_MISSING_ROW}
)

RESULT_COLUMNS = [
    "kind",
    "period_end",
    "investee_ticker",
    "status",
    "field",
    "sheet_value",
    "searched",
    "matched",
    "accession_no",
    "cik",
    "document",
    "source_kind",
    "detail",
]

_NULLS = {"", "nan", "none", "null", "nat"}


def _s(v: Any) -> str:
    if v is None:
        return ""
    s = str(v).strip()
    return "" if s.lower() in _NULLS else s


def _f(v: Any) -> float | None:
    s = _s(v)
    if not s:
        return None
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def parse_note(note: Any) -> dict:
    """Pull ``key=value`` tokens and every ``source=`` kind out of a note string.

    ``"source=13g form=SC 13D cik=0001543151; source=10q_investments_table fv_usd=1900000000"``
    -> ``{"sources": ["13g", "10q_investments_table"], "form": "SC 13D", "cik": ..., "fv_usd": ...}``
    """
    text = _s(note)
    out: dict = {"sources": []}
    if not text:
        return out
    for segment in re.split(r"\s*;\s*", text):
        tokens = re.findall(r"(\w+)=([^\s;]+(?:\s+[A-Z0-9/][A-Z0-9/.\-]*)*)", segment)
        for key, val in tokens:
            val = val.strip()
            if key == "source":
                out["sources"].append(val)
            elif key not in out:
                out[key] = val
    return out


def cik_from_filing_url(url: Any) -> str | None:
    m = re.search(r"/edgar/data/(\d+)/", _s(url))
    return m.group(1) if m else None


def _boundary(num_str: str) -> str:
    """Regex that matches ``num_str`` as a whole number (no digit run-on either side)."""
    core = re.escape(num_str)
    return rf"(?<![\d.])(?<!\d,){core}(?:\.0+)?(?!\d)(?!,\d)(?!\.\d)"


def format_musd(fv_usd: float) -> str:
    m = fv_usd / 1_000_000.0
    if abs(m - round(m)) < 1e-9:
        return f"{int(round(m)):,}"
    return f"{m:,.1f}"


def format_shares(shares: float) -> str:
    return f"{int(round(shares)):,}"


def format_pct(pct: float) -> str:
    if abs(pct - round(pct)) < 1e-9:
        return f"{int(round(pct))}"
    return f"{pct:g}"


@dataclass
class SearchTarget:
    field: str
    sheet_value: str
    display: str
    patterns: list[str] = field(default_factory=list)


def _target_musd(fv_usd: float, *, field_name: str = "market_value_usd") -> SearchTarget:
    """Match the $M figure as filings print it; also accept thousands / whole-dollar forms."""
    musd = format_musd(fv_usd)
    pats = [_boundary(musd)]
    if abs(fv_usd - round(fv_usd)) < 1e-6 and int(fv_usd) % 1000 == 0:
        pats.append(_boundary(f"{int(fv_usd) // 1000:,}"))
        pats.append(_boundary(f"{int(fv_usd):,}"))
    return SearchTarget(field_name, _s(fv_usd), f"{musd} ($M)", pats)


def _target_shares(shares: float) -> SearchTarget:
    with_commas = format_shares(shares)
    plain = str(int(round(shares)))
    return SearchTarget(
        "shares_held", _s(shares), with_commas, [_boundary(with_commas), _boundary(plain)]
    )


def _target_pct(pct: float) -> SearchTarget:
    p = format_pct(pct)
    variants = {p}
    if "." not in p:
        variants.add(f"{p}.0")
    pats = [rf"(?<![\d.]){re.escape(v)}\s?%" for v in sorted(variants)]
    pats.append(_boundary(p))
    return SearchTarget("ownership_pct", _s(pct), f"{p}%", pats)


def _target_cusip(cusip: str) -> SearchTarget:
    return SearchTarget(
        "cusip", cusip, cusip, [rf"(?<![A-Za-z0-9]){re.escape(cusip)}(?![A-Za-z0-9])"]
    )


@dataclass
class RowPlan:
    source_kind: str
    doc_kind: str
    targets: list[SearchTarget]
    require_all: bool


def plan_row(row: dict) -> RowPlan | None:
    """Decide what to search for in the cited document; None means nothing checkable.

    source_kind: investments_table (needs the $M figure), 13f (share count and
    CUSIP in the information table), 13g (share count or ownership %), generic.
    """
    note = parse_note(row.get("note"))
    sources = note.get("sources") or []
    mv = _f(row.get("market_value_usd"))
    shares = _f(row.get("shares_held"))
    pct = _f(row.get("ownership_pct"))
    fv_note = _f(note.get("fv_usd"))
    cusip = _s(row.get("cusip")) or _s(note.get("cusip"))

    inv_src = next((s for s in sources if "investments_table" in s), None)
    if inv_src and (fv_note is not None or mv is not None):
        fv = fv_note if fv_note is not None else mv
        return RowPlan(inv_src, "primary", [_target_musd(fv)], require_all=True)

    if any(s == "sec_api_13f" or s.startswith("13f") for s in sources):
        targets: list[SearchTarget] = []
        if shares is not None and shares > 0:
            targets.append(_target_shares(shares))
        if cusip:
            targets.append(_target_cusip(cusip))
        if not targets:
            return None
        return RowPlan("13f", "infotable", targets, require_all=True)

    if any(s == "13g" or s.startswith("13g") for s in sources):
        targets = []
        if shares is not None and shares > 0:
            targets.append(_target_shares(shares))
        if pct is not None and pct > 0:
            targets.append(_target_pct(pct))
        if not targets:
            return None
        return RowPlan("13g", "primary", targets, require_all=False)

    targets = []
    if mv is not None and mv > 0:
        targets.append(_target_musd(mv))
    if shares is not None and shares > 0:
        targets.append(_target_shares(shares))
    if not targets:
        return None
    return RowPlan("generic", "primary", targets, require_all=False)


def normalize_document_text(raw: str) -> str:
    """HTML/XML -> flat searchable text (tags stripped, whitespace collapsed)."""
    if not raw:
        return ""
    head = raw.lstrip()[:200].lower()
    if any(tag in head for tag in ("<html", "<!doctype", "<div", "<body", "<p", "<document")):
        try:
            from bs4 import BeautifulSoup

            text = BeautifulSoup(raw, "html.parser").get_text(" ")
        except Exception:
            text = re.sub(r"<[^>]+>", " ", raw)
    else:
        text = re.sub(r"<[^>]+>", " ", raw)
    text = text.replace("\xa0", " ").replace("&nbsp;", " ").replace("&#160;", " ")
    text = text.replace("&#8203;", "").replace("​", "")
    return re.sub(r"\s+", " ", text)


_EXHIBIT_NAME = re.compile(r"(?<![a-ce-wyz])(?:ex|exhibit)[\-_.]?\d", re.I)


def _pick_primary_document(docs: Iterable[dict]) -> str | None:
    """Choose the document the sheet value should live in.

    Order: ``primary_doc.xml`` (structured 13D/13G since 2024), then the largest
    non-exhibit ``.htm`` that is not an index / XBRL viewer page, then the largest
    exhibit ``.htm``, then the largest ``.txt``.
    """
    htm: list[tuple[int, str]] = []
    exhibits: list[tuple[int, str]] = []
    txt: list[tuple[int, str]] = []
    for d in docs:
        name = _s(d.get("name"))
        if not name:
            continue
        low = name.lower()
        if low == "primary_doc.xml":
            return name
        if "-index" in low or re.fullmatch(r"r\d+\.htm", low):
            continue
        if low in {"filingsummary.xml", "index.json"}:
            continue
        try:
            size = int(d.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        if low.endswith((".htm", ".html")):
            (exhibits if _EXHIBIT_NAME.search(low) else htm).append((size, name))
        elif low.endswith(".txt"):
            txt.append((size, name))
    pool = htm or exhibits or txt
    if not pool:
        return None
    return sorted(pool, key=lambda t: (-t[0], t[1]))[0][1]


class DocumentFetcher:
    """Fetch (and disk-cache) the text of a cited filing document.

    ``edgar`` needs ``list_filing_documents(cik, acc)``,
    ``fetch_filing_document(cik, acc, name)`` and ``fetch_13f_infotable(cik, acc)``,
    the same surface as ``EdgarResource``. Cache lives under ``.cache/reconcile``.
    """

    def __init__(self, edgar: Any, cache_dir: Path | None = None):
        self.edgar = edgar
        self.cache_dir = Path(cache_dir) if cache_dir is not None else DEFAULT_CACHE_DIR
        self._mem: dict[tuple[str, str, str], tuple[str, str]] = {}

    def _cache_paths(self, cik: str, accession_no: str, doc_kind: str) -> tuple[Path, Path]:
        nodash = accession_no.replace("-", "")
        d = self.cache_dir / str(int(cik)) / nodash
        return d / f"{doc_kind}.name", d / f"{doc_kind}.txt"

    def _store(self, key: tuple[str, str, str], name_p: Path, text_p: Path, name: str, raw: str):
        text = normalize_document_text(raw)
        name_p.parent.mkdir(parents=True, exist_ok=True)
        name_p.write_text(name + "\n", encoding="utf-8")
        text_p.write_text(text, encoding="utf-8")
        out = (name, text)
        self._mem[key] = out
        return out

    def get(self, cik: str, accession_no: str, doc_kind: str) -> tuple[str, str]:
        """Return ``(document_name, normalized_text)``; raises on fetch failure."""
        key = (str(int(cik)), accession_no, doc_kind)
        if key in self._mem:
            return self._mem[key]
        name_p, text_p = self._cache_paths(cik, accession_no, doc_kind)
        if name_p.is_file() and text_p.is_file():
            out = (name_p.read_text(encoding="utf-8").strip(), text_p.read_text(encoding="utf-8"))
            self._mem[key] = out
            return out

        if doc_kind == "infotable":
            raw = self.edgar.fetch_13f_infotable(cik, accession_no)
            if not raw:
                raise RuntimeError("no 13F information table in accession")
            name = "infotable.xml"
        else:
            docs = self.edgar.list_filing_documents(cik, accession_no)
            name = _pick_primary_document(docs)
            if not name:
                raise RuntimeError("no primary .htm/.txt document listed in accession")
            raw = self.edgar.fetch_filing_document(cik, accession_no, name)
        return self._store(key, name_p, text_p, name, raw)

    def get_url(self, url: str) -> tuple[str, str]:
        """Fetch an explicit EDGAR archive document URL (anchor ``source_url``)."""
        m = re.search(r"/edgar/data/(\d+)/(\d{18})/([^/?#]+)$", _s(url))
        if not m:
            raise RuntimeError(f"not an EDGAR archive document URL: {url}")
        cik, nodash, name = m.group(1), m.group(2), m.group(3)
        acc = f"{nodash[:10]}-{nodash[10:12]}-{nodash[12:]}"
        key = (str(int(cik)), acc, f"url:{name}")
        if key in self._mem:
            return self._mem[key]
        safe = hashlib.sha1(name.encode()).hexdigest()[:12]
        name_p, text_p = self._cache_paths(cik, acc, f"url_{safe}")
        if text_p.is_file():
            out = (name, text_p.read_text(encoding="utf-8"))
            self._mem[key] = out
            return out
        raw = self.edgar.fetch_filing_document(cik, acc, name)
        return self._store(key, name_p, text_p, name, raw)


def _find(text: str, target: SearchTarget) -> str | None:
    for pat in target.patterns:
        m = re.search(pat, text)
        if m:
            return m.group(0)
    return None


def _snippet(text: str, needle: str, width: int = 60) -> str:
    i = text.find(needle)
    if i < 0:
        return ""
    lo, hi = max(0, i - width), min(len(text), i + len(needle) + width)
    return text[lo:hi].strip()


def _base_result(row: dict, **extra: Any) -> dict:
    out = {c: "" for c in RESULT_COLUMNS}
    out.update(
        kind="history",
        period_end=_s(row.get("period_end"))[:10],
        investee_ticker=_s(row.get("investee_ticker")).upper(),
        accession_no=_s(row.get("accession_no")),
    )
    out.update(extra)
    return out


def reconcile_row(
    row: dict,
    fetcher: DocumentFetcher,
    *,
    parent_cik: str | None = None,
) -> dict:
    mv = _f(row.get("market_value_usd"))
    shares = _f(row.get("shares_held"))
    if (mv is None or mv <= 0) and (shares is None or shares <= 0):
        return _base_result(row, status=STATUS_SKIPPED_NULL, detail="no $ or share count")

    plan = plan_row(row)
    if plan is None:
        return _base_result(row, status=STATUS_SKIPPED_NULL, detail="nothing checkable")

    searched = " | ".join(f"{t.field}={t.display}" for t in plan.targets)
    fields = ",".join(t.field for t in plan.targets)
    sheet_values = ",".join(t.sheet_value for t in plan.targets)
    common = dict(
        field=fields, sheet_value=sheet_values, searched=searched, source_kind=plan.source_kind
    )
    acc = _s(row.get("accession_no"))
    if not acc:
        return _base_result(row, status=STATUS_NO_CITATION, detail="accession_no is blank", **common)
    note = parse_note(row.get("note"))
    cik = cik_from_filing_url(row.get("filing_url")) or _s(note.get("cik")) or _s(parent_cik)
    if not cik:
        return _base_result(
            row, status=STATUS_NO_CITATION, detail="no CIK (filing_url/note/parent)", **common
        )
    try:
        doc_name, text = fetcher.get(cik, acc, plan.doc_kind)
    except Exception as e:
        return _base_result(
            row,
            status=STATUS_FETCH_ERROR,
            cik=str(int(cik)),
            detail=f"{type(e).__name__}: {e}",
            **common,
        )

    hits: list[str] = []
    misses: list[str] = []
    snippet = ""
    for t in plan.targets:
        found = _find(text, t)
        if found:
            hits.append(f"{t.field}={found}")
            if not snippet:
                snippet = _snippet(text, found)
        else:
            misses.append(f"{t.field}={t.display}")
    ok = (not misses) if plan.require_all else bool(hits)
    detail = f"missing: {'; '.join(misses)}" if misses else ""
    if snippet:
        detail = (detail + " | " if detail else "") + f"context: …{snippet}…"
    return _base_result(
        row,
        status=STATUS_ANCHORED if ok else STATUS_NOT_FOUND,
        matched="; ".join(hits),
        cik=str(int(cik)),
        document=doc_name,
        detail=detail,
        **common,
    )


def reconcile_rows(
    rows: Iterable[dict],
    *,
    edgar: Any = None,
    cache_dir: Path | None = None,
    parent_cik: str | None = None,
    max_rows: int | None = None,
    fetcher: DocumentFetcher | None = None,
    progress: Callable[[dict], None] | None = None,
) -> list[dict]:
    """One result dict per history row; stops after ``max_rows`` checked (non-null) rows."""
    f = fetcher or DocumentFetcher(edgar, cache_dir)
    out: list[dict] = []
    checked = 0
    for row in rows:
        if max_rows is not None and checked >= max_rows:
            break
        res = reconcile_row(row, f, parent_cik=parent_cik)
        out.append(res)
        if res["status"] != STATUS_SKIPPED_NULL:
            checked += 1
        if progress:
            progress(res)
    return out


def anchors_path(parent: str, data_dir: Path | None = None) -> Path:
    key = _s(parent).lower()
    return (data_dir or _DATA_DIR) / f"{key}_anchors.yaml"


MUSD_ROUNDING_TOLERANCE_USD = 500_000.0


def _fmt_num(v: float) -> str:
    return f"{v:,.0f}" if abs(v) >= 1000 else f"{v:g}"


def load_anchors(parent: str, data_dir: Path | None = None) -> list[dict]:
    """Read ``<parent>_anchors.yaml``.

    ``expected_musd`` is shorthand for market_value_usd in $M and carries an
    implicit +/- $0.5M rounding tolerance (a filing prints whole millions);
    ``expected_value`` is exact unless ``tolerance_pct`` / ``tolerance_abs`` is set.
    """
    path = anchors_path(parent, data_dir)
    if not path.is_file():
        return []
    import yaml

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out: list[dict] = []
    for e in raw.get("anchors") or []:
        t = _s(e.get("investee_ticker") or e.get("ticker")).upper()
        pe = _s(e.get("period_end"))[:10]
        fld = _s(e.get("field")) or "market_value_usd"
        expected = _f(e.get("expected_value"))
        tolerance_abs = _f(e.get("tolerance_abs")) or 0.0
        if expected is None and e.get("expected_musd") is not None:
            expected = float(e["expected_musd"]) * 1_000_000.0
            fld = "market_value_usd"
            tolerance_abs = max(tolerance_abs, MUSD_ROUNDING_TOLERANCE_USD)
        if not t or not pe or expected is None:
            continue
        out.append(
            {
                "investee_ticker": t,
                "period_end": pe,
                "field": fld,
                "expected_value": expected,
                "tolerance_pct": _f(e.get("tolerance_pct")) or 0.0,
                "tolerance_abs": tolerance_abs,
                "source_url": _s(e.get("source_url")),
                "quote": _s(e.get("quote")),
            }
        )
    return out


def check_anchors(
    rows: Iterable[dict],
    anchors: list[dict],
    *,
    fetcher: DocumentFetcher | None = None,
) -> list[dict]:
    """Compare each anchor to the sheet row it names; verify the quote when a fetcher is given."""
    by_key: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        key = (_s(r.get("period_end"))[:10], _s(r.get("investee_ticker")).upper())
        by_key.setdefault(key, []).append(r)

    out: list[dict] = []
    for a in anchors:
        base = {c: "" for c in RESULT_COLUMNS}
        base.update(
            kind="anchor",
            period_end=a["period_end"],
            investee_ticker=a["investee_ticker"],
            field=a["field"],
            searched=f"expected {a['field']}={_fmt_num(a['expected_value'])}",
            source_kind="anchors.yaml",
            accession_no=a["source_url"],
        )
        matches = by_key.get((a["period_end"], a["investee_ticker"])) or []
        vals = [v for v in (_f(r.get(a["field"])) for r in matches) if v is not None]
        if not vals:
            out.append(
                {**base, "status": STATUS_ANCHOR_MISSING_ROW, "detail": "no sheet row / null field"}
            )
            continue
        exp = a["expected_value"]
        tol = max(abs(exp) * (a["tolerance_pct"] / 100.0), a.get("tolerance_abs") or 0.0)
        ok = any(abs(v - exp) <= tol + 1e-6 for v in vals)
        quote_note = ""
        if a["quote"] and a["source_url"] and fetcher is not None:
            try:
                doc_name, text = fetcher.get_url(a["source_url"])
                base["document"] = doc_name
                q = re.sub(r"\s+", " ", a["quote"]).strip()
                quote_note = "quote: found" if q in text else "quote: NOT found in source_url"
            except Exception as e:
                quote_note = f"quote: fetch_error {type(e).__name__}: {e}"
        shown = ",".join(_fmt_num(v) for v in vals)
        mismatch = "" if ok else f"sheet {shown} != expected {_fmt_num(exp)} (tol {_fmt_num(tol)})"
        out.append(
            {
                **base,
                "status": STATUS_ANCHOR_MATCH if ok else STATUS_ANCHOR_MISMATCH,
                "sheet_value": shown,
                "matched": f"{a['field']}={_fmt_num(vals[0])}" if ok else "",
                "detail": " | ".join(x for x in (mismatch, quote_note) if x),
            }
        )
    return out


def has_failures(results: Iterable[dict]) -> bool:
    return any(r.get("status") in FAILING_STATUSES for r in results)


def status_counts(results: Iterable[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in results:
        st = r.get("status", "")
        counts[st] = counts.get(st, 0) + 1
    return counts


def write_reconcile_csv(results: list[dict], path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=RESULT_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for r in results:
            w.writerow({c: r.get(c, "") for c in RESULT_COLUMNS})
    return path


def read_reconcile_csv(path: Path) -> list[dict]:
    path = Path(path)
    if not path.is_file():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def summary_table(results: list[dict]) -> str:
    lines = [
        f"{'status':<20}{'period_end':<12}{'ticker':<8}{'searched':<44}{'doc':<26}matched / detail"
    ]
    for r in results:
        if r.get("status") == STATUS_SKIPPED_NULL:
            continue
        tail = r.get("matched") or r.get("detail") or ""
        lines.append(
            f"{r.get('status', ''):<20}{r.get('period_end', ''):<12}"
            f"{r.get('investee_ticker', ''):<8}{r.get('searched', '')[:43]:<44}"
            f"{r.get('document', '')[:25]:<26}{tail[:70]}"
        )
    counts = status_counts(results)
    lines.append("")
    lines.append("counts: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return "\n".join(lines)
