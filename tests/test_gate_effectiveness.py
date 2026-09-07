"""Gate effectiveness: each reflect gate FIRES on its original defect through the
real pipeline path and STAYS SILENT on a clean export.

A synthetic parent (TESTCO) is driven through the real functions, never mocks of
the gates: ``build_holdings_history`` with the real note collector (fixture
10-Q/10-K HTML flattened by the real ``get_filing_text``) and the real 13D/G
collector (fixture HTML through the real cover-page parser; only HTTP is
patched), then ``write_csvs`` / ``performance_frames``, the real reconcile CLI
against fixture documents, and ``scripts/grade_holdings_sheet.py``'s mechanical
judge + ``write_board``.

One ``Defect`` per original defect. Each is derived from the clean scenario by a
single mutation and asserts both halves: ``fires`` (the named gate/precheck id
fires and the verdict is not PASS) and ``silent`` (the clean scenario does not
trip that gate). ``test_gate_fires_and_stays_silent[<defect id>]`` is one gate's
effectiveness. ``test_every_gate_has_an_effectiveness_test`` fails CI when a
precheck id or an ``assert_*`` export gains no entry here.

Fixture tables: ``NOTE_FILINGS`` is (form, filing_date, accession, primary
document) newest first as EDGAR lists them; ``OFFGRID_10Q`` is a 10-Q whose
June-2025 column carries $ with no 13F period; ``G13_ITEMS`` is (filing_date,
form, accession, primary document) as ``_list_13g_items`` returns.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from unittest.mock import patch

import pandas as pd
import pytest

from helpers import as_production_text
from hidden_stock.quirks.holdings import history as history_mod
from hidden_stock.quirks.holdings import performance as perf_mod
from hidden_stock.quirks.holdings import sec_13g
from hidden_stock.quirks.holdings.export import write_csvs
from hidden_stock.quirks.holdings.history import HISTORY_COLUMNS, build_holdings_history
from hidden_stock.quirks.holdings.schema import HOLDINGS_COLUMNS

_ROOT = Path(__file__).resolve().parents[1]
FIX = Path(__file__).resolve().parent / "fixtures" / "gate_effectiveness"

PARENT = "TESTCO"
SLUG = "testco"
CIK = "0009999999"
AS_OF = "2025-09-06"
GRID = ["2023-12-31", "2024-03-31", "2024-06-30", "2024-09-30", "2024-12-31", "2025-03-31"]

NOTE_FILINGS = [
    ("10-Q", "2025-05-08", "0009999999-25-000002", "testco_10q_2025q1.htm"),
    ("10-K", "2025-02-10", "0009999999-25-000001", "testco_10k_2024.htm"),
    ("10-Q", "2024-11-06", "0009999999-24-000003", "testco_10q_2024q3.htm"),
    ("10-Q", "2024-08-06", "0009999999-24-000002", "testco_10q_2024q2.htm"),
    ("10-Q", "2024-05-08", "0009999999-24-000001", "testco_10q_2024q1.htm"),
]
OFFGRID_10Q = ("10-Q", "2025-08-06", "0009999999-25-000003", "testco_10q_2025q2_offgrid.htm")

G13_ITEMS = [
    ("2025-01-21", "SC 13D/A", "0009999999-25-000101", "serv_13da.htm"),
    ("2024-05-08", "SC 13D", "0009999999-24-000101", "serv_13d.htm"),
]

INFOTABLES = {
    "0009999999-24-000011": "testco_13f_2024q1.xml",
    "0009999999-24-000012": "testco_13f_2024q2.xml",
    "0009999999-24-000013": "testco_13f_2024q3.xml",
    "0009999999-24-000014": "testco_13f_2024q4.xml",
    "0009999999-25-000011": "testco_13f_2025q1.xml",
}


def _f13(pe: str, fd: str, acc: str, grab: tuple[float, float], aur: tuple[float, float] | None):
    rows = [
        {
            "investee_name": "GRAB HOLDINGS LTD",
            "investee_ticker": "GRAB",
            "shares_held": grab[0],
            "market_value_usd": grab[1],
            "_cusip": "G4124C109",
            "note": "source=sec_api_13f",
        }
    ]
    if aur is not None:
        rows.append(
            {
                "investee_name": "AURORA INNOVATION INC",
                "investee_ticker": "AUR",
                "shares_held": aur[0],
                "market_value_usd": aur[1],
                "_cusip": "05177A100",
                "note": "source=sec_api_13f",
            }
        )
    return (pe, fd, acc, rows)


F13_PERIODS = [
    _f13("2024-03-31", "2024-05-15", "0009999999-24-000011", (600e6, 2.1e9), None),
    _f13("2024-06-30", "2024-08-14", "0009999999-24-000012", (600e6, 2.2e9), (500e6, 1.0e9)),
    _f13("2024-09-30", "2024-11-14", "0009999999-24-000013", (600e6, 2.3e9), (500e6, 1.1e9)),
    _f13("2024-12-31", "2025-02-14", "0009999999-24-000014", (400e6, 1.6e9), (500e6, 1.0e9)),
    _f13("2025-03-31", "2025-05-15", "0009999999-25-000011", (400e6, 1.7e9), None),
]


def _fixture(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


class FakeEdgar:
    """Offline EDGAR with the surface both the history collectors and the
    reconcile ``DocumentFetcher`` use. ``docs`` maps primary-document name to
    text so a defect can swap one filing body."""

    user_agent = "tests"

    def __init__(self, *, note_filings=None, g13_items=None, docs=None):
        self.note_filings = list(NOTE_FILINGS if note_filings is None else note_filings)
        self.g13_items = list(G13_ITEMS if g13_items is None else g13_items)
        self.docs = dict(docs or {})
        self.calls: list[tuple] = []

    def _doc(self, name: str) -> str:
        return self.docs.get(name) or _fixture(name)

    def get_cik(self, ticker: str) -> str:
        return CIK

    def list_filings(self, cik, form_types=(), as_of=None, limit=None):
        out = [
            {"form": f, "filing_date": d, "accession_no": a, "primary_document": p}
            for f, d, a, p in self.note_filings
            if f in set(form_types)
        ]
        return out[: limit or len(out)]

    def fetch_filing_document(self, cik, accession_no, name):
        self.calls.append(("fetch", accession_no, name))
        return self._doc(name)

    def get_filing_text(self, html_text: str, max_chars: int = 180000) -> str:
        return as_production_text(html_text, max_chars=max_chars)

    def _primary_for(self, accession_no: str) -> str | None:
        for _f, _d, acc, primary in self.note_filings + [
            (f, d, a, p) for d, f, a, p in self.g13_items
        ]:
            if acc == accession_no:
                return primary
        return None

    def list_filing_documents(self, cik, accession_no):
        self.calls.append(("list", accession_no))
        primary = self._primary_for(accession_no)
        if primary is None:
            raise RuntimeError(f"404 index.json for {accession_no}")
        return [{"name": primary, "size": len(self._doc(primary))}]

    def fetch_13f_infotable(self, cik, accession_no):
        self.calls.append(("infotable", accession_no))
        name = INFOTABLES.get(accession_no)
        return _fixture(name) if name else None


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, _ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def grade():
    return _load_script("grade_holdings_sheet")


@pytest.fixture(scope="module")
def reconcile_cli():
    return _load_script("reconcile_holdings")


def build_history(edgar: FakeEdgar, *, f13=None) -> tuple[list[dict], dict]:
    """Real ``build_holdings_history``: only the 13F API and 13D/G HTTP are patched."""
    periods = F13_PERIODS if f13 is None else f13
    with (
        patch.object(
            history_mod, "_collect_13f_periods",
            return_value=(list(periods), {"error": None, "num_periods": len(periods)}),
        ),
        patch.object(sec_13g, "_list_13g_items", return_value=list(edgar.g13_items)),
        patch.object(
            sec_13g, "fetch_filing_text",
            side_effect=lambda _s, _c, _acc, primary: (edgar._doc(primary), "html"),
        ),
        patch.object(sec_13g.time, "sleep", return_value=None),
        patch.object(history_mod.time, "sleep", return_value=None),
    ):
        return build_holdings_history(
            parent_ticker=PARENT, edgar=edgar, as_of=AS_OF, max_filings=10, lookback_years=0
        )


def export_rows(rows: list[dict], out_dir: Path) -> dict[str, Path]:
    hold = pd.DataFrame(columns=HOLDINGS_COLUMNS)
    hist = pd.DataFrame(rows, columns=HISTORY_COLUMNS)
    return write_csvs(PARENT, hold, hist, out_dir)


def run_reconcile(cli, out_dir: Path, edgar: FakeEdgar) -> tuple[list[dict], int]:
    return cli.run(
        parent=PARENT,
        history_csv=out_dir / f"{SLUG}_equity_holdings_history.csv",
        out_csv=out_dir / f"{SLUG}_reconcile.csv",
        edgar=edgar,
        cache_dir=out_dir / ".cache",
        anchors_dir=out_dir,
        quiet=True,
    )


def run_grade(grade, out_dir: Path, **kw) -> tuple[int, dict, dict, str]:
    code, info = grade.run_grade(ticker=PARENT, out_dir=out_dir, judges=["mechanical"], **kw)
    mech = json.loads((out_dir / f"{SLUG}_grade_mechanical.json").read_text(encoding="utf-8"))
    return code, info, mech, Path(info["board"]).read_text(encoding="utf-8")


def precheck(grade, out_dir: Path, *, parent: str = PARENT) -> dict:
    return grade.mechanical_precheck(
        out_dir / f"{SLUG}_equity_holdings_history.csv",
        out_dir / f"{SLUG}_portfolio_by_period.csv",
        parent=parent,
    )


@dataclass
class Scenario:
    edgar: FakeEdgar
    rows: list[dict]
    meta: dict
    out_dir: Path
    paths: dict[str, Path]
    reconcile: list[dict]
    reconcile_code: int
    grade_code: int
    info: dict
    mech: dict
    board: str

    def csv(self, key: str) -> pd.DataFrame:
        return pd.read_csv(self.paths[key])

    def records(self, key: str) -> list[dict]:
        return self.csv(key).to_dict(orient="records")


@pytest.fixture(scope="module")
def clean(tmp_path_factory, grade, reconcile_cli) -> Scenario:
    out_dir = tmp_path_factory.mktemp("testco_clean")
    edgar = FakeEdgar()
    rows, meta = build_history(edgar)
    paths = export_rows(rows, out_dir)
    results, rcode = run_reconcile(reconcile_cli, out_dir, edgar)
    gcode, info, mech, board = run_grade(grade, out_dir)
    return Scenario(edgar, rows, meta, out_dir, paths, results, rcode, gcode, info, mech, board)


def copy_export(clean: Scenario, dst: Path) -> Path:
    shutil.copytree(clean.out_dir, dst, dirs_exist_ok=True)
    return dst


def _edit_csv(path: Path, fn: Callable[[pd.DataFrame], pd.DataFrame]) -> None:
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    fn(df).to_csv(path, index=False)


def _at(df: pd.DataFrame, pe: str, ticker: str) -> pd.Series:
    return (df["period_end"].astype(str).str[:10] == pe) & (
        df["investee_ticker"].astype(str).str.upper() == ticker
    )


def _issue_ids(res: dict) -> set[str]:
    return {i["id"] for i in res["blocking_issues"]} | {i["id"] for i in res["minor_issues"]}


def _by(rows: list[dict], pe: str, ticker: str) -> dict:
    hits = [r for r in rows if str(r.get("period_end"))[:10] == pe and r.get("investee_ticker") == ticker]
    assert len(hits) == 1, (pe, ticker, hits)
    return hits[0]


@dataclass
class Ctx:
    clean: Scenario
    grade: object
    reconcile_cli: object
    tmp_path: Path
    monkeypatch: pytest.MonkeyPatch
    caplog: pytest.LogCaptureFixture

    def export_copy(self, name: str = "defect") -> Path:
        return copy_export(self.clean, self.tmp_path / name)


@dataclass
class Defect:
    id: str
    gates: tuple[str, ...]
    fires: Callable[[Ctx], None]
    silent: Callable[[Ctx], None]
    mutation: str = field(default="")


DEFECTS: list[Defect] = []


def defect(id: str, gates: tuple[str, ...], mutation: str):
    """Register ``(fires, silent)`` halves as one Defect."""

    def wrap(pair):
        fires, silent = pair()
        DEFECTS.append(Defect(id, gates, fires, silent, mutation))
        return pair

    return wrap


def test_clean_scenario_is_pass_with_every_check_resolved(clean: Scenario):
    assert clean.meta["grid"] == "13f"
    assert clean.meta["num_note_snapshots"] == 5 and clean.meta["num_13g_filings"] == 2
    assert sorted({r["period_end"] for r in clean.rows}) == GRID
    assert clean.reconcile_code == 0 and clean.grade_code == 0
    assert clean.mech["verdict"] == "pass", clean.mech
    unresolved = {k: v for k, v in clean.mech["checks"].items() if v not in {"pass", "n/a"}}
    assert unresolved == {}, f"checks not resolved on the clean export: {unresolved}"
    assert "unknown" not in clean.mech["checks"].values()
    assert "BOARD: PASS" in clean.board
    assert clean.mech["checks"]["unreconciled_rows"] == "pass"

