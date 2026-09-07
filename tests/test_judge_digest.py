"""Judge digest: every export row aggregated for LLM judges (no CSV slice),
plus the once-per-final-export gate in scripts/grade_holdings_sheet.py.

No LLM, no network: the grade script's LLM runner is injected as a fake.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pandas as pd
import pytest

from hidden_stock.quirks.holdings import judge_digest as jd

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "grade_holdings_sheet.py"

HIST_COLS = [
    "period_end", "investee_ticker", "investee_name", "action", "shares_held",
    "shares_prev", "shares_delta", "ownership_pct", "market_value_usd",
    "accession_no", "note",
]
QUARTERS = ("03-31", "06-30", "09-30", "12-31")


@pytest.fixture(scope="module")
def grade():
    spec = importlib.util.spec_from_file_location("grade_holdings_sheet", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _row(pe, t, action="hold", *, sh=10.0, delta=0.0, pct=None, mv=1e9,
         acc="0001-23-000001", note="source=sec_api_13f"):
    return {
        "period_end": pe, "investee_ticker": t, "investee_name": t, "action": action,
        "shares_held": sh, "shares_prev": None if sh is None else sh - delta,
        "shares_delta": delta, "ownership_pct": pct, "market_value_usd": mv,
        "accession_no": acc, "note": note,
    }


def _write_export(out: Path, slug: str, hist_rows, *, realized_rows=None,
                  returns_rows=None, chart=None, positions=None):
    pd.DataFrame(hist_rows, columns=HIST_COLS).to_csv(
        out / f"{slug}_equity_holdings_history.csv", index=False
    )
    pd.DataFrame(
        [{"period_end": r["period_end"], "investee_ticker": r["investee_ticker"],
          "market_value_usd": r["market_value_usd"]} for r in hist_rows]
    ).to_csv(out / f"{slug}_portfolio_by_period.csv", index=False)
    if realized_rows is not None:
        pd.DataFrame(
            realized_rows,
            columns=["period_end", "investee_ticker", "shares_sold", "cost_method",
                     "cost_basis_status", "realized_pnl_est"],
        ).to_csv(out / f"{slug}_realized_pnl_qoq.csv", index=False)
    if returns_rows is not None:
        pd.DataFrame(
            returns_rows,
            columns=["period_end", "portfolio_mv_end", "net_external_flow", "dietz_return"],
        ).to_csv(out / f"{slug}_returns_by_period.csv", index=False)
    if chart is not None:
        chart.to_csv(out / f"{slug}_holdings_qoq_chart.csv", index=False)
    if positions is not None:
        pd.DataFrame(positions).to_csv(out / f"{slug}_equity_holdings.csv", index=False)


def _quarters(start_year: int, n: int) -> list[str]:
    out = []
    y, qi = start_year, 3
    for _ in range(n):
        out.append(f"{y}-{QUARTERS[qi]}")
        qi += 1
        if qi == 4:
            qi, y = 0, y + 1
    return out


def _coverage_line(text: str, ticker: str) -> str:
    return next(l for l in text.splitlines() if l.startswith(f"- {ticker}: "))


def test_coverage_grid_codes_every_ticker_period_cell(tmp_path):
    rows = [
        _row("2024-03-31", "A"),
        _row("2024-06-30", "A", mv=None),
        _row("2024-09-30", "A", sh=None, mv=2e9),
        _row("2024-06-30", "B", sh=None, mv=None, pct=5.0, note="source=13g"),
        _row("2024-09-30", "B", "exit", sh=0.0, delta=-10.0, mv=None, note="source=13g 13g_exit=1"),
        _row("2024-09-30", "C", sh=None, mv=None, pct=None, note="source=13g"),
    ]
    _write_export(tmp_path, "baba", rows, realized_rows=[])
    data = jd.build_digest_data(tmp_path, "baba")
    assert data["periods"] == ["2024-03-31", "2024-06-30", "2024-09-30"]
    assert data["coverage"] == {
        "A": {"2024-03-31": "$", "2024-06-30": "s", "2024-09-30": "d"},
        "B": {"2024-06-30": "p", "2024-09-30": "x"},
        "C": {"2024-09-30": "."},
    }
    text = jd.render_digest(data)
    assert "### Coverage grid (3 tickers × 3 periods)" in text
    assert _coverage_line(text, "A").startswith("- A: $sd  rows=3 first=2024-03-31 last=2024-09-30 $=2 shares=2 null=0")
    assert _coverage_line(text, "B").startswith("- B: -px  rows=2")
    assert _coverage_line(text, "C").startswith("- C: --.  rows=1")
    assert data["provenance"]["by_source"] == {"13g": 3, "sec_api_13f": 3}


def test_duplicate_period_ticker_is_coded_D(tmp_path):
    rows = [_row("2024-03-31", "A"), _row("2024-03-31", "A", note="source=13g")]
    _write_export(tmp_path, "baba", rows)
    assert jd.build_digest_data(tmp_path, "baba")["coverage"]["A"] == {"2024-03-31": "D"}


def test_sell_without_realized_row_is_MISSING_and_counted(tmp_path):
    rows = [
        _row("2024-03-31", "A"),
        _row("2024-06-30", "A", "sell", delta=-4.0),
        _row("2024-03-31", "SERV"),
        _row("2024-06-30", "SERV", "sell", delta=-2.0),
        _row("2024-09-30", "SERV", "exit", sh=0.0, delta=-8.0, mv=None),
    ]
    realized = [
        {"period_end": "2024-06-30", "investee_ticker": "A", "shares_sold": 4.0,
         "cost_method": "avg", "cost_basis_status": "partial", "realized_pnl_est": 1.0},
        {"period_end": "2024-06-30", "investee_ticker": "A", "shares_sold": 4.0,
         "cost_method": "fifo", "cost_basis_status": "exact", "realized_pnl_est": 1.0},
        {"period_end": "2024-09-30", "investee_ticker": "SERV", "shares_sold": 8.0,
         "cost_method": "fifo", "cost_basis_status": "unknown", "realized_pnl_est": None},
    ]
    _write_export(tmp_path, "uber", rows, realized_rows=realized)
    data = jd.build_digest_data(tmp_path, "uber")
    by_key = {(s["period_end"], s["ticker"]): s["realized"] for s in data["sells"]}
    assert by_key == {
        ("2024-06-30", "A"): "avg:partial;fifo:exact",
        ("2024-06-30", "SERV"): "MISSING",
        ("2024-09-30", "SERV"): "fifo:unknown (no avg row)",
    }
    text = jd.render_digest(data)
    assert '### Sells / exits (3 rows; realized coverage: {"covered": 2, "MISSING": 1})' in text
    assert "- 2024-06-30 SERV sell Δsh=-2.0 realized=MISSING" in text


def test_sells_without_realized_csv_are_UNKNOWN_not_covered(tmp_path):
    rows = [_row("2024-03-31", "A"), _row("2024-06-30", "A", "exit", sh=0.0, delta=-10.0, mv=None)]
    _write_export(tmp_path, "uber", rows)
    data = jd.build_digest_data(tmp_path, "uber")
    assert data["sells"][0]["realized"].startswith("UNKNOWN")
    assert '"UNKNOWN": 1' in jd.render_digest(data)


def _uber_shaped(out: Path) -> None:
    """9 tickers × 31 quarters, 115 history rows, 31 returns, 31 chart rows."""
    periods = _quarters(2018, 31)
    spans = {"DIDIY": (0, 31), "GRAB": (0, 31), "AUR": (9, 31), "SERV": (19, 31),
             "JOBY": (24, 31), "MQ": (26, 31), "LCID": (27, 31), "WRD": (29, 31), "RIVN": (30, 31)}
    rows, realized = [], []
    for t, (a, b) in spans.items():
        for i in range(a, b):
            action = "new" if i == a else "hold"
            note = "source=10q_investments_table as_of=%s fv_usd=1900000000" % periods[i] if t in {"DIDIY", "GRAB"} else "source=sec_api_13f"
            rows.append(_row(periods[i], t, action, mv=1.9e9 + i * 1e6, note=note))
    for pe, t in (("2025-03-31", "SERV"), ("2025-06-30", "JOBY"), ("2025-09-30", "JOBY"),
                  ("2025-09-30", "SERV"), ("2026-06-30", "AUR")):
        r = next(x for x in rows if x["period_end"] == pe and x["investee_ticker"] == t)
        r.update(action="sell", shares_delta=-3.0)
        for m, st in (("avg", "estimated"), ("fifo", "estimated")):
            realized.append({"period_end": pe, "investee_ticker": t, "shares_sold": 3.0,
                             "cost_method": m, "cost_basis_status": st, "realized_pnl_est": 1.0})
    assert len(rows) == 115
    returns = [{"period_end": pe, "portfolio_mv_end": 1e10 + i * 1e8,
                "net_external_flow": 0.0 if i % 7 else 2.5e8,
                "dietz_return": None if i == 0 else (i % 5 - 2) / 10} for i, pe in enumerate(periods)]
    chart = pd.DataFrame({"period_end": periods, **{t: [1.0] * 31 for t in spans}})
    positions = [{"investee_ticker": t, "market_value_usd": 1e9} for t in spans]
    _write_export(out, "uber", rows, realized_rows=realized, returns_rows=returns,
                  chart=chart, positions=positions)


def test_uber_shaped_export_digest_is_under_8kb_with_nothing_truncated(tmp_path):
    _uber_shaped(tmp_path)
    data = jd.build_digest_data(tmp_path, "uber")
    text = jd.render_digest(data, max_chars=8000)
    assert len(text.encode("utf-8")) < 8000, len(text)
    assert "truncated:" not in text
    assert "### Coverage grid (9 tickers × 31 periods)" in text
    assert "### Sells / exits (5 rows" in text
    assert "### Returns by period (31 periods" in text
    assert _coverage_line(text, "RIVN").startswith("- RIVN: " + "-" * 30 + "$  rows=1")
    assert data["files"] == {
        "history": {"present": True, "rows": 115},
        "positions": {"present": True, "rows": 9},
        "realized": {"present": True, "rows": 10},
        "returns": {"present": True, "rows": 31},
        "chart": {"present": True, "rows": 31},
    }


def test_size_bound_trims_lists_with_truncated_counts_but_keeps_aggregates(tmp_path):
    periods = _quarters(2010, 60)
    rows = [_row(periods[0], "A")] + [
        _row(pe, "A", "sell", delta=-1.0) for pe in periods[1:]
    ]
    returns = [{"period_end": pe, "portfolio_mv_end": 1e9, "net_external_flow": 0.0,
                "dietz_return": 0.01} for pe in periods]
    _write_export(tmp_path, "baba", rows, realized_rows=[], returns_rows=returns)
    data = jd.build_digest_data(tmp_path, "baba")
    text = jd.render_digest(data, max_chars=2500)
    assert len(text) <= 2500 + 400, len(text)
    assert "### Sells / exits (59 rows" in text
    assert '{"MISSING": 59}' in text
    assert "### Returns by period (60 periods" in text
    lines = [l for l in text.splitlines() if l.startswith("truncated:")]
    assert any("sell rows not listed" in l and "of 59" in l for l in lines), lines
    assert any("return rows not listed" in l and "of 60" in l for l in lines), lines
    assert _coverage_line(text, "A").startswith("- A: " + "$" * 60 + "  rows=60")


def test_since_last_digest_reports_row_period_sell_and_check_deltas(tmp_path):
    rows = [_row("2024-03-31", "A"), _row("2024-06-30", "A", "sell", delta=-4.0)]
    _write_export(tmp_path, "baba", rows, realized_rows=[])
    text1, data1, jp = jd.write_digest(
        tmp_path, "baba", mechanical={"verdict": "fail", "checks": {"sell_without_realized_row": "fail"}}
    )
    assert "(no previous digest in this directory — first iteration)" in text1
    assert jp.is_file() and data1["since_last_digest"] == {"previous": None}

    rows2 = rows + [_row("2024-09-30", "A", mv=None), _row("2024-09-30", "NEW")]
    realized = [{"period_end": "2024-06-30", "investee_ticker": "A", "shares_sold": 4.0,
                 "cost_method": "avg", "cost_basis_status": "estimated", "realized_pnl_est": 1.0}]
    _write_export(tmp_path, "baba", rows2, realized_rows=realized)
    text2, data2, _ = jd.write_digest(
        tmp_path, "baba", mechanical={"verdict": "pass", "checks": {"sell_without_realized_row": "pass"}}
    )
    since = data2["since_last_digest"]
    assert since["previous"] == data1["content_hash"] and since["unchanged"] is False
    assert since["rows"] == {"history": [2, 4], "realized": [0, 1]}
    assert since["tickers_added"] == ["NEW"] and since["periods_added"] == ["2024-09-30"]
    assert since["coverage_changes"] == ["A@2024-09-30: -->s"]
    assert since["sells_changed"] == ["2024-06-30/A: MISSING -> avg:estimated"]
    assert since["checks_changed"] == ["sell_without_realized_row: fail->pass"]
    assert "### since_last_digest" in text2
    assert "row counts: history 2->4, realized 0->1" in text2
    assert "- A@2024-09-30: -->s" in text2
    assert "- 2024-06-30/A: MISSING -> avg:estimated" in text2

    text3, data3, _ = jd.write_digest(tmp_path, "baba", mechanical={"verdict": "pass", "checks": {"sell_without_realized_row": "pass"}})
    assert data3["since_last_digest"]["unchanged"] is True
    assert "export unchanged since previous digest" in text3


def test_content_hash_changes_with_any_export_csv(tmp_path):
    _write_export(tmp_path, "baba", [_row("2024-03-31", "A")], realized_rows=[])
    h1 = jd.export_content_hash(tmp_path, "baba")
    assert h1 == jd.export_content_hash(tmp_path, "baba")
    (tmp_path / "baba_realized_pnl_qoq.csv").write_text("period_end,investee_ticker\n2024-03-31,A\n")
    assert jd.export_content_hash(tmp_path, "baba") != h1


def test_full_judge_skip_reason_hash_match_force_and_mechanical(tmp_path):
    _write_export(tmp_path, "baba", [_row("2024-03-31", "A")], realized_rows=[])
    h = jd.export_content_hash(tmp_path, "baba")
    assert jd.full_judge_skip_reason(tmp_path, "baba", judge_mode="full", force=False) is None
    jd.record_last_judged(tmp_path, "baba", content_hash=h, judges=["fable", "codex"])
    reason = jd.full_judge_skip_reason(tmp_path, "baba", judge_mode="full", force=False)
    assert reason and h[:12] in reason and "fable,codex" in reason and "--force" in reason
    assert jd.full_judge_skip_reason(tmp_path, "baba", judge_mode="full", force=True) is None
    mech = jd.full_judge_skip_reason(tmp_path, "baba", judge_mode="mechanical", force=False)
    assert mech and mech.startswith("judge-mode=mechanical")
    (tmp_path / "baba_equity_holdings_history.csv").write_text(
        pd.DataFrame([_row("2024-03-31", "A"), _row("2024-06-30", "A")], columns=HIST_COLS).to_csv(index=False)
    )
    assert jd.full_judge_skip_reason(tmp_path, "baba", judge_mode="full", force=False) is None
    assert jd.read_last_judged(tmp_path, "baba", scope="swarm") is None


def _fake_runner(calls: list):
    def run(name, packet_text, schema):
        calls.append((name, packet_text))
        return {"judge": name, "verdict": "pass", "score": 100, "blocking_issues": [],
                "minor_issues": [], "what_looks_good": ["ok"], "checks": {"x": "pass"},
                "summary": f"{name} ok"}
    return run


def test_run_grade_mechanical_default_never_calls_llm_and_writes_digest(grade, tmp_path):
    _uber_shaped(tmp_path)
    calls: list = []
    code, info = grade.run_grade(
        ticker="UBER", out_dir=tmp_path, judges=["fable", "codex"], llm_runner=_fake_runner(calls)
    )
    assert code == 0 and calls == []
    assert info["skip_reason"].startswith("judge-mode=mechanical")
    assert (tmp_path / "uber_judge_digest.json").is_file()
    assert (tmp_path / "uber_judge_digest.md").is_file()
    assert not (tmp_path / "uber_last_judged.json").exists()
    board = (tmp_path / "uber_grade_board.md").read_text()
    assert "| mechanical |" in board and "| fable |" not in board
    packet = (tmp_path / "uber_grade_packet.md").read_text()
    assert "## Export digest (complete" in packet
    assert "### Coverage grid (9 tickers × 31 periods)" in packet
    assert "raw head-slice attachment" not in packet
    assert "source=sec_api_13f" not in packet


def test_run_grade_full_judges_once_per_export_hash_then_skips_unless_forced(grade, tmp_path, capsys):
    _uber_shaped(tmp_path)
    calls: list = []
    runner = _fake_runner(calls)
    code, info = grade.run_grade(
        ticker="UBER", out_dir=tmp_path, judges=["fable", "codex"], judge_mode="full", llm_runner=runner
    )
    assert code == 0 and sorted(c[0] for c in calls) == ["codex", "fable"]
    assert "### Coverage grid (9 tickers × 31 periods)" in calls[0][1]
    last = json.loads((tmp_path / "uber_last_judged.json").read_text())
    assert last["content_hash"] == info["content_hash"] == jd.export_content_hash(tmp_path, "uber")
    assert "| fable |" in (tmp_path / "uber_grade_board.md").read_text()

    calls.clear()
    code2, info2 = grade.run_grade(
        ticker="UBER", out_dir=tmp_path, judges=["fable", "codex"], judge_mode="full", llm_runner=runner
    )
    assert code2 == grade.EXIT_FULL_SKIPPED and calls == []
    assert "already judged" in info2["skip_reason"] and "--force" in info2["skip_reason"]
    assert f"LLM judges skipped: {info2['skip_reason']}" in capsys.readouterr().err

    code3, info3 = grade.run_grade(
        ticker="UBER", out_dir=tmp_path, judges=["fable"], judge_mode="full", force=True, llm_runner=runner
    )
    assert code3 == 0 and [c[0] for c in calls] == ["fable"] and info3["skip_reason"] is None

    calls.clear()
    (tmp_path / "uber_holdings_qoq_chart.csv").write_text("period_end,AUR\n2026-06-30,1.0\n")
    code4, _ = grade.run_grade(
        ticker="UBER", out_dir=tmp_path, judges=["fable"], judge_mode="full", llm_runner=runner
    )
    assert code4 == 0 and [c[0] for c in calls] == ["fable"]


def test_build_packet_attach_csv_flag_appends_raw_slices(grade, tmp_path):
    _uber_shaped(tmp_path)
    p = grade.build_packet(ticker="UBER", sheet_url=None, out_dir=tmp_path, attach_csv=True)
    text = p.read_text()
    assert "## Export digest (complete" in text
    assert "## equity_holdings_history.csv (raw head-slice attachment)" in text
    assert "## realized_pnl_qoq.csv (raw head-slice attachment)" in text
