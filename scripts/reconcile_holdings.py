#!/usr/bin/env python3
"""Reconcile an exported holdings history CSV against the EDGAR filings it cites.

  python scripts/reconcile_holdings.py --ticker UBER [--history-csv path] [--max-rows N]

For each history row with a $ value or share count, re-fetches the cited
accession from EDGAR (cached under .cache/reconcile) and asserts the literal
value string is present in that document. Also checks the optional
hand-curated hidden_stock/quirks/holdings/data/<parent>_anchors.yaml.

Writes exports/<slug>_reconcile.csv. Exit 1 when any row is not_found or an
anchor mismatches / has no sheet row. Never modifies the export.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from hidden_stock.quirks.holdings.reconcile import (  # noqa: E402
    DocumentFetcher,
    check_anchors,
    has_failures,
    load_anchors,
    reconcile_rows,
    status_counts,
    summary_table,
    write_reconcile_csv,
)


def _load_dotenv() -> None:
    env_path = _ROOT / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def parent_slug(parent: str) -> str:
    return parent.lower().replace("-", "")


def read_history_csv(path: Path) -> list[dict]:
    with Path(path).open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _build_edgar():
    from hidden_stock.resources.edgar_resource import EdgarResource

    ua = os.environ.get("SEC_EDGAR_USER_AGENT")
    if not ua:
        raise SystemExit("SEC_EDGAR_USER_AGENT is not set (see .env)")
    return EdgarResource(user_agent=ua)


def run(
    *,
    parent: str,
    history_csv: Path,
    out_csv: Path,
    edgar,
    cache_dir: Path | None = None,
    max_rows: int | None = None,
    anchors_dir: Path | None = None,
    parent_cik: str | None = None,
    quiet: bool = False,
) -> tuple[list[dict], int]:
    """Reconcile + anchors, write the CSV, print the summary; return (results, exit_code)."""
    if not Path(history_csv).is_file():
        print(f"missing history CSV: {history_csv}", file=sys.stderr)
        return [], 2
    rows = read_history_csv(history_csv)
    fetcher = DocumentFetcher(edgar, cache_dir)

    def _progress(res: dict) -> None:
        if not quiet:
            print(
                f"  {res['status']:<14} {res['period_end']} {res['investee_ticker']:<7} "
                f"{res.get('searched', '')}",
                flush=True,
            )

    results = reconcile_rows(
        rows, fetcher=fetcher, parent_cik=parent_cik, max_rows=max_rows, progress=_progress
    )
    anchors = load_anchors(parent, anchors_dir)
    results.extend(check_anchors(rows, anchors, fetcher=fetcher))
    write_reconcile_csv(results, out_csv)

    print()
    print(summary_table(results))
    print(f"anchors checked: {len(anchors)}")
    print(f"wrote {out_csv}")
    failed = has_failures(results)
    counts = status_counts(results)
    print("RECONCILE: " + ("FAIL" if failed else "PASS") + f" ({counts})")
    return results, (1 if failed else 0)


def main(argv: list[str] | None = None) -> int:
    _load_dotenv()
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--ticker", required=True)
    p.add_argument("--history-csv", default=None)
    p.add_argument("--out-dir", default=str(_ROOT / "exports"))
    p.add_argument("--max-rows", type=int, default=None)
    p.add_argument("--cache-dir", default=None)
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args(argv)

    from hidden_stock.quirks.holdings.parents import normalize_parent
    from hidden_stock.quirks.holdings.runner import PARENT_CIK_OVERRIDES

    parent = normalize_parent(args.ticker)
    slug = parent_slug(parent)
    out_dir = Path(args.out_dir)
    history_csv = Path(args.history_csv) if args.history_csv else out_dir / f"{slug}_equity_holdings_history.csv"
    out_csv = out_dir / f"{slug}_reconcile.csv"
    edgar = _build_edgar()
    parent_cik = PARENT_CIK_OVERRIDES.get(parent)

    _, code = run(
        parent=parent,
        history_csv=history_csv,
        out_csv=out_csv,
        edgar=edgar,
        cache_dir=Path(args.cache_dir) if args.cache_dir else None,
        max_rows=args.max_rows,
        parent_cik=parent_cik,
        quiet=args.quiet,
    )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
