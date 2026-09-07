"""Mechanical precheck gates in scripts/grade_holdings_sheet.py (no LLM, no network)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pandas as pd
import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "grade_holdings_sheet.py"


@pytest.fixture(scope="module")
def grade():
    spec = importlib.util.spec_from_file_location("grade_holdings_sheet", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


HIST_COLS = [
    "period_end", "investee_ticker", "investee_name", "action", "shares_held",
    "shares_prev", "shares_delta", "ownership_pct", "market_value_usd", "note",
]


def _hist_row(pe, t, action, delta, mv=1.0, note="source=sec_api_13f"):
    return {
        "period_end": pe, "investee_ticker": t, "investee_name": t, "action": action,
        "shares_held": 10.0, "shares_prev": 10.0 - delta, "shares_delta": delta,
        "ownership_pct": None, "market_value_usd": mv, "note": note,
    }


def _write(out: Path, slug: str, hist_rows, realized_rows=None, returns_rows=None):
    hist = out / f"{slug}_equity_holdings_history.csv"
    pd.DataFrame(hist_rows, columns=HIST_COLS).to_csv(hist, index=False)
    port = out / f"{slug}_portfolio_by_period.csv"
    pd.DataFrame(
        [{"period_end": r["period_end"], "investee_ticker": r["investee_ticker"],
          "market_value_usd": r["market_value_usd"]} for r in hist_rows]
    ).to_csv(port, index=False)
    if realized_rows is not None:
        pd.DataFrame(
            realized_rows,
            columns=["period_end", "investee_ticker", "cost_method", "shares_sold", "realized_pnl_est"],
        ).to_csv(out / f"{slug}_realized_pnl_qoq.csv", index=False)
    if returns_rows is not None:
        pd.DataFrame(returns_rows, columns=["period_end", "dietz_return"]).to_csv(
            out / f"{slug}_returns_by_period.csv", index=False
        )
    return hist, port


def _issue_ids(res):
    return {i["id"] for i in res["blocking_issues"]} | {i["id"] for i in res["minor_issues"]}


def test_sell_without_realized_row_flags_uncovered_sale(grade, tmp_path):
    rows = [
        _hist_row("2024-03-31", "A", "new", 10.0),
        _hist_row("2024-06-30", "A", "sell", -4.0),
        _hist_row("2024-06-30", "B", "new", 3.0),
    ]
    hist, port = _write(tmp_path, "baba", rows, realized_rows=[
        {"period_end": "2024-06-30", "investee_ticker": "A", "cost_method": "fifo", "shares_sold": 4.0, "realized_pnl_est": 1.0},
    ])
    res = grade.mechanical_precheck(hist, port, parent="BABA")
    assert res["checks"]["sell_without_realized_row"] == "fail"
    assert "sell_without_realized_row" in _issue_ids(res)
    ev = next(i for i in res["blocking_issues"] if i["id"] == "sell_without_realized_row")["evidence"]
    assert "2024-06-30/A" in str(ev)
    assert res["verdict"] == "fail"


def test_sell_with_avg_realized_row_passes(grade, tmp_path):
    rows = [
        _hist_row("2024-03-31", "A", "new", 10.0),
        _hist_row("2024-06-30", "A", "sell", -4.0),
    ]
    hist, port = _write(tmp_path, "baba", rows, realized_rows=[
        {"period_end": "2024-06-30", "investee_ticker": "A", "cost_method": "avg", "shares_sold": 4.0, "realized_pnl_est": None},
        {"period_end": "2024-06-30", "investee_ticker": "A", "cost_method": "fifo", "shares_sold": 4.0, "realized_pnl_est": 1.0},
    ])
    res = grade.mechanical_precheck(hist, port, parent="BABA")
    assert res["checks"]["sell_without_realized_row"] == "pass"
    assert "sell_without_realized_row" not in _issue_ids(res)


def test_sell_without_realized_csv_is_unknown_not_pass(grade, tmp_path):
    rows = [_hist_row("2024-03-31", "A", "new", 10.0), _hist_row("2024-06-30", "A", "exit", -10.0, mv=None)]
    hist, port = _write(tmp_path, "baba", rows, realized_rows=None)
    res = grade.mechanical_precheck(hist, port, parent="BABA")
    assert res["checks"]["sell_without_realized_row"] == "unknown"


def _judge(name, verdict="pass", checks=None):
    return {
        "judge": name, "verdict": verdict, "score": 100 if verdict == "pass" else 50,
        "blocking_issues": [], "minor_issues": [], "what_looks_good": [],
        "checks": checks or {}, "summary": f"{name} ok",
    }


def test_board_with_unknown_check_is_needs_work_not_pass(grade, tmp_path):
    results = [
        _judge("mechanical", checks={"aur_one_per_period": "pass", "chart_ranking_sane": "unknown"}),
        _judge("fable", checks={"didi_2026_06_30_fv": "n/a", "no_share_invent": "unknown"}),
    ]
    path = grade.write_board("BABA", tmp_path, results, None)
    text = path.read_text(encoding="utf-8")
    assert "BOARD: NEEDS_WORK" in text
    assert "BOARD: PASS" not in text
    assert "mechanical:chart_ranking_sane" in text
    assert "fable:no_share_invent" in text


def test_board_all_checks_resolved_is_pass(grade, tmp_path):
    results = [
        _judge("mechanical", checks={"aur_one_per_period": "pass", "didi_2026_06_30_fv": "n/a"}),
        _judge("fable", checks={"no_share_invent": "pass"}),
    ]
    text = grade.write_board("BABA", tmp_path, results, None).read_text(encoding="utf-8")
    assert "BOARD: PASS" in text


def test_period_grid_gap_flags_missing_calendar_quarter(grade, tmp_path):
    rows = [
        _hist_row("2024-03-31", "A", "new", 10.0),
        _hist_row("2024-09-30", "A", "hold", 0.0),
        _hist_row("2025-03-31", "A", "hold", 0.0),
    ]
    hist, port = _write(tmp_path, "baba", rows, realized_rows=[])
    res = grade.mechanical_precheck(hist, port, parent="BABA")
    assert res["checks"]["period_grid_gap"] == "fail"
    gap = next(i for i in res["minor_issues"] if i["id"] == "period_grid_gap")
    assert gap["evidence"] == ["2024-06-30", "2024-12-31"]
    assert res["verdict"] == "needs_work"
    assert not res["blocking_issues"]


def test_period_grid_gap_contiguous_quarters_pass(grade, tmp_path):
    rows = [_hist_row(pe, "A", "hold", 0.0) for pe in ("2024-03-31", "2024-06-30", "2024-09-30")]
    hist, port = _write(tmp_path, "baba", rows, realized_rows=[])
    res = grade.mechanical_precheck(hist, port, parent="BABA")
    assert res["checks"]["period_grid_gap"] == "pass"
    assert res["verdict"] == "pass"


def test_period_grid_gap_not_applicable_on_13g_date_grid(grade, tmp_path):
    """HK parents use 13G filing dates as period_end — not a quarterly grid."""
    rows = [_hist_row(pe, "A", "hold", 0.0) for pe in ("2024-02-14", "2024-11-20")]
    hist, port = _write(tmp_path, "tcehy", rows, realized_rows=[])
    res = grade.mechanical_precheck(hist, port, parent="TCEHY")
    assert res["checks"]["period_grid_gap"] == "n/a"
