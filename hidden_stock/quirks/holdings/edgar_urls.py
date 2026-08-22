"""SEC EDGAR URL helpers for holdings export / verification."""

from __future__ import annotations


def edgar_filing_index_url(cik: str | None, accession_no: str | None) -> str | None:
    """Browser URL for an EDGAR filing index page.

    ``https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/{acc}-index.htm``
    """
    if not cik or not accession_no:
        return None
    try:
        cik_int = int(str(cik).lstrip("0") or "0")
    except ValueError:
        return None
    if cik_int <= 0:
        return None
    acc = str(accession_no).strip()
    if not acc:
        return None
    nodash = acc.replace("-", "")
    return f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{nodash}/{acc}-index.htm"


def stamp_filing_urls(rows: list[dict], *, cik: str | None) -> list[dict]:
    """Set ``filing_url`` on each history row from CIK + accession_no."""
    out: list[dict] = []
    for raw in rows:
        row = dict(raw)
        row["filing_url"] = edgar_filing_index_url(cik, row.get("accession_no"))
        out.append(row)
    return out
