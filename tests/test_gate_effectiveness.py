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
from hidden_stock.quirks.holdings import validate as validate_mod
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
SELF_13D = ("2024-07-15", "SC 13D", "0009999999-24-000102", "legacy_widgets_13d.htm")
PARENT_NAMES = ["TESTCO HOLDINGS INC", "LEGACY WIDGETS CORP"]

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

    def get_company_names(self, cik: str) -> list[str]:
        assert cik == CIK
        return list(PARENT_NAMES)

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


@defect(
    "sell_no_cost_lot_13g_only",
    ("sell_without_realized_row", "assert_sell_realized_coverage"),
    "drop the SERV 2025-03-31 rows from realized_pnl_qoq.csv",
)
def _sell_no_cost_lot():
    def fires(ctx: Ctx):
        realized = ctx.clean.records("realized_pnl_qoq")
        serv = [r for r in realized if r["investee_ticker"] == "SERV"]
        assert [(r["period_end"], r["cost_method"], r["cost_basis_status"]) for r in serv] == [
            ("2025-03-31", "avg", "unknown")
        ]
        assert "13g_only_no_dollars" in serv[0]["cost_basis_note"]
        out = ctx.export_copy()
        _edit_csv(
            out / f"{SLUG}_realized_pnl_qoq.csv",
            lambda df: df[df["investee_ticker"] != "SERV"],
        )
        res = precheck(ctx.grade, out)
        assert res["checks"]["sell_without_realized_row"] == "fail"
        assert "sell_without_realized_row" in _issue_ids(res)
        assert "2025-03-31/SERV" in str(
            next(i for i in res["blocking_issues"] if i["id"] == "sell_without_realized_row")["evidence"]
        )
        assert res["verdict"] == "fail"
        assert "BOARD: FAIL" in run_grade(ctx.grade, out)[3]
        without = [r for r in realized if r["investee_ticker"] != "SERV"]
        with pytest.raises(AssertionError, match=r"sell_realized_coverage.*2025-03-31/SERV"):
            perf_mod.assert_sell_realized_coverage(ctx.clean.rows, without)

    def silent(ctx: Ctx):
        assert ctx.clean.mech["checks"]["sell_without_realized_row"] == "pass"
        assert perf_mod.assert_sell_realized_coverage(
            ctx.clean.rows, ctx.clean.records("realized_pnl_qoq")
        ) is None

    return fires, silent


@defect(
    "note_column_date_off_grid",
    ("assert_note_dates_in_period_grid", "period_grid_gap"),
    "add a 10-Q whose 2025-06-30 column has $ but no 13F period; drop 2024-09-30 from the CSV",
)
def _note_column_off_grid():
    def fires(ctx: Ctx):
        edgar = FakeEdgar(note_filings=[OFFGRID_10Q] + NOTE_FILINGS)
        with pytest.raises(ValueError, match=r"period_grid.*2025-06-30"):
            build_history(edgar)
        out = ctx.export_copy()
        _edit_csv(
            out / f"{SLUG}_equity_holdings_history.csv",
            lambda df: df[df["period_end"].str[:10] != "2024-09-30"],
        )
        res = precheck(ctx.grade, out)
        assert res["checks"]["period_grid_gap"] == "fail"
        gap = next(i for i in res["minor_issues"] if i["id"] == "period_grid_gap")
        assert gap["evidence"] == ["2024-09-30"]
        assert res["verdict"] == "needs_work"
        assert "BOARD: NEEDS_WORK" in run_grade(ctx.grade, out)[3]

    def silent(ctx: Ctx):
        assert ctx.clean.meta["num_note_grid_periods"] == 1
        assert ctx.clean.mech["checks"]["period_grid_gap"] == "pass"
        history_mod.assert_note_dates_in_period_grid(
            [], GRID, lookback_start="2000-01-01", upper=AS_OF
        )

    return fires, silent


@defect(
    "tenk_flattened_earlier_header",
    ("parse_investments_table.header_scan",),
    "10-K production text whose first two-date header is the balance sheet, not the Investments table",
)
def _tenk_header_scan():
    from hidden_stock.quirks.holdings.parse_notes import (
        _AS_OF_DATES_RE,
        _AS_OF_YEAR_PAIR_RE,
        _INVESTMENTS_ROW_RE,
        parse_investments_table,
    )

    def _prose(text: str) -> str:
        return re.sub(r"\s+", " ", text)

    def fires(ctx: Ctx):
        text = as_production_text(_fixture("testco_10k_2024.htm"))
        prose = _prose(text)
        first = min(
            [m.start() for m in _AS_OF_DATES_RE.finditer(prose)]
            + [m.start() for m in _AS_OF_YEAR_PAIR_RE.finditer(prose)]
        )
        assert "Cash and cash equivalents" in prose[first : first + 200]
        assert _INVESTMENTS_ROW_RE.search(prose[first : first + 4000]) is None
        assert parse_investments_table(prose[first : first + 4000], parent_ticker=PARENT, form="10-K") == []
        rows = parse_investments_table(text, parent_ticker=PARENT, form="10-K", filing_date="2025-02-10")
        fv = {(r["investee_ticker"], r["as_of_date"]): r["fair_value_disclosed_usd"] for r in rows}
        assert fv[("DIDIY", "2024-12-31")] == 2.1e9 and fv[("DHER.DE", "2024-12-31")] == 5.2e8
        assert fv[("DIDIY", "2023-12-31")] == 1.9e9

    def silent(ctx: Ctx):
        didi = _by(ctx.clean.rows, "2024-12-31", "DIDIY")
        assert didi["market_value_usd"] == 2.1e9
        assert didi["accession_no"] == "0009999999-25-000001"
        assert "10k_investments_table" in didi["note"]

    return fires, silent


NARRATIVE = (
    "Balances As of December 31, 2017 June 30, 2018 Restricted cash 1,000 1,200 "
    "During the period we recognized an unrealized gain on our investment in "
    "Didi, $501 million, and other gains of $20 million."
)


@defect(
    "narrative_comma_amount",
    ("parse_investments_table.digit_anchor",),
    'narrative "Didi, $501 million" after a two-date header (no table row)',
)
def _narrative_comma_amount():
    from hidden_stock.quirks.holdings.parse_notes import _INVESTMENTS_ROW_RE, parse_investments_table

    def fires(ctx: Ctx):
        old = re.compile(_INVESTMENTS_ROW_RE.pattern.replace(r"\d[\d,]*", r"[\d,]+"), re.IGNORECASE)
        assert old.pattern != _INVESTMENTS_ROW_RE.pattern
        m = old.search(NARRATIVE)
        assert m and m.group("a") == "," and m.group("b") == "501"
        assert _INVESTMENTS_ROW_RE.search(NARRATIVE) is None
        assert parse_investments_table(NARRATIVE, parent_ticker=PARENT, form="10-K") == []

    def silent(ctx: Ctx):
        tenk = as_production_text(_fixture("testco_10k_2024.htm"))
        assert "Didi, $501 million" in tenk
        rows = parse_investments_table(tenk, parent_ticker=PARENT, form="10-K")
        assert 5.01e8 not in {r["fair_value_disclosed_usd"] for r in rows}
        didi = sorted(r["market_value_usd"] for r in ctx.clean.rows if r["investee_ticker"] == "DIDIY")
        assert didi == [1.9e9, 1.95e9, 2.0e9, 2.05e9, 2.1e9, 2.15e9]

    return fires, silent


@defect(
    "peer_comps_table_612pct",
    ("assert_pct_domain", "parse_cmbigm_strategic_investments"),
    "CMBI peer-comp table (price/mcap/PE columns); SERV 13D/A restated at 612%",
)
def _peer_comps_612():
    from hidden_stock.quirks.holdings.broker_sotp import parse_cmbigm_strategic_investments
    from hidden_stock.quirks.holdings.identity import assert_pct_domain

    def fires(ctx: Ctx):
        assert parse_cmbigm_strategic_investments(_fixture("cmbigm_peer_comps.txt")) == []
        with pytest.raises(ValueError, match=r"ownership_pct=612\.0 outside \(0, 100\]"):
            assert_pct_domain(612.0, field="ownership_pct", context="peer_comps")
        bad = _fixture("serv_13da.htm").replace("13.1%", "612.0%")
        edgar = FakeEdgar(docs={"serv_13da.htm": bad})
        with ctx.caplog.at_level(logging.WARNING):
            rows, _meta = build_history(edgar)
        assert any("ownership_pct=612.0" in r.getMessage() for r in ctx.caplog.records)
        serv = _by(rows, "2025-03-31", "SERV")
        assert (serv["action"], serv["shares_held"]) == ("hold", 5_298_833.0)

    def silent(ctx: Ctx):
        real = parse_cmbigm_strategic_investments(
            (_ROOT / "tests" / "fixtures" / "broker_sotp" / "cmbigm_strategic_investments_snippet.txt").read_text(encoding="utf-8")
        )
        assert real and all(assert_pct_domain(r["ownership_pct"], field="ownership_pct", context="t") for r in real)
        serv = _by(ctx.clean.rows, "2025-03-31", "SERV")
        assert (serv["action"], serv["shares_delta"], serv["ownership_pct"]) == ("sell", -550_000.0, 13.1)

    return fires, silent


@defect(
    "dollar_only_name_flow_and_dietz",
    ("dietz_sane",),
    "dietz_return 19.52 (1,952%) written into returns_by_period.csv at 2024-12-31",
)
def _dietz_sane():
    def fires(ctx: Ctx):
        out = ctx.export_copy()

        def inject(df):
            df.loc[df["period_end"].str[:10] == "2024-12-31", "dietz_return"] = "19.52"
            return df

        _edit_csv(out / f"{SLUG}_returns_by_period.csv", inject)
        res = precheck(ctx.grade, out)
        assert res["checks"]["dietz_sane"] == "fail"
        d = next(i for i in res["minor_issues"] if i["id"] == "dietz_sane")
        assert d["evidence"] == ["2024-12-31: 1952.0%"]
        assert res["verdict"] == "needs_work"
        assert "BOARD: NEEDS_WORK" in run_grade(ctx.grade, out)[3]

    def silent(ctx: Ctx):
        r = ctx.clean.csv("returns_by_period").set_index("period_end")
        assert abs(r.loc["2024-09-30", "net_external_flow"] - 5.0e8) < 1.0
        assert abs(r.loc["2024-09-30", "mtm_pnl"] - 2.5e8) < 1.0
        assert r["dietz_return"].dropna().abs().max() < 0.1
        assert ctx.clean.mech["checks"]["dietz_sane"] == "pass"

    return fires, silent


@defect(
    "self_issuer_13g_under_parent_cik",
    ("self_issuer_row", "no_self_issuer_row", "assert_no_self_issuer_rows"),
    "a third-party SC 13D about TESTCO captioned with its former name (Legacy Widgets Corp.), "
    "no trading symbol, listed under TESTCO's own CIK",
)
def _self_issuer_under_parent_cik():
    def fires(ctx: Ctx):
        edgar = FakeEdgar(g13_items=[G13_ITEMS[0], SELF_13D, G13_ITEMS[1]])
        rows, meta = build_history(edgar)
        assert meta["num_13g_filings"] == 3 and meta["num_13g_self_issuer_filings"] == 1
        assert meta["num_self_issuer_dropped"] == 0
        assert [r for r in rows if "widgets" in str(r.get("investee_name") or "").lower()] == []
        assert sorted({r["investee_ticker"] for r in rows}) == sorted({r["investee_ticker"] for r in ctx.clean.rows})

        parsed = sec_13g.parse_13g_html(_fixture(SELF_13D[3]))
        assert parsed.get("ticker") is None
        assert not sec_13g.is_self_issuer(parsed, parent_ticker=PARENT, parent_name_hints=[])
        assert sec_13g.is_self_issuer(parsed, parent_ticker=PARENT, parent_name_hints=PARENT_NAMES)

        leaked = dict(_by(ctx.clean.rows, "2024-06-30", "SERV"))
        leaked.update(investee_ticker=None, investee_name="Legacy Widgets Corp.", cusip="52468A104")
        with pytest.raises(AssertionError, match=r"self_issuer rows for TESTCO.*Legacy Widgets"):
            validate_mod.assert_no_self_issuer_rows(
                ctx.clean.rows + [leaked], PARENT, parent_name_hints=PARENT_NAMES, context="t"
            )
        kept, dropped = validate_mod.drop_self_issuer_rows(
            ctx.clean.rows + [leaked], PARENT, parent_name_hints=PARENT_NAMES
        )
        assert dropped == [leaked] and len(kept) == len(ctx.clean.rows)

        out = ctx.export_copy()
        hist_csv = out / f"{SLUG}_equity_holdings_history.csv"

        def add_self_row(df):
            row = df[_at(df, "2024-06-30", "SERV")].iloc[0].copy()
            row["investee_ticker"] = ""
            row["investee_name"] = "Legacy Widgets Corp."
            row["cusip"] = "52468A104"
            return pd.concat([df, row.to_frame().T], ignore_index=True)

        _edit_csv(hist_csv, add_self_row)
        res = ctx.grade.mechanical_precheck(
            hist_csv, out / f"{SLUG}_portfolio_by_period.csv", parent=PARENT, parent_name_hints=PARENT_NAMES
        )
        assert res["checks"]["no_self_issuer_row"] == "fail"
        hit = next(i for i in res["blocking_issues"] if i["id"] == "self_issuer_row")
        assert hit["evidence"][0]["investee_name"] == "Legacy Widgets Corp."
        assert res["verdict"] == "fail"
        res_cached = precheck(ctx.grade, out)
        assert res_cached["checks"]["no_self_issuer_row"] == "fail", "cached EDGAR names not used"
        assert "BOARD: FAIL" in run_grade(ctx.grade, out)[3]

        _edit_csv(
            hist_csv,
            lambda df: df.assign(
                investee_ticker=df["investee_ticker"].where(df["investee_name"] != "Legacy Widgets Corp.", PARENT)
            ),
        )
        res = ctx.grade.mechanical_precheck(
            hist_csv, out / f"{SLUG}_portfolio_by_period.csv", parent=PARENT, parent_name_hints=[]
        )
        assert "investee_ticker=TESTCO is the parent" in str(
            next(i for i in res["blocking_issues"] if i["id"] == "self_issuer_row")["evidence"]
        )

    def silent(ctx: Ctx):
        assert ctx.clean.meta["num_13g_self_issuer_filings"] == 0
        assert ctx.clean.meta["num_self_issuer_dropped"] == 0
        assert ctx.clean.mech["checks"]["no_self_issuer_row"] == "pass"
        assert "self_issuer_row" not in _issue_ids(ctx.clean.mech)
        assert validate_mod.assert_no_self_issuer_rows(
            ctx.clean.rows, PARENT, parent_name_hints=PARENT_NAMES
        ) is None
        assert sec_13g.parent_name_hints_for(PARENT) == PARENT_NAMES

    return fires, silent


@defect(
    "empty_export_unexplained",
    ("empty_export_unexplained", "empty_export_explained", "empty_export_note"),
    "a parent whose only 13D/G under its CIK is a third party's stake in the parent: "
    "zero rows after filtering, exported with no export_status note",
)
def _empty_export_unexplained():
    from hidden_stock.quirks.holdings import export as export_mod

    def fires(ctx: Ctx):
        edgar = FakeEdgar(g13_items=[SELF_13D], note_filings=[])
        rows, meta = build_history(edgar, f13=[])
        assert rows == [] and meta["num_13g_self_issuer_filings"] == 1
        note = export_mod.empty_export_note(PARENT, num_current=0, num_history=0, build_meta=meta)
        assert note.startswith(
            f"no named public equity stakes disclosed via 13F/13G/notes for {PARENT}; "
            "13G filings under the CIK were third-party filings about the parent: 1"
        )
        assert "13G filings scanned: 1" in note
        assert export_mod.empty_export_note(PARENT, num_current=0, num_history=0) is not None

        empty_hold = pd.DataFrame(columns=HOLDINGS_COLUMNS)
        empty_hist = pd.DataFrame(rows, columns=HISTORY_COLUMNS)
        out = ctx.tmp_path / "empty_silent"
        paths = write_csvs(PARENT, empty_hold, empty_hist, out)
        assert "export_status" not in paths
        res = precheck(ctx.grade, out)
        assert res["checks"]["empty_export_explained"] == "fail"
        assert "empty_export_unexplained" in _issue_ids(res)
        assert res["verdict"] == "fail"
        assert "BOARD: FAIL" in run_grade(ctx.grade, out)[3]

        explained = ctx.tmp_path / "empty_explained"
        paths = write_csvs(PARENT, empty_hold, empty_hist, explained, status_note=note, build_meta=meta)
        status = pd.read_csv(paths["export_status"])
        assert status["note"].iloc[0] == note and int(status["num_13g_self_issuer_filings"].iloc[0]) == 1
        res = precheck(ctx.grade, explained)
        assert res["checks"]["empty_export_explained"] == "pass"
        assert res["checks"]["no_self_issuer_row"] == "pass", "zero rows must resolve, not stay unknown"
        assert "empty_export_unexplained" not in _issue_ids(res)
        assert meta["num_self_issuer_dropped"] == 0
        assert "empty_export_unexplained" not in run_grade(ctx.grade, explained)[3]

        paths = write_csvs(PARENT, empty_hold, empty_hist, explained)
        assert not (explained / f"{SLUG}_export_status.csv").is_file(), "stale status note kept"

    def silent(ctx: Ctx):
        assert ctx.clean.mech["checks"]["empty_export_explained"] == "pass"
        assert "empty_export_unexplained" not in _issue_ids(ctx.clean.mech)
        assert export_mod.empty_export_note(
            PARENT, num_current=0, num_history=len(ctx.clean.rows), build_meta=ctx.clean.meta
        ) is None
        assert not (ctx.clean.out_dir / f"{SLUG}_export_status.csv").is_file()

    return fires, silent


@defect(
    "board_unknown_check",
    ("unknown_check_ids", "write_board"),
    "one mechanical check left at unknown on the clean board",
)
def _board_unknown():
    def fires(ctx: Ctx):
        mech = json.loads(json.dumps(ctx.clean.mech))
        mech["checks"]["chart_ranking_sane"] = "unknown"
        assert ctx.grade.unknown_check_ids([mech]) == ["mechanical:chart_ranking_sane"]
        text = ctx.grade.write_board(PARENT, ctx.tmp_path, [mech], None).read_text(encoding="utf-8")
        assert "BOARD: NEEDS_WORK" in text and "BOARD: PASS" not in text
        assert "- mechanical:chart_ranking_sane" in text

    def silent(ctx: Ctx):
        assert ctx.grade.unknown_check_ids([ctx.clean.mech]) == []
        assert "BOARD: PASS" in ctx.clean.board

    return fires, silent


@defect(
    "regex_lint_unanchored",
    ("unanchored_numeric_regex_hits",),
    r'a module under hidden_stock/ compiling "(?P<sh>[\d,]+)"',
)
def _regex_lint():
    from test_regex_lint import unanchored_numeric_regex_hits

    def fires(ctx: Ctx):
        root = ctx.tmp_path / "hidden_stock"
        (root / "quirks").mkdir(parents=True)
        (root / "quirks" / "_tmp_gate.py").write_text(
            'import re\n\nSHARES = re.compile(r"(?P<sh>[\\d,]+)")\n', encoding="utf-8"
        )
        hits = unanchored_numeric_regex_hits(root)
        assert hits == ['hidden_stock/quirks/_tmp_gate.py:3: SHARES = re.compile(r"(?P<sh>[\\d,]+)")']

    def silent(ctx: Ctx):
        assert unanchored_numeric_regex_hits() == []

    return fires, silent


@defect(
    "13d_event_in_q2_filed_in_45_day_window",
    ("positions_as_of.by_event",),
    "SERV 13D: event 2024-04-22, filed 2024-05-08 (before the Q1 13F filed 2024-05-15)",
)
def _13d_event_quarter():
    def _snaps(edgar: FakeEdgar):
        with (
            patch.object(sec_13g, "_list_13g_items", return_value=list(edgar.g13_items)),
            patch.object(
                sec_13g, "fetch_filing_text",
                side_effect=lambda _s, _c, _acc, primary: (edgar._doc(primary), "html"),
            ),
            patch.object(sec_13g.time, "sleep", return_value=None),
        ):
            return sec_13g.collect_13g_period_snapshots(
                cik=CIK, parent_ticker=PARENT, user_agent="tests", max_filings=10
            )

    def fires(ctx: Ctx):
        ordered, _meta = _snaps(ctx.clean.edgar)
        assert [(t[0], t[1]) for t in ordered] == [("2024-04-22", "2024-05-08"), ("2025-01-15", "2025-01-21")]
        stale = sec_13g.positions_as_of(ordered, "2024-05-15", by="filing")
        assert [p["investee_ticker"] for p in stale] == ["SERV"]
        serv = sorted(
            (r["period_end"], r["action"], r["shares_held"]) for r in ctx.clean.rows if r["investee_ticker"] == "SERV"
        )
        assert serv == [
            ("2024-06-30", "new", 5_298_833.0),
            ("2024-09-30", "hold", 5_298_833.0),
            ("2024-12-31", "hold", 5_298_833.0),
            ("2025-03-31", "sell", 4_748_833.0),
        ]

    def silent(ctx: Ctx):
        ordered, _meta = _snaps(ctx.clean.edgar)
        assert sec_13g.positions_as_of(ordered, "2024-03-31", by="event") == []
        assert not [r for r in ctx.clean.rows if r["period_end"] == "2024-03-31" and r["investee_ticker"] == "SERV"]

    return fires, silent


@defect(
    "reconcile_value_not_in_document",
    ("unreconciled_rows", "reconcile.not_found"),
    "GRAB 2024-06-30 shares_held changed to 650,000,000 (13F infotable says 600,000,000)",
)
def _reconcile_not_found():
    def fires(ctx: Ctx):
        out = ctx.export_copy()

        def mutate(df):
            df.loc[_at(df, "2024-06-30", "GRAB"), "shares_held"] = "650000000.0"
            return df

        _edit_csv(out / f"{SLUG}_equity_holdings_history.csv", mutate)
        results, code = run_reconcile(ctx.reconcile_cli, out, ctx.clean.edgar)
        bad = [r for r in results if r["status"] == "not_found"]
        assert code == 1
        assert [(r["period_end"], r["investee_ticker"]) for r in bad] == [("2024-06-30", "GRAB")]
        assert "missing: shares_held=650,000,000" in bad[0]["detail"]
        res = precheck(ctx.grade, out)
        assert res["checks"]["unreconciled_rows"] == "fail"
        assert "unreconciled_rows" in _issue_ids(res) and res["verdict"] == "fail"
        assert "BOARD: FAIL" in run_grade(ctx.grade, out)[3]

    def silent(ctx: Ctx):
        statuses = {r["status"] for r in ctx.clean.reconcile}
        assert statuses <= {"anchored", "skipped_null"}, statuses
        anchored = [r for r in ctx.clean.reconcile if r["status"] == "anchored"]
        assert {r["source_kind"] for r in anchored} == {"10q_investments_table", "10k_investments_table", "13f", "13g"}
        assert ctx.clean.reconcile_code == 0
        assert ctx.clean.mech["checks"]["unreconciled_rows"] == "pass"

    return fires, silent


def _fake_runner(calls: list):
    def run(name, packet_text, schema):
        calls.append(name)
        return {
            "judge": name, "verdict": "pass", "score": 100, "blocking_issues": [],
            "minor_issues": [], "what_looks_good": ["ok"], "checks": {"x": "pass"},
            "summary": f"{name} ok",
        }

    return run


@defect(
    "judge_digest_missing_sell_and_hash_skip",
    ("judge_digest.MISSING", "full_judge_skip_reason", "EXIT_FULL_SKIPPED"),
    "GRAB 2024-12-31 rows dropped from realized_pnl_qoq.csv; unchanged export re-judged in full mode",
)
def _judge_digest():
    from hidden_stock.quirks.holdings import judge_digest as jd

    def fires(ctx: Ctx):
        out = ctx.export_copy()
        _edit_csv(
            out / f"{SLUG}_realized_pnl_qoq.csv",
            lambda df: df[~_at(df, "2024-12-31", "GRAB")],
        )
        data = jd.build_digest_data(out, SLUG)
        sells = {(s["period_end"], s["ticker"]): s["realized"] for s in data["sells"]}
        assert sells[("2024-12-31", "GRAB")] == "MISSING"
        assert "- 2024-12-31 GRAB sell Δsh=-200000000.0 realized=MISSING" in jd.render_digest(data)

        full = ctx.export_copy("full")
        calls: list = []
        code1, info1 = ctx.grade.run_grade(
            ticker=PARENT, out_dir=full, judges=["fable"], judge_mode="full", llm_runner=_fake_runner(calls)
        )
        assert code1 == 0 and calls == ["fable"] and info1["skip_reason"] is None
        code2, info2 = ctx.grade.run_grade(
            ticker=PARENT, out_dir=full, judges=["fable"], judge_mode="full", llm_runner=_fake_runner(calls)
        )
        assert code2 == ctx.grade.EXIT_FULL_SKIPPED and calls == ["fable"]
        assert "already judged" in info2["skip_reason"] and info1["content_hash"][:12] in info2["skip_reason"]

    def silent(ctx: Ctx):
        data = jd.build_digest_data(ctx.clean.out_dir, SLUG)
        sells = {(s["period_end"], s["ticker"]): s["realized"] for s in data["sells"]}
        assert sells[("2024-12-31", "GRAB")] == "avg:estimated;fifo:estimated"
        assert sells[("2025-03-31", "SERV")] == "avg:unknown"
        assert "MISSING" not in {v.split(":")[0] for v in sells.values()}
        assert jd.full_judge_skip_reason(ctx.clean.out_dir, SLUG, judge_mode="full", force=False) is None

    return fires, silent


@defect(
    "duplicate_period_ticker",
    ("aur_one_per_period", "assert_unique_period_ticker"),
    "a second GRAB row (source=13g) in 2024-06-30",
)
def _duplicate_period_ticker():
    def fires(ctx: Ctx):
        """The exporter coalesces (pe, ticker) before asserting, so the assert is
        the backstop for the pre-coalesce era: prove it fires on the raw rows,
        that the pipeline absorbs the duplicate, and that with coalesce gone
        both the exporter and the precheck fire."""
        dup = dict(_by(ctx.clean.rows, "2024-06-30", "GRAB"))
        dup.update(market_value_usd=None, note="source=13g form=SC 13G")
        with pytest.raises(ValueError, match=r"duplicate investee_ticker.*2024-06-30/GRAB"):
            history_mod.assert_unique_period_ticker(ctx.clean.rows + [dup], context="dup")
        paths = export_rows(ctx.clean.rows + [dup], ctx.tmp_path / "dup_export")
        hist = pd.read_csv(paths["history"])
        assert int(_at(hist, "2024-06-30", "GRAB").sum()) == 1

        out = ctx.export_copy()

        def duplicate(df):
            extra = df[_at(df, "2024-06-30", "GRAB")].copy()
            extra["market_value_usd"] = ""
            extra["note"] = "source=13g form=SC 13G"
            return pd.concat([df, extra], ignore_index=True)

        _edit_csv(out / f"{SLUG}_equity_holdings_history.csv", duplicate)
        from hidden_stock.quirks.holdings import lookback

        ctx.monkeypatch.setattr(lookback, "coalesce_period_ticker", lambda rows: rows)
        with pytest.raises(ValueError, match=r"export/TESTCO: duplicate investee_ticker.*2024-06-30/GRAB"):
            export_rows(ctx.clean.rows + [dup], ctx.tmp_path / "dup_export_no_coalesce")
        res = precheck(ctx.grade, out)
        assert res["checks"]["aur_one_per_period"] == "fail"
        assert "duplicate_ticker_same_period" in _issue_ids(res) and res["verdict"] == "fail"

    def silent(ctx: Ctx):
        history_mod.assert_unique_period_ticker(ctx.clean.rows, context="clean")
        assert ctx.clean.mech["checks"]["aur_one_per_period"] == "pass"

    return fires, silent


_APPEND_EXIT = object()


def _history_mutation(gate: str, issue: str, pe: str, ticker: str, edits: dict, *, verdict="fail"):
    def fires(ctx: Ctx):
        out = ctx.export_copy()

        def mutate(df):
            mask = _at(df, pe, ticker)
            assert int(mask.sum()) == 1, (pe, ticker)
            for col, val in edits.items():
                if val is _APPEND_EXIT:
                    df.loc[mask, col] = df.loc[mask, col] + "; 13g_exit=1"
                else:
                    df.loc[mask, col] = val
            return df

        _edit_csv(out / f"{SLUG}_equity_holdings_history.csv", mutate)
        res = precheck(ctx.grade, out)
        assert res["checks"][gate] == "fail", res["checks"]
        assert issue in _issue_ids(res)
        assert res["verdict"] == verdict
        assert "BOARD: PASS" not in run_grade(ctx.grade, out)[3]

    def silent(ctx: Ctx):
        assert ctx.clean.mech["checks"][gate] == "pass"
        assert issue not in _issue_ids(ctx.clean.mech)

    return fires, silent


@defect(
    "13g_dollar_invent",
    ("no_otc_invent_marks", "assert_estimates_not_in_market_value"),
    "SERV 2024-06-30 market_value_usd=25,000,000 with only source=13g",
)
def _13g_dollar_invent():
    fires_csv, silent_csv = _history_mutation(
        "no_otc_invent_marks", "beneficial_ownership_used_as_value_source",
        "2024-06-30", "SERV", {"market_value_usd": "25000000.0"},
    )

    def fires(ctx: Ctx):
        fires_csv(ctx)
        est = dict(_by(ctx.clean.rows, "2024-06-30", "SERV"))
        est.update(market_value_usd=25e6, note=f"{est.get('note') or ''}; value_estimate=eod_at_filing")
        with pytest.raises(AssertionError, match=r"eod_estimate_as_market_value"):
            validate_mod.assert_estimates_not_in_market_value([est], context="t")

    def silent(ctx: Ctx):
        silent_csv(ctx)
        assert validate_mod.assert_estimates_not_in_market_value(ctx.clean.rows, context="t") is None

    return fires, silent


@defect(
    "ownership_pct_as_shares",
    ("no_share_invent", "assert_live_shares_held_sane"),
    "SERV 2024-06-30 shares_held=14.6 (== ownership_pct)",
)
def _pct_as_shares():
    fires_csv, silent_csv = _history_mutation(
        "no_share_invent", "ownership_pct_stuffed_into_shares_held",
        "2024-06-30", "SERV", {"shares_held": "14.6"},
    )

    def fires(ctx: Ctx):
        fires_csv(ctx)
        stuffed = dict(_by(ctx.clean.rows, "2024-06-30", "SERV"), shares_held=14.6, ownership_pct=14.6)
        with pytest.raises(ValueError, match=r"shares_held=14.6 == ownership_pct=14.6"):
            validate_mod.assert_live_shares_held_sane([stuffed])

    def silent(ctx: Ctx):
        silent_csv(ctx)
        assert validate_mod.assert_live_shares_held_sane(ctx.clean.rows) is None

    return fires, silent


@defect("placeholder_exchange_ticker", ("no_placeholder_tickers",), "DIDIY 2023-12-31 re-tickered ANT from the note name")
def _placeholder_ticker():
    return _history_mutation(
        "no_placeholder_tickers", "placeholder_exchange_like_ticker",
        "2023-12-31", "DIDIY", {"investee_ticker": "ANT", "investee_name": "Ant Group", "cusip": ""},
    )


@defect("false_13g_exit", ("no_false_13g_exit",), "SERV 2024-09-30 note stamped 13g_exit=1 while shares > 0")
def _false_13g_exit():
    return _history_mutation(
        "no_false_13g_exit", "false_13g_exit_positive_stake",
        "2024-09-30", "SERV", {"note": _APPEND_EXIT},
    )


@defect("public_name_as_private", ("no_private_escape_hatch",), "DIDIY 2023-12-31 hidden as PRIV_DIDI_GLOBAL + ticker=private_note")
def _public_as_private():
    return _history_mutation(
        "no_private_escape_hatch", "public_ticker_misclassified_as_private",
        "2023-12-31", "DIDIY",
        {"investee_ticker": "PRIV_DIDI_GLOBAL", "investee_name": "Didi Global", "cusip": "",
         "note": "source=10q_investments_table; ticker=private_note"},
    )


@defect("overlay_exit_zero_dollar", ("no_overlay_exit_invent",), "SERV 2025-03-31 made an exit with market_value_usd=0.0")
def _overlay_exit_zero():
    return _history_mutation(
        "no_overlay_exit_invent", "overlay_exit_invented_zero",
        "2025-03-31", "SERV", {"action": "exit", "market_value_usd": "0.0", "shares_held": "0.0"},
    )


@defect("blank_public_ticker", ("no_blank_public_ticker",), "GRAB 2024-06-30 ticker/CUSIP blanked under an unresolvable name")
def _blank_public_ticker():
    return _history_mutation(
        "no_blank_public_ticker", "blank_public_ticker",
        "2024-06-30", "GRAB", {"investee_ticker": "", "cusip": "", "investee_name": "Zed Robotics Corp"},
    )


@defect(
    "display_basis_cliff",
    ("no_display_basis_cliff", "chart_ranking_sane"),
    "GRAB 2024-09-30 chart cell cut to 20% of the prior quarter, rebounding next quarter",
)
def _display_basis_cliff():
    def fires(ctx: Ctx):
        out = ctx.export_copy()

        def cliff(df):
            prev = float(df.loc[df["period_end"] == "2024-06-30", "GRAB"].iloc[0])
            df.loc[df["period_end"] == "2024-09-30", "GRAB"] = str(round(prev * 0.2, 2))
            return df

        _edit_csv(out / f"{SLUG}_holdings_qoq_chart.csv", cliff)
        res = precheck(ctx.grade, out)
        assert res["checks"]["no_display_basis_cliff"] == "fail"
        assert res["checks"]["chart_ranking_sane"] == "fail"
        ev = next(i for i in res["blocking_issues"] if i["id"] == "display_basis_cliff")["evidence"]
        assert ev and ev[0].startswith("GRAB@2024-09-30")
        assert res["verdict"] == "fail"

    def silent(ctx: Ctx):
        assert ctx.clean.mech["checks"]["no_display_basis_cliff"] == "pass"
        assert ctx.clean.mech["checks"]["chart_ranking_sane"] == "pass"
        chart = ctx.clean.csv("holdings_qoq_chart")
        assert list(chart["period_end"]) == GRID

    return fires, silent


@defect(
    "parent_scoped_uber_anchors",
    ("parent_scoped", "didi_2026_06_30_fv", "grab_aurora_vs_10q"),
    "same export graded as UBER with a 2026-06-30 DIDIY portfolio row of $1.0B",
)
def _parent_scoped():
    def fires(ctx: Ctx):
        out = ctx.export_copy()
        _edit_csv(
            out / f"{SLUG}_portfolio_by_period.csv",
            lambda df: pd.concat(
                [df, pd.DataFrame([{"period_end": "2026-06-30", "investee_ticker": "DIDIY", "market_value_usd": "1000000000.0"}])],
                ignore_index=True,
            ),
        )
        res = precheck(ctx.grade, out, parent="UBER")
        assert res["parent"] == "UBER" and res["checks"]["parent_scoped"] == "pass"
        assert res["checks"]["didi_2026_06_30_fv"] == "fail"
        assert res["checks"]["grab_aurora_vs_10q"] == "fail"
        assert "didi_fv_mismatch" in _issue_ids(res) and res["verdict"] == "fail"

    def silent(ctx: Ctx):
        checks = ctx.clean.mech["checks"]
        assert ctx.clean.mech["parent"] == PARENT and checks["parent_scoped"] == "pass"
        assert (checks["didi_2026_06_30_fv"], checks["grab_aurora_vs_10q"]) == ("n/a", "n/a")
        assert "didi_fv_mismatch" not in _issue_ids(ctx.clean.mech)

    return fires, silent


@defect(
    "mtm_identity_break",
    ("assert_mtm_identity",),
    "mtm_pnl at 2024-09-30 shifted by $5 so mtm != end - start - flow",
)
def _mtm_identity():
    def fires(ctx: Ctx):
        r = ctx.clean.csv("returns_by_period").copy()
        r.loc[r["period_end"] == "2024-09-30", "mtm_pnl"] += 5.0
        with pytest.raises(AssertionError, match=r"mtm_identity: period 2024-09-30"):
            perf_mod.assert_mtm_identity(r)

    def silent(ctx: Ctx):
        assert perf_mod.assert_mtm_identity(ctx.clean.csv("returns_by_period")) is None

    return fires, silent


@pytest.mark.parametrize("case", DEFECTS, ids=[d.id for d in DEFECTS])
def test_gate_fires_and_stays_silent(case: Defect, clean, grade, reconcile_cli, tmp_path, monkeypatch, caplog):
    ctx = Ctx(clean, grade, reconcile_cli, tmp_path, monkeypatch, caplog)
    case.silent(ctx)
    case.fires(ctx)


_CHECK_ID_RE = re.compile(r'checks(?:\[\s*|\.setdefault\(\s*)"([a-z0-9_]+)"')


def registered_precheck_ids(grade_src: str, clean_checks: dict) -> set[str]:
    return set(_CHECK_ID_RE.findall(grade_src)) | set(clean_checks)


def exported_assert_functions() -> set[str]:
    out: set[str] = set()
    for mod in (perf_mod, history_mod, validate_mod):
        out |= {n for n, obj in vars(mod).items() if n.startswith("assert_") and inspect.isfunction(obj)}
    return out


def test_every_gate_has_an_effectiveness_test(clean: Scenario):
    src = (_ROOT / "scripts" / "grade_holdings_sheet.py").read_text(encoding="utf-8")
    required = registered_precheck_ids(src, clean.mech["checks"]) | exported_assert_functions()
    covered = {g for d in DEFECTS for g in d.gates}
    ids = [d.id for d in DEFECTS]
    assert len(ids) == len(set(ids))
    missing = sorted(required - covered)
    assert not missing, (
        "gates with no effectiveness test in tests/test_gate_effectiveness.py "
        f"(add a Defect naming each): {missing}"
    )
    assert {"sell_without_realized_row", "period_grid_gap", "dietz_sane", "unreconciled_rows"} <= required
    assert {"assert_sell_realized_coverage", "assert_note_dates_in_period_grid",
            "assert_unique_period_ticker", "assert_mtm_identity"} <= required
