"""reconcile: sheet values must appear verbatim in the EDGAR filing each row cites."""

from __future__ import annotations

import csv
import importlib.util
import sys
from pathlib import Path

import pytest

from hidden_stock.quirks.holdings import reconcile as rc

_ROOT = Path(__file__).resolve().parents[1]
UBER_CIK = "1543151"
TENQ_ACC = "0001543151-26-000032"
TENQ_URL = f"https://www.sec.gov/Archives/edgar/data/{UBER_CIK}/000154315126000032/{TENQ_ACC}-index.htm"
F13_ACC = "0001552781-25-000036"
F13_URL = f"https://www.sec.gov/Archives/edgar/data/{UBER_CIK}/000155278125000036/{F13_ACC}-index.htm"
G13_ACC = "0001552781-24-000296"
G13_URL = f"https://www.sec.gov/Archives/edgar/data/{UBER_CIK}/000155278124000296/{G13_ACC}-index.htm"

TENQ_HTML = """<html><body><p>Investments</p>
<table><tr><td>Didi</td><td>$</td><td>1,900</td></tr>
<tr><td>Grab</td><td>$</td><td>2,020</td></tr>
<tr><td>Aurora</td><td>$</td><td>1,763</td></tr></table>
<p>Total 11,900 and 1,900,5 are decoys.</p></body></html>"""

INFOTABLE_XML = """<?xml version="1.0"?><informationTable>
<infoTable><nameOfIssuer>GRAB HOLDINGS LTD</nameOfIssuer><cusip>G4124C109</cusip>
<value>2529462075</value><shrsOrPrnAmt><sshPrnamt>535902982</sshPrnamt></shrsOrPrnAmt></infoTable>
</informationTable>"""

G13_HTML = """<html><body><p>SCHEDULE 13D</p><p>Serve Robotics Inc.</p>
<p>Amount beneficially owned: 5,298,833</p><p>Percent of class: 15.2%</p></body></html>"""

D13A_ACC = "0001552781-25-000298"
D13A_URL = f"https://www.sec.gov/Archives/edgar/data/{UBER_CIK}/000155278125000298/{D13A_ACC}-index.htm"
D13A_XML = """<?xml version="1.0" encoding="UTF-8"?><edgarSubmission xmlns="http://www.sec.gov/edgar/schedule13D">
<soleVotingPower>2070629.00</soleVotingPower><percentOfClass>3.36</percentOfClass></edgarSubmission>"""
D13A_EXHIBIT = "<html><body><p>Joint filing agreement. No numbers here.</p></body></html>"


class FakeEdgar:
    """Canned EDGAR: no network. Records every call so tests can assert caching."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.docs = {
            TENQ_ACC: [
                {"name": f"{TENQ_ACC}-index.htm", "size": 1000},
                {"name": "R4.htm", "size": 999999},
                {"name": "uber-20260630.htm", "size": 5000},
                {"name": "ex31.htm", "size": 200},
            ],
            G13_ACC: [{"name": "serv-13d.htm", "size": 300}],
            D13A_ACC: [
                {"name": f"{D13A_ACC}-index.html", "size": ""},
                {"name": "e25338_ex99-1.htm", "size": 17403},
                {"name": "primary_doc.xml", "size": 9414},
            ],
        }
        self.text = {
            (TENQ_ACC, "uber-20260630.htm"): TENQ_HTML,
            (G13_ACC, "serv-13d.htm"): G13_HTML,
            (D13A_ACC, "primary_doc.xml"): D13A_XML,
            (D13A_ACC, "e25338_ex99-1.htm"): D13A_EXHIBIT,
        }

    def list_filing_documents(self, cik, accession_no):
        self.calls.append(("list", accession_no))
        if accession_no not in self.docs:
            raise RuntimeError("404 index.json")
        return self.docs[accession_no]

    def fetch_filing_document(self, cik, accession_no, name):
        self.calls.append(("fetch", accession_no, name))
        return self.text[(accession_no, name)]

    def fetch_13f_infotable(self, cik, accession_no):
        self.calls.append(("infotable", accession_no))
        return INFOTABLE_XML if accession_no == F13_ACC else None


def _row(**kw):
    base = {
        "period_end": "2026-06-30",
        "investee_ticker": "DIDIY",
        "cusip": "",
        "shares_held": "",
        "ownership_pct": "",
        "market_value_usd": "",
        "accession_no": TENQ_ACC,
        "filing_url": TENQ_URL,
        "note": "source=10q_investments_table as_of=2026-06-30 fv_usd=1900000000",
    }
    base.update(kw)
    return base


@pytest.fixture
def fetcher(tmp_path):
    return rc.DocumentFetcher(FakeEdgar(), tmp_path / "cache")


def test_investments_table_musd_anchored(fetcher):
    res = rc.reconcile_row(_row(market_value_usd="1900000000.0"), fetcher)
    assert res["status"] == rc.STATUS_ANCHORED
    assert res["searched"] == "market_value_usd=1,900 ($M)"
    assert res["matched"] == "market_value_usd=1,900"
    assert res["document"] == "uber-20260630.htm"
    assert res["cik"] == UBER_CIK
    assert res["source_kind"] == "10q_investments_table"


def test_musd_not_found_when_filing_shows_other_number(fetcher):
    row = _row(
        market_value_usd="2338000000.0",
        note="source=10q_investments_table as_of=2026-06-30 fv_usd=2338000000",
    )
    res = rc.reconcile_row(row, fetcher)
    assert res["status"] == rc.STATUS_NOT_FOUND
    assert "missing: market_value_usd=2,338 ($M)" in res["detail"]
    assert res["matched"] == ""


def test_musd_boundary_rejects_decoys(fetcher):
    """11,900 and 1,900,5 must not satisfy a search for 1,900; 900 must not match 1,900."""
    row = _row(
        market_value_usd="900000000.0",
        note="source=10q_investments_table as_of=2026-06-30 fv_usd=900000000",
    )
    assert rc.reconcile_row(row, fetcher)["status"] == rc.STATUS_NOT_FOUND


def test_13f_share_count_and_cusip_anchored(fetcher):
    row = _row(
        investee_ticker="GRAB",
        period_end="2024-12-31",
        cusip="G4124C109",
        shares_held="535902982.0",
        market_value_usd="2529462075.0",
        accession_no=F13_ACC,
        filing_url=F13_URL,
        note="source=sec_api_13f cusip=G4124C109 class=CLASS A ORD",
    )
    res = rc.reconcile_row(row, fetcher)
    assert res["status"] == rc.STATUS_ANCHORED
    assert res["searched"] == "shares_held=535,902,982 | cusip=G4124C109"
    assert res["matched"] == "shares_held=535902982; cusip=G4124C109"
    assert res["document"] == "infotable.xml"


def test_13f_wrong_share_count_not_found(fetcher):
    row = _row(
        investee_ticker="GRAB",
        cusip="G4124C109",
        shares_held="535902983.0",
        accession_no=F13_ACC,
        filing_url=F13_URL,
        note="source=sec_api_13f cusip=G4124C109 class=CLASS A ORD",
    )
    res = rc.reconcile_row(row, fetcher)
    assert res["status"] == rc.STATUS_NOT_FOUND
    assert "missing: shares_held=535,902,983" in res["detail"]
    assert "cusip=G4124C109" in res["matched"]


def test_13g_share_count_or_pct_anchored(fetcher):
    row = _row(
        investee_ticker="SERV",
        period_end="2024-03-31",
        shares_held="5298833.0",
        ownership_pct="15.2",
        accession_no=G13_ACC,
        filing_url=G13_URL,
        note="source=13g form=SC 13D cik=0001543151",
    )
    res = rc.reconcile_row(row, fetcher)
    assert res["status"] == rc.STATUS_ANCHORED
    assert res["matched"] == "shares_held=5,298,833; ownership_pct=15.2%"


def test_structured_13d_prefers_primary_doc_xml_over_exhibit(fetcher):
    """Live miss on UBER/SERV 2026-06-30: the 13D/A's only .htm is an exhibit;
    the share count sits in primary_doc.xml as 2070629.00 and the pct as bare 3.36."""
    row = _row(
        investee_ticker="SERV",
        period_end="2026-06-30",
        shares_held="2070629.0",
        ownership_pct="3.36",
        accession_no=D13A_ACC,
        filing_url=D13A_URL,
        note="source=13g form=SCHEDULE 13D/A cik=0001543151",
    )
    res = rc.reconcile_row(row, fetcher)
    assert res["status"] == rc.STATUS_ANCHORED
    assert res["document"] == "primary_doc.xml"
    assert res["matched"] == "shares_held=2070629.00; ownership_pct=3.36"
    assert ("fetch", D13A_ACC, "e25338_ex99-1.htm") not in fetcher.edgar.calls


def test_pick_primary_document_demotes_exhibits():
    pick = rc._pick_primary_document
    assert pick([
        {"name": "uber-20260630.htm", "size": 5000},
        {"name": "uber-20260630ex311.htm", "size": 9000},
        {"name": "d1dex991.htm", "size": 99999},
        {"name": "R4.htm", "size": 999999},
        {"name": "0001-index.htm", "size": 999999},
    ]) == "uber-20260630.htm"
    assert pick([{"name": "ex99-1.htm", "size": 10}, {"name": "0001.txt", "size": 99}]) == "ex99-1.htm"
    assert pick([{"name": "0001.txt", "size": 99}]) == "0001.txt"
    assert pick([{"name": "0001-index.htm", "size": 99}]) is None


def test_no_citation_when_accession_blank(fetcher):
    res = rc.reconcile_row(_row(market_value_usd="1900000000.0", accession_no="", filing_url=""), fetcher)
    assert res["status"] == rc.STATUS_NO_CITATION
    assert res["searched"] == "market_value_usd=1,900 ($M)"


def test_skipped_null_when_no_value_or_shares(fetcher):
    res = rc.reconcile_row(_row(market_value_usd="", shares_held="nan"), fetcher)
    assert res["status"] == rc.STATUS_SKIPPED_NULL
    assert not fetcher.edgar.calls


def test_fetch_error_is_reported_not_swallowed(fetcher):
    row = _row(market_value_usd="1900000000.0", accession_no="0009999999-99-000001")
    res = rc.reconcile_row(row, fetcher)
    assert res["status"] == rc.STATUS_FETCH_ERROR
    assert "RuntimeError: 404 index.json" in res["detail"]


def test_primary_document_skips_index_and_r_pages(fetcher):
    rc.reconcile_row(_row(market_value_usd="1900000000.0"), fetcher)
    fetched = [c for c in fetcher.edgar.calls if c[0] == "fetch"]
    assert fetched == [("fetch", TENQ_ACC, "uber-20260630.htm")]


def test_disk_cache_makes_rerun_free(tmp_path):
    edgar = FakeEdgar()
    f1 = rc.DocumentFetcher(edgar, tmp_path / "cache")
    rc.reconcile_row(_row(market_value_usd="1900000000.0"), f1)
    rc.reconcile_row(_row(investee_ticker="GRAB", market_value_usd="2020000000.0",
                          note="source=10q_investments_table fv_usd=2020000000"), f1)
    n = len(edgar.calls)
    assert n == 2
    f2 = rc.DocumentFetcher(edgar, tmp_path / "cache")
    res = rc.reconcile_row(_row(market_value_usd="1900000000.0"), f2)
    assert res["status"] == rc.STATUS_ANCHORED
    assert len(edgar.calls) == n
    assert (tmp_path / "cache" / UBER_CIK / "000154315126000032" / "primary.txt").is_file()


def test_max_rows_counts_only_checked_rows(fetcher):
    rows = [
        _row(market_value_usd=""),
        _row(market_value_usd="1900000000.0"),
        _row(investee_ticker="GRAB", market_value_usd="2020000000.0",
             note="source=10q_investments_table fv_usd=2020000000"),
    ]
    out = rc.reconcile_rows(rows, fetcher=fetcher, max_rows=1)
    assert [r["status"] for r in out] == [rc.STATUS_SKIPPED_NULL, rc.STATUS_ANCHORED]


def _write_anchors(dir_: Path, parent: str, body: str) -> Path:
    p = dir_ / f"{parent.lower()}_anchors.yaml"
    p.write_text(body, encoding="utf-8")
    return p


def test_anchor_yaml_match_and_mismatch(tmp_path, fetcher):
    _write_anchors(
        tmp_path,
        "uber",
        """
anchors:
  - investee_ticker: DIDIY
    period_end: "2026-06-30"
    expected_musd: 1900
    source_url: https://www.sec.gov/Archives/edgar/data/1543151/000154315126000032/uber-20260630.htm
    quote: "Didi $ 1,900"
  - investee_ticker: GRAB
    period_end: "2026-06-30"
    field: market_value_usd
    expected_value: 2020000000
  - investee_ticker: AUR
    period_end: "2026-06-30"
    expected_musd: 1763
  - investee_ticker: JOBY
    period_end: "2026-06-30"
    expected_musd: 2020
""",
    )
    anchors = rc.load_anchors("UBER", tmp_path)
    assert len(anchors) == 4
    rows = [
        _row(market_value_usd="1900000000.0"),
        _row(investee_ticker="GRAB", market_value_usd="2020354242.0"),
        _row(investee_ticker="JOBY", market_value_usd="2020354242.0"),
    ]
    out = rc.check_anchors(rows, anchors, fetcher=fetcher)
    by = {r["investee_ticker"]: r for r in out}
    assert by["DIDIY"]["status"] == rc.STATUS_ANCHOR_MATCH
    assert by["DIDIY"]["detail"] == "quote: found"
    assert by["DIDIY"]["document"] == "uber-20260630.htm"
    assert by["GRAB"]["status"] == rc.STATUS_ANCHOR_MISMATCH
    assert by["GRAB"]["detail"] == "sheet 2,020,354,242 != expected 2,020,000,000 (tol 0)"
    assert by["JOBY"]["status"] == rc.STATUS_ANCHOR_MATCH
    assert by["AUR"]["status"] == rc.STATUS_ANCHOR_MISSING_ROW
    assert rc.has_failures(out)


def test_anchor_missing_file_is_empty(tmp_path):
    assert rc.load_anchors("NOPE", tmp_path) == []


def test_example_anchors_yaml_parses():
    example = _ROOT / "hidden_stock" / "quirks" / "holdings" / "data" / "anchors.example.yaml"
    import yaml

    data = yaml.safe_load(example.read_text(encoding="utf-8"))
    assert data["anchors"][0]["investee_ticker"] == "EXAMPLE"


def test_anchors_yaml_gitignored():
    ignore = (_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "**/data/*_anchors.yaml" in ignore


def _load_cli():
    spec = importlib.util.spec_from_file_location(
        "reconcile_holdings", _ROOT / "scripts" / "reconcile_holdings.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["reconcile_holdings"] = mod
    spec.loader.exec_module(mod)
    return mod


def _write_history(path: Path, rows: list[dict]) -> Path:
    cols = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    return path


def test_cli_writes_csv_and_exit_codes(tmp_path, capsys):
    cli = _load_cli()
    hist = _write_history(
        tmp_path / "uber_equity_holdings_history.csv",
        [
            _row(market_value_usd="1900000000.0"),
            _row(investee_ticker="GRAB", market_value_usd="2020000000.0",
                 note="source=10q_investments_table fv_usd=2020000000"),
            _row(investee_ticker="AUR", market_value_usd=""),
        ],
    )
    out_csv = tmp_path / "uber_reconcile.csv"
    results, code = cli.run(
        parent="UBER", history_csv=hist, out_csv=out_csv, edgar=FakeEdgar(),
        cache_dir=tmp_path / "cache", anchors_dir=tmp_path, quiet=True,
    )
    assert code == 0
    rows = list(csv.DictReader(out_csv.open()))
    assert [r["status"] for r in rows] == ["anchored", "anchored", "skipped_null"]
    assert rows[0]["searched"] == "market_value_usd=1,900 ($M)"
    printed = capsys.readouterr().out
    assert "RECONCILE: PASS" in printed
    assert "anchored=2" in printed

    _write_anchors(tmp_path, "uber", "anchors:\n  - investee_ticker: DIDIY\n    period_end: '2026-06-30'\n    expected_musd: 1901\n")
    _, code = cli.run(
        parent="UBER", history_csv=hist, out_csv=out_csv, edgar=FakeEdgar(),
        cache_dir=tmp_path / "cache", anchors_dir=tmp_path, quiet=True,
    )
    assert code == 1
    rows = list(csv.DictReader(out_csv.open()))
    assert rows[-1]["kind"] == "anchor" and rows[-1]["status"] == "anchor_mismatch"
    assert "RECONCILE: FAIL" in capsys.readouterr().out


def test_cli_not_found_exits_1(tmp_path):
    cli = _load_cli()
    hist = _write_history(
        tmp_path / "uber_equity_holdings_history.csv",
        [_row(market_value_usd="2338000000.0",
              note="source=10q_investments_table fv_usd=2338000000")],
    )
    _, code = cli.run(
        parent="UBER", history_csv=hist, out_csv=tmp_path / "uber_reconcile.csv",
        edgar=FakeEdgar(), cache_dir=tmp_path / "cache", anchors_dir=tmp_path, quiet=True,
    )
    assert code == 1


def test_grade_precheck_flags_unreconciled_rows(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "grade_holdings_sheet", _ROOT / "scripts" / "grade_holdings_sheet.py"
    )
    grade = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(grade)
    hist = _write_history(
        tmp_path / "uber_equity_holdings_history.csv",
        [{**_row(market_value_usd="1900000000.0"), "investee_name": "Didi", "action": "hold"}],
    )
    portfolio = tmp_path / "uber_portfolio_by_period.csv"

    mech = grade.mechanical_precheck(hist, portfolio, parent="UBER")
    assert mech["checks"]["unreconciled_rows"] == "not_run"

    rc.write_reconcile_csv(
        [{"kind": "history", "period_end": "2026-06-30", "investee_ticker": "DIDIY",
          "status": "not_found", "searched": "market_value_usd=1,900 ($M)"}],
        tmp_path / "uber_reconcile.csv",
    )
    mech = grade.mechanical_precheck(hist, portfolio, parent="UBER")
    assert mech["checks"]["unreconciled_rows"] == "fail"
    ids = [i["id"] for i in mech["blocking_issues"]]
    assert "unreconciled_rows" in ids
    assert mech["verdict"] == "fail"

    rc.write_reconcile_csv(
        [{"kind": "history", "period_end": "2026-06-30", "investee_ticker": "DIDIY",
          "status": "anchored", "searched": "market_value_usd=1,900 ($M)"}],
        tmp_path / "uber_reconcile.csv",
    )
    mech = grade.mechanical_precheck(hist, portfolio, parent="UBER")
    assert mech["checks"]["unreconciled_rows"] == "pass"
    assert "unreconciled_rows" not in [i["id"] for i in mech["blocking_issues"]]
