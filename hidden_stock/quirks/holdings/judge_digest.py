"""Derived, bounded digest of a holdings export for LLM judges.

Judges used to get a ~12KB head-slice of the raw history CSV, so anything
past the slice (a missing realized row, a dropped 2021 quarter) was
invisible to them. This module reads every row of the export CSVs and
aggregates them into a compact digest: per ticker × period coverage codes,
every sell/exit with its realized-row status, every period's Dietz + flow,
provenance counts, the mechanical precheck, and a ``since_last_digest`` diff
against the previous digest JSON in the same directory.

Two rules:

* Every row is covered by aggregation. Detail lists may be shortened for
  size, but only with a ``truncated:`` line carrying the dropped count.
* A content hash of the export CSVs is recorded when the LLM judges run, so
  ``--judge-mode full`` can refuse to re-judge an unchanged export.

Coverage codes are one character per (ticker, period_end) cell; see
``COVERAGE_LEGEND``.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

EXPORT_SUFFIXES = (
    "_equity_holdings_history.csv",
    "_equity_holdings.csv",
    "_realized_pnl_qoq.csv",
    "_returns_by_period.csv",
    "_holdings_qoq_chart.csv",
)

DIGEST_JSON_SUFFIX = "_judge_digest.json"
DIGEST_MD_SUFFIX = "_judge_digest.md"
LAST_JUDGED_SUFFIX = "_last_judged.json"

COVERAGE_LEGEND = (
    "$=$ and shares, d=$ only, s=shares only, p=ownership_pct only, "
    ".=row with no $/shares/pct, x=action=exit, -=no row"
)

_SOURCE_RE = re.compile(r"(?<![a-z_])source=([a-z0-9_]+)", re.IGNORECASE)


def export_paths(out_dir: Path, slug: str) -> dict[str, Path]:
    slug = slug.lower()
    return {
        "history": out_dir / f"{slug}_equity_holdings_history.csv",
        "positions": out_dir / f"{slug}_equity_holdings.csv",
        "realized": out_dir / f"{slug}_realized_pnl_qoq.csv",
        "returns": out_dir / f"{slug}_returns_by_period.csv",
        "chart": out_dir / f"{slug}_holdings_qoq_chart.csv",
    }


def export_content_hash(out_dir: Path, slug: str) -> str:
    """sha256 over (name, bytes) of every present export CSV, sorted by name."""
    h = hashlib.sha256()
    for name, path in sorted(export_paths(out_dir, slug).items()):
        if not path.is_file():
            continue
        h.update(name.encode())
        h.update(b"\0")
        h.update(path.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def _last_judged_path(out_dir: Path, slug: str, scope: str) -> Path:
    scope_part = "" if scope == "sheet" else f"_{scope}"
    return out_dir / f"{slug.lower()}{scope_part}{LAST_JUDGED_SUFFIX}"


def read_last_judged(out_dir: Path, slug: str, *, scope: str = "sheet") -> dict | None:
    path = _last_judged_path(out_dir, slug, scope)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return {"content_hash": None, "error": f"{path}: {e}"}
    return data if isinstance(data, dict) else None


def record_last_judged(
    out_dir: Path,
    slug: str,
    *,
    content_hash: str,
    judges: list[str],
    scope: str = "sheet",
) -> Path:
    path = _last_judged_path(out_dir, slug, scope)
    path.write_text(
        json.dumps(
            {
                "content_hash": content_hash,
                "judged_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "judges": list(judges),
                "scope": scope,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def full_judge_skip_reason(
    out_dir: Path,
    slug: str,
    *,
    judge_mode: str,
    force: bool,
    scope: str = "sheet",
    content_hash: str | None = None,
) -> str | None:
    """Why the LLM judges will NOT run this invocation, or None if they will.

    ``mechanical`` mode never runs LLM judges. ``full`` mode runs them once per
    export content hash unless ``force``.
    """
    if judge_mode != "full":
        return (
            f"judge-mode={judge_mode}: LLM judges skipped "
            "(iterate mechanically; run --judge-mode full once on the final export)"
        )
    if force:
        return None
    last = read_last_judged(out_dir, slug, scope=scope)
    if not last:
        return None
    content_hash = content_hash or export_content_hash(out_dir, slug)
    if last.get("content_hash") == content_hash:
        return (
            f"export content hash {content_hash[:12]} already judged at "
            f"{last.get('judged_at')} by {','.join(last.get('judges') or [])} "
            f"({_last_judged_path(out_dir, slug, scope).name}); "
            "nothing changed since — pass --force to re-judge"
        )
    return None


def _read(path: Path) -> pd.DataFrame | None:
    if not path.is_file():
        return None
    try:
        return pd.read_csv(path)
    except pd.errors.EmptyDataError:
        return pd.DataFrame()


def _num(df: pd.DataFrame, col: str) -> pd.Series:
    if col not in df.columns:
        return pd.Series([float("nan")] * len(df), index=df.index, dtype=float)
    return pd.to_numeric(df[col], errors="coerce")


def _str(df: pd.DataFrame, col: str) -> pd.Series:
    if col not in df.columns:
        return pd.Series([""] * len(df), index=df.index, dtype=object)
    return df[col].fillna("").astype(str).str.strip()


def _tickers(df: pd.DataFrame) -> pd.Series:
    if "investee_ticker" not in df.columns:
        return pd.Series([""] * len(df), index=df.index, dtype=object)
    return df["investee_ticker"].map(_ticker_key)


def _ticker_key(value: Any) -> str:
    s = str(value or "").strip().upper()
    return "" if s in {"", "NAN", "NONE", "NAT"} else s


def _pos(v: Any) -> bool:
    return v is not None and v == v and v > 0


def _cell_code(action: str, mv: Any, sh: Any, pct: Any) -> str:
    if action.lower() == "exit":
        return "x"
    if _pos(mv) and _pos(sh):
        return "$"
    if _pos(mv):
        return "d"
    if _pos(sh):
        return "s"
    if _pos(pct):
        return "p"
    return "."


def _coverage(hist: pd.DataFrame) -> tuple[list[str], dict[str, dict[str, str]]]:
    """Return (periods, {ticker: {period: code}}) covering every history row.

    A second row for the same ticker × period is coded ``D`` (duplicate) so
    the judge sees it even though the mechanical precheck FAILs it too.
    """
    if hist is None or hist.empty or "period_end" not in hist.columns:
        return [], {}
    pe = _str(hist, "period_end").str[:10]
    tk = _tickers(hist)
    mv = _num(hist, "market_value_usd")
    sh = _num(hist, "shares_held")
    pct = _num(hist, "ownership_pct")
    action = _str(hist, "action")
    periods = sorted({p for p in pe if p})
    grid: dict[str, dict[str, str]] = {}
    for i in range(len(hist)):
        p = pe.iloc[i]
        if not p:
            continue
        t = tk.iloc[i] or "(blank)"
        code = _cell_code(action.iloc[i], mv.iloc[i], sh.iloc[i], pct.iloc[i])
        cell = grid.setdefault(t, {})
        cell[p] = code if p not in cell else "D"
    return periods, grid


def _sells(hist: pd.DataFrame, realized: pd.DataFrame | None) -> list[dict]:
    """Every sell/exit/13g_exit row with its realized_pnl_qoq coverage."""
    if hist is None or hist.empty:
        return []
    action = _str(hist, "action").str.lower()
    note = _str(hist, "note")
    delta = _num(hist, "shares_delta")
    mask = action.isin({"sell", "exit"}) | note.str.contains("13g_exit=1", na=False)
    if not mask.any():
        return []
    covered: dict[tuple[str, str], list[str]] = {}
    realized_present = realized is not None
    if (
        realized is not None
        and not realized.empty
        and {"period_end", "investee_ticker"} <= set(realized.columns)
    ):
        rpe = _str(realized, "period_end").str[:10]
        rtk = _tickers(realized)
        rmethod = _str(realized, "cost_method").str.lower()
        rstatus = _str(realized, "cost_basis_status").str.lower()
        for i in range(len(realized)):
            covered.setdefault((rpe.iloc[i], rtk.iloc[i]), []).append(
                f"{rmethod.iloc[i] or 'avg'}:{rstatus.iloc[i] or '?'}"
            )
    out: list[dict] = []
    pe = _str(hist, "period_end").str[:10]
    tk = _tickers(hist)
    for i in hist.index[mask]:
        key = (pe.loc[i], tk.loc[i])
        d = delta.loc[i]
        rows = covered.get(key)
        if rows:
            has_avg = any(r.startswith("avg:") for r in rows)
            status = ";".join(sorted(set(rows))) + ("" if has_avg else " (no avg row)")
        elif not realized_present:
            status = "UNKNOWN (no realized_pnl_qoq.csv)"
        else:
            status = "MISSING"
        out.append(
            {
                "period_end": key[0],
                "ticker": key[1] or "(blank)",
                "action": action.loc[i] or ("13g_exit" if "13g_exit=1" in note.loc[i] else ""),
                "shares_delta": None if d != d else float(d),
                "realized": status,
            }
        )
    out.sort(key=lambda r: (r["period_end"], r["ticker"]))
    return out


def _returns(returns: pd.DataFrame | None) -> list[dict]:
    if returns is None or returns.empty or "period_end" not in returns.columns:
        return []
    pe = _str(returns, "period_end").str[:10]
    dz = _num(returns, "dietz_return")
    flow = _num(returns, "net_external_flow")
    end = _num(returns, "portfolio_mv_end")

    def _f(v: Any, nd: int | None = None) -> float | None:
        if v != v:
            return None
        return round(float(v), nd) if nd is not None else float(v)

    return [
        {
            "period_end": pe.iloc[i],
            "dietz": _f(dz.iloc[i], 6),
            "flow": _f(flow.iloc[i]),
            "mv_end": _f(end.iloc[i]),
        }
        for i in range(len(returns))
    ]


def _provenance(hist: pd.DataFrame | None) -> dict:
    if hist is None or hist.empty:
        return {"by_source": {}, "dollar_rows": 0, "dollar_rows_without_citation": []}
    note = _str(hist, "note")
    mv = _num(hist, "market_value_usd")
    acc = _str(hist, "accession_no")
    pe = _str(hist, "period_end").str[:10]
    tk = _tickers(hist)
    by_source: dict[str, int] = {}
    no_cite: list[str] = []
    dollar_rows = 0
    for i in range(len(hist)):
        srcs = sorted({m.lower() for m in _SOURCE_RE.findall(note.iloc[i])}) or ["none"]
        key = "+".join(srcs)
        by_source[key] = by_source.get(key, 0) + 1
        if _pos(mv.iloc[i]):
            dollar_rows += 1
            if not acc.iloc[i] and srcs == ["none"]:
                no_cite.append(f"{pe.iloc[i]}/{tk.iloc[i] or '(blank)'}")
    return {
        "by_source": dict(sorted(by_source.items())),
        "dollar_rows": dollar_rows,
        "dollar_rows_without_citation": no_cite,
    }


def _chart(chart: pd.DataFrame | None) -> dict:
    if chart is None or chart.empty or "period_end" not in chart.columns:
        return {"periods": 0, "first": None, "last": None, "tickers": {}}
    tickers = {
        str(c).upper(): int(_num(chart, c).notna().sum())
        for c in chart.columns
        if c != "period_end"
    }
    pes = sorted({p for p in _str(chart, "period_end").str[:10] if p})
    return {
        "periods": len(pes),
        "first": pes[0] if pes else None,
        "last": pes[-1] if pes else None,
        "tickers": tickers,
    }


def _positions(pos: pd.DataFrame | None) -> dict:
    if pos is None or pos.empty:
        return {"rows": 0, "tickers": [], "with_dollars": 0}
    tk = _tickers(pos)
    mv = _num(pos, "market_value_usd")
    return {
        "rows": int(len(pos)),
        "tickers": sorted({t for t in tk if t}),
        "with_dollars": int(((mv == mv) & (mv > 0)).sum()),
    }


def build_digest_data(
    out_dir: Path,
    slug: str,
    *,
    mechanical: dict | None = None,
) -> dict:
    """Structured digest (the JSON persisted for the next iteration's diff)."""
    paths = export_paths(out_dir, slug)
    frames = {k: _read(p) for k, p in paths.items()}
    hist = frames["history"]
    periods, grid = _coverage(hist) if hist is not None else ([], {})
    mech = mechanical or {}
    return {
        "slug": slug.lower(),
        "content_hash": export_content_hash(out_dir, slug),
        "files": {
            k: {
                "present": p.is_file(),
                "rows": 0 if frames[k] is None else int(len(frames[k])),
            }
            for k, p in paths.items()
        },
        "periods": periods,
        "coverage": grid,
        "sells": _sells(hist, frames["realized"]) if hist is not None else [],
        "returns": _returns(frames["returns"]),
        "provenance": _provenance(hist),
        "chart": _chart(frames["chart"]),
        "positions": _positions(frames["positions"]),
        "mechanical": {
            "verdict": mech.get("verdict"),
            "checks": dict(mech.get("checks") or {}),
            "blocking": [str(i.get("id")) for i in (mech.get("blocking_issues") or [])],
            "minor": [str(i.get("id")) for i in (mech.get("minor_issues") or [])],
        },
    }


_DIFF_KEYS = (
    "rows",
    "tickers_added",
    "tickers_removed",
    "periods_added",
    "periods_removed",
    "coverage_changes",
    "sells_changed",
    "returns_changed",
    "checks_changed",
)


def diff_digests(prev: dict | None, cur: dict) -> dict:
    """What changed between two digest JSON dicts (rows, coverage, sells, returns, checks)."""
    if not prev:
        return {"previous": None}
    out: dict[str, Any] = {
        "previous": prev.get("content_hash"),
        "unchanged": prev.get("content_hash") == cur.get("content_hash"),
        "rows": {},
        "tickers_added": [],
        "tickers_removed": [],
        "periods_added": [],
        "periods_removed": [],
        "coverage_changes": [],
        "sells_changed": [],
        "returns_changed": [],
        "checks_changed": [],
    }
    for k, cur_f in (cur.get("files") or {}).items():
        prev_n = ((prev.get("files") or {}).get(k) or {}).get("rows")
        if prev_n != cur_f.get("rows"):
            out["rows"][k] = [prev_n, cur_f.get("rows")]
    pc, cc = prev.get("coverage") or {}, cur.get("coverage") or {}
    out["tickers_added"] = sorted(set(cc) - set(pc))
    out["tickers_removed"] = sorted(set(pc) - set(cc))
    pp, cp = set(prev.get("periods") or []), set(cur.get("periods") or [])
    out["periods_added"] = sorted(cp - pp)
    out["periods_removed"] = sorted(pp - cp)
    for t in sorted(set(pc) & set(cc)):
        for p in sorted(set(pc[t]) | set(cc[t])):
            a, b = pc[t].get(p, "-"), cc[t].get(p, "-")
            if a != b:
                out["coverage_changes"].append(f"{t}@{p}: {a}->{b}")
    ps = {f"{s['period_end']}/{s['ticker']}": s.get("realized") for s in prev.get("sells") or []}
    cs = {f"{s['period_end']}/{s['ticker']}": s.get("realized") for s in cur.get("sells") or []}
    for k in sorted(set(ps) | set(cs)):
        if ps.get(k) != cs.get(k):
            out["sells_changed"].append(
                f"{k}: {ps.get(k, '(absent)')} -> {cs.get(k, '(absent)')}"
            )
    pr = {r["period_end"]: r for r in prev.get("returns") or []}
    cr = {r["period_end"]: r for r in cur.get("returns") or []}
    for k in sorted(set(pr) | set(cr)):
        a, b = pr.get(k), cr.get(k)
        if a is None or b is None:
            out["returns_changed"].append(f"{k}: {'added' if a is None else 'removed'}")
        elif a.get("dietz") != b.get("dietz") or a.get("flow") != b.get("flow"):
            out["returns_changed"].append(
                f"{k}: dietz {a.get('dietz')}->{b.get('dietz')} "
                f"flow {a.get('flow')}->{b.get('flow')}"
            )
    pk = (prev.get("mechanical") or {}).get("checks") or {}
    ck = (cur.get("mechanical") or {}).get("checks") or {}
    for k in sorted(set(pk) | set(ck)):
        if pk.get(k) != ck.get(k):
            out["checks_changed"].append(
                f"{k}: {pk.get(k, '(absent)')}->{ck.get(k, '(absent)')}"
            )
    return out


def _fmt_money(v: float | None) -> str:
    if v is None:
        return "null"
    a = abs(v)
    if a >= 1e9:
        return f"{v / 1e9:.2f}B"
    if a >= 1e6:
        return f"{v / 1e6:.1f}M"
    return f"{v:,.0f}"


def _bounded_list(lines: list[str], limit: int, label: str) -> list[str]:
    if len(lines) <= limit:
        return lines
    kept = lines[:limit]
    kept.append(
        f"truncated: {len(lines) - limit} of {len(lines)} {label} not listed "
        "(counts above cover all rows)"
    )
    return kept


_DEFAULT_LIMITS = {
    "sells": 60,
    "returns": 80,
    "coverage_changes": 60,
    "no_cite": 20,
    "other": 30,
}


def render_digest(data: dict, *, since: dict | None = None, max_chars: int = 8000) -> str:
    """Markdown digest. Detail lists halve until the text fits ``max_chars``.

    Aggregates (coverage grid, counts, per-status totals) are never dropped;
    only itemised lists shrink, each leaving a ``truncated:`` line.
    """
    since = since or {"previous": None}
    limits = dict(_DEFAULT_LIMITS)
    text = _render(data, since, limits)
    while len(text) > max_chars and any(v > 4 for v in limits.values()):
        limits = {k: max(4, v // 2) for k, v in limits.items()}
        text = _render(data, since, limits)
    return text


def _render(data: dict, since: dict, limits: dict[str, int]) -> str:
    L: list[str] = []
    files = data.get("files") or {}
    L.append(
        f"## Judge digest — {data.get('slug', '').upper()} "
        "(derived from every export row; not a CSV slice)"
    )
    L.append("")
    L.append(f"content_hash: {data.get('content_hash')}")
    L.append(
        "files: "
        + ", ".join(
            f"{k}={'missing' if not v.get('present') else str(v.get('rows')) + ' rows'}"
            for k, v in files.items()
        )
    )
    L.append("")

    periods = data.get("periods") or []
    grid = data.get("coverage") or {}
    L.append(f"### Coverage grid ({len(grid)} tickers × {len(periods)} periods)")
    L.append(f"legend: {COVERAGE_LEGEND}; D=duplicate row same period")
    if periods:
        L.append(f"periods ({len(periods)}): " + " ".join(periods))
    for t in sorted(grid):
        cells = grid[t]
        row = "".join(cells.get(p, "-") for p in periods)
        have = sorted(cells)
        n_mv = sum(1 for c in cells.values() if c in {"$", "d"})
        n_sh = sum(1 for c in cells.values() if c in {"$", "s"})
        n_null = sum(1 for c in cells.values() if c == ".")
        L.append(
            f"- {t}: {row}  rows={len(cells)} first={have[0]} last={have[-1]} "
            f"$={n_mv} shares={n_sh} null={n_null}"
        )
    if not grid:
        L.append("(no history rows)")
    L.append("")

    sells = data.get("sells") or []
    by_status: dict[str, int] = {}
    for s in sells:
        r = str(s.get("realized") or "")
        k = "MISSING" if r == "MISSING" else "UNKNOWN" if r.startswith("UNKNOWN") else "covered"
        by_status[k] = by_status.get(k, 0) + 1
    L.append(
        f"### Sells / exits ({len(sells)} rows; realized coverage: "
        f"{json.dumps(by_status) if by_status else 'n/a'})"
    )
    sell_lines = [
        f"- {s['period_end']} {s['ticker']} {s['action']} Δsh={s['shares_delta']} "
        f"realized={s['realized']}"
        for s in sells
    ]
    L.extend(_bounded_list(sell_lines, limits["sells"], "sell rows") or ["(none)"])
    L.append("")

    rets = data.get("returns") or []
    L.append(f"### Returns by period ({len(rets)} periods; dietz as %, flow/mv_end USD)")
    ret_lines = []
    for r in rets:
        dz = "null" if r["dietz"] is None else f"{r['dietz'] * 100:.2f}%"
        ret_lines.append(
            f"- {r['period_end']}: dietz={dz} flow={_fmt_money(r['flow'])} "
            f"mv_end={_fmt_money(r['mv_end'])}"
        )
    L.extend(_bounded_list(ret_lines, limits["returns"], "return rows") or ["(none)"])
    L.append("")

    prov = data.get("provenance") or {}
    L.append("### Provenance")
    L.append(f"rows by note source: {json.dumps(prov.get('by_source') or {})}")
    nc = prov.get("dollar_rows_without_citation") or []
    L.append(
        f"$ rows: {prov.get('dollar_rows', 0)}; "
        f"$ rows with no accession and no source=: {len(nc)}"
    )
    if nc:
        L.extend(_bounded_list([f"- {x}" for x in nc], limits["no_cite"], "uncited $ rows"))
    L.append("")

    chart = data.get("chart") or {}
    pos = data.get("positions") or {}
    L.append("### Chart + positions")
    L.append(
        f"holdings_qoq_chart: {chart.get('periods', 0)} periods "
        f"{chart.get('first')}…{chart.get('last')}; non-null per ticker: "
        f"{json.dumps(chart.get('tickers') or {})}"
    )
    L.append(
        f"current positions: {pos.get('rows', 0)} rows, {pos.get('with_dollars', 0)} with $; "
        f"tickers: {', '.join(pos.get('tickers') or []) or '(none)'}"
    )
    L.append("")

    mech = data.get("mechanical") or {}
    L.append(f"### Mechanical precheck: verdict={mech.get('verdict')}")
    L.append(f"checks: {json.dumps(mech.get('checks') or {})}")
    if mech.get("blocking"):
        L.append(f"blocking: {', '.join(mech['blocking'])}")
    if mech.get("minor"):
        L.append(f"minor: {', '.join(mech['minor'])}")
    L.append("")

    L.append("### since_last_digest")
    if since.get("previous") is None:
        L.append("(no previous digest in this directory — first iteration)")
    elif since.get("unchanged"):
        L.append(f"export unchanged since previous digest {str(since.get('previous'))[:12]}")
    else:
        L.append(f"previous content_hash: {str(since.get('previous'))[:12]}")
        if since.get("rows"):
            L.append(
                "row counts: "
                + ", ".join(f"{k} {a}->{b}" for k, (a, b) in since["rows"].items())
            )
        for key, label in (
            ("tickers_added", "tickers added"),
            ("tickers_removed", "tickers removed"),
            ("periods_added", "periods added"),
            ("periods_removed", "periods removed"),
        ):
            if since.get(key):
                L.append(f"{label}: {', '.join(since[key])}")
        for key, label, lim in (
            ("coverage_changes", "coverage cell changes", limits["coverage_changes"]),
            ("sells_changed", "sell/realized changes", limits["other"]),
            ("returns_changed", "return changes", limits["other"]),
            ("checks_changed", "precheck changes", limits["other"]),
        ):
            items = since.get(key) or []
            if items:
                L.append(f"{label} ({len(items)}):")
                L.extend(_bounded_list([f"- {x}" for x in items], lim, label))
        if not any(since.get(k) for k in _DIFF_KEYS):
            L.append("(hash changed but no tracked field differs — note/cosmetic edits only)")
    L.append("")
    return "\n".join(L)


def write_digest(
    out_dir: Path,
    slug: str,
    *,
    mechanical: dict | None = None,
    max_chars: int = 8000,
) -> tuple[str, dict, Path]:
    """Build, diff against the previous JSON, persist, and return (md, data, json_path)."""
    slug = slug.lower()
    json_path = out_dir / f"{slug}{DIGEST_JSON_SUFFIX}"
    previous: dict | None = None
    if json_path.is_file():
        try:
            previous = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            print(f"judge_digest: could not read previous digest {json_path}: {e}")
    data = build_digest_data(out_dir, slug, mechanical=mechanical)
    since = diff_digests(previous, data)
    data["since_last_digest"] = since
    text = render_digest(data, since=since, max_chars=max_chars)
    json_path.write_text(json.dumps(data, indent=2, default=str) + "\n", encoding="utf-8")
    (out_dir / f"{slug}{DIGEST_MD_SUFFIX}").write_text(text + "\n", encoding="utf-8")
    return text, data, json_path
