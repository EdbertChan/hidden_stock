"""SEC EDGAR URL helpers for holdings export / verification."""

from __future__ import annotations


def edgar_filing_index_url(cik: str | None, accession_no: str | None) -> str | None:
    """Browser URL for an EDGAR filing index page.

    ``https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/{acc}-index.htm``
    """
    if not cik or accession_no is None:
        return None
    acc = str(accession_no).strip()
    if not acc or acc.lower() in {"nan", "none", "null"}:
        return None
    try:
        cik_int = int(str(cik).lstrip("0") or "0")
    except ValueError:
        return None
    if cik_int <= 0:
        return None
    nodash = acc.replace("-", "")
    return f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{nodash}/{acc}-index.htm"


def stamp_filing_urls(rows: list[dict], *, cik: str | None) -> list[dict]:
    """Set ``filing_url`` from CIK + accession when missing; keep HKEX/other URLs."""
    out: list[dict] = []
    for raw in rows:
        row = dict(raw)
        existing = row.get("filing_url")
        ex_s = str(existing).strip() if existing is not None else ""
        if ex_s.startswith("http") and "nan" not in ex_s.lower():
            out.append(row)
            continue
        url = edgar_filing_index_url(cik, row.get("accession_no"))
        row["filing_url"] = url
        out.append(row)
    return out
