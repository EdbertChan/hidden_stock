"""swarm_verify.merge_board: an unevaluated check can never make a PASS board."""

from __future__ import annotations

from types import SimpleNamespace

from hidden_stock.quirks.holdings.swarm_verify import merge_board, stage_report_to_mechanical


def _judge(name, verdict="pass", checks=None):
    return {
        "judge": name, "verdict": verdict, "score": 100, "blocking_issues": [],
        "minor_issues": [], "what_looks_good": [], "checks": checks or {},
        "summary": name,
    }


def test_merge_board_unknown_check_downgrades_pass_to_needs_work():
    mech = _judge("mechanical", checks={"row_count": "pass"})
    fable = _judge("fable", checks={"unit_sanity": "unknown", "names": "pass"})
    board = merge_board(mech, [fable], stage="broker_pdf_parse")
    assert board["board"] == "NEEDS_WORK"
    assert board["ok"] is False
    assert board["unknown_checks"] == ["fable:unit_sanity"]


def test_merge_board_all_resolved_is_pass():
    mech = _judge("mechanical", checks={"row_count": "pass"})
    fable = _judge("fable", checks={"unit_sanity": "n/a", "names": "pass"})
    board = merge_board(mech, [fable], stage="broker_pdf_parse")
    assert board["board"] == "PASS"
    assert board["unknown_checks"] == []


def test_stage_report_warn_is_warn_not_unknown():
    """A warn finding was evaluated; it must not read as an unevaluated check."""
    report = SimpleNamespace(
        ok=True,
        findings=[
            SimpleNamespace(severity="warn", check_id="fx_stale", message="fx 3d old", evidence={}),
            SimpleNamespace(severity="info", check_id="rows", message="21 rows", evidence={}),
        ],
    )
    mech = stage_report_to_mechanical(report, stage="broker_pdf_parse")
    assert mech["checks"] == {"fx_stale": "warn", "rows": "pass"}
    assert merge_board(mech, [], stage="broker_pdf_parse")["board"] == "PASS"
