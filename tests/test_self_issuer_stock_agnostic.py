"""Self-issuer filtering is stock-agnostic: EDGAR names (current + former), not a
hardcoded UBER/BRK/BABA list.

Origin: the PDD export shipped 11 history rows + 1 live holding that were
third-party SC 13D/13G filings ABOUT Pinduoduo, listed under PDD's own CIK.
The cover page said "Pinduoduo Inc." with no trading symbol, so the
ticker-equality check never fired and no name hint existed for PDD.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from hidden_stock.quirks.holdings import sec_13g
from hidden_stock.quirks.holdings.identity import (
    clean_issuer_name,
    company_name_stem,
    name_matches_parent_hints,
)
from hidden_stock.quirks.holdings.sec_13g import (
    collect_13g_period_snapshots,
    fetch_latest_13g_holdings,
    is_self_issuer,
    parent_name_hints_for,
    parse_13g_html,
    raw_to_live_row,
    raw_to_position,
)
from hidden_stock.resources import edgar_resource

PDD_NAMES = ["PDD Holdings Inc.", "Pinduoduo Inc.", "Walnut Street Group Holding Ltd"]
UBER_NAMES = ["Uber Technologies, Inc."]
BABA_NAMES = ["Alibaba Group Holding Ltd"]
TENCENT_NAMES = ["TENCENT HOLDINGS Ltd"]

PDD_13D_2018 = """<html><body>
SCHEDULE 13D
Pinduoduo Inc. (Name of Issuer)
Class A Ordinary Shares, par value US$0.000005 per share (Title of Class of Securities)
722304102 (CUSIP Number)
July 26, 2018 (Date of Event Which Requires Filing of this Statement)
(11) Aggregate Amount Beneficially Owned by Each Reporting Person 786,466,688
(13) Percent of Class Represented by Amount in Row (11): 33.4%
(14) Type of Reporting Person IN
</body></html>"""

PDD_13G_2019 = """<html><body>
SCHEDULE 13G
Pinduoduo Inc. (Name of Issuer)
Class A Ordinary Shares (Title of Class of Securities)
722304102 (CUSIP Number)
December 31, 2018 (Date of Event Which Requires Filing of this Statement)
(9) Aggregate Amount Beneficially Owned by Each Reporting Person 181,830,600
(11) Percent of Class Represented by Amount in Row (9): 7.7%
</body></html>"""

PDD_ITEMS = [
    ("2019-02-14", "SC 13G", "0001104659-19-000002", "pdd_13g_2019.htm"),
    ("2018-08-06", "SC 13D", "0001104659-18-000001", "pdd_13d_2018.htm"),
]
PDD_DOCS = {"pdd_13g_2019.htm": PDD_13G_2019, "pdd_13d_2018.htm": PDD_13D_2018}


def _patched_13g(items, docs):
    return (
        patch.object(sec_13g, "_list_13g_items", return_value=list(items)),
        patch.object(
            sec_13g,
            "fetch_filing_text",
            side_effect=lambda _s, _c, _acc, primary: (docs[primary], "html"),
        ),
        patch.object(sec_13g.time, "sleep", return_value=None),
    )


def test_company_name_stem_strips_corporate_suffixes_only():
    assert company_name_stem("PDD Holdings Inc.") == "pdd"
    assert company_name_stem("Pinduoduo Inc.") == "pinduoduo"
    assert company_name_stem("Uber Technologies, Inc.") == "uber technologies"
    assert company_name_stem("Alibaba Group Holding Ltd") == "alibaba"
    assert company_name_stem("Walnut Street Group Holding Ltd") == "walnut street"
    assert company_name_stem("Aurora Innovation") == "aurora innovation"
    assert company_name_stem("Grab Holdings") == "grab"
    assert company_name_stem("") == ""


def test_name_matches_parent_hints_stem_and_brand_rules():
    assert name_matches_parent_hints("Pinduoduo Inc.", PDD_NAMES)
    assert name_matches_parent_hints("PDD Holdings Inc.", PDD_NAMES)
    assert name_matches_parent_hints("Uber Technologies, Inc.", ["uber"])
    assert name_matches_parent_hints("Uber", ["uber technologies"])
    assert name_matches_parent_hints("Alibaba Group Holding", BABA_NAMES)
    assert not name_matches_parent_hints("Aurora Innovation", UBER_NAMES + ["uber"])
    assert not name_matches_parent_hints("Grab Holdings", UBER_NAMES + ["uber"])
    assert not name_matches_parent_hints("Tencent Music Entertainment Group", TENCENT_NAMES)
    assert not name_matches_parent_hints("Sea Limited", ["se"])


@pytest.mark.parametrize(
    "issuer, parent, hints, expected",
    [
        ("Pinduoduo Inc.", "PDD", PDD_NAMES, True),
        ("PDD Holdings Inc.", "PDD", PDD_NAMES, True),
        ("PDD Holdings Inc.", "PDD", [], True),
        ("Uber Technologies, Inc.", "UBER", UBER_NAMES, True),
        ("Uber Technologies, Inc.", "UBER", [], True),
        ("Alibaba Group Holding", "BABA", BABA_NAMES, True),
        ("Aurora Innovation", "UBER", UBER_NAMES, False),
        ("Grab Holdings", "UBER", UBER_NAMES, False),
        ("DiDi Global Inc.", "UBER", UBER_NAMES, False),
        ("Tencent Music Entertainment Group", "TCEHY", TENCENT_NAMES, False),
    ],
)
def test_is_self_issuer_by_edgar_names(issuer, parent, hints, expected):
    parsed = {"issuer_name": issuer, "ticker": None, "ownership_pct": 5.0}
    assert is_self_issuer(parsed, parent_ticker=parent, parent_name_hints=hints) is expected


def test_pdd_third_party_13d_dropped_from_live_and_history_rows():
    parsed = parse_13g_html(PDD_13D_2018)
    assert clean_issuer_name(parsed["issuer_name"]) == "Pinduoduo Inc."
    assert parsed.get("ticker") is None
    assert is_self_issuer(parsed, parent_ticker="PDD", parent_name_hints=PDD_NAMES)
    assert parsed["shares"] == 786_466_688.0 and parsed["ownership_pct"] == 33.4
    kw = dict(form="SC 13D", acc="0001104659-18-000001", filing_date="2018-08-06", cik="0001737806")
    assert raw_to_live_row(parsed, parent_ticker="PDD", parent_name_hints=PDD_NAMES, **kw) is None
    assert raw_to_position(parsed, parent_ticker="PDD", parent_name_hints=PDD_NAMES, **kw) is None
    assert raw_to_live_row(parsed, parent_ticker="TCEHY", parent_name_hints=TENCENT_NAMES, **kw)[
        "investee_ticker"
    ] == "PDD"


def test_pdd_collectors_skip_self_filings_and_count_them():
    p1, p2, p3 = _patched_13g(PDD_ITEMS, PDD_DOCS)
    with p1, p2, p3:
        rows, meta = fetch_latest_13g_holdings(
            cik="0001737806", parent_ticker="PDD", user_agent="t", parent_name_hints=PDD_NAMES
        )
        snaps, hmeta = collect_13g_period_snapshots(
            cik="0001737806", parent_ticker="PDD", user_agent="t", parent_name_hints=PDD_NAMES
        )
    assert rows == [] and meta["num_self_issuer_filings"] == 2
    assert snaps == [] and hmeta["num_self_issuer_filings"] == 2


def test_uber_negatives_survive_collectors():
    docs = {
        "aur.htm": PDD_13D_2018.replace("Pinduoduo Inc.", "Aurora Innovation, Inc."),
        "grab.htm": PDD_13G_2019.replace("Pinduoduo Inc.", "Grab Holdings Limited"),
    }
    items = [("2025-02-14", "SC 13G", "a-1", "aur.htm"), ("2025-02-13", "SC 13G", "a-2", "grab.htm")]
    p1, p2, p3 = _patched_13g(items, docs)
    with p1, p2, p3:
        rows, meta = fetch_latest_13g_holdings(
            cik="0001543151", parent_ticker="UBER", user_agent="t", parent_name_hints=UBER_NAMES
        )
    assert sorted(r["investee_ticker"] for r in rows) == ["AUR", "GRAB"]
    assert meta["num_self_issuer_filings"] == 0


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code != 200:
            raise RuntimeError(f"http {self.status_code}")

    def json(self):
        return self._payload


class _Session:
    def __init__(self, payload):
        self.payload = payload
        self.urls: list[str] = []

    def get(self, url, timeout=None):
        self.urls.append(url)
        return _Resp(self.payload)


def test_get_company_names_uses_current_and_former_names_and_caches(tmp_path, monkeypatch):
    payload = {
        "cik": "1737806",
        "name": "PDD Holdings Inc.",
        "tickers": ["PDD"],
        "formerNames": [
            {"name": "Pinduoduo Inc.", "from": "2018", "to": "2023"},
            {"name": "Walnut Street Group Holding Ltd", "from": "2015", "to": "2018"},
        ],
    }
    session = _Session(payload)
    monkeypatch.setattr(edgar_resource, "COMPANY_NAMES_CACHE_DIR", tmp_path)
    edgar_resource._COMPANY_NAMES_CACHE.clear()
    edgar = edgar_resource.EdgarResource(user_agent="tests")
    with patch.object(edgar_resource.EdgarResource, "_session", return_value=session):
        names = edgar.get_company_names("1737806")
        again = edgar.get_company_names("0001737806")
    assert names == PDD_NAMES and again == PDD_NAMES
    assert session.urls == ["https://data.sec.gov/submissions/CIK0001737806.json"]
    on_disk = json.loads((tmp_path / "CIK0001737806.json").read_text())
    assert on_disk["names"] == PDD_NAMES and on_disk["tickers"] == ["PDD"]

    edgar_resource._COMPANY_NAMES_CACHE.clear()
    fresh = _Session({"name": "should not be fetched"})
    with patch.object(edgar_resource.EdgarResource, "_session", return_value=fresh):
        assert edgar.get_company_names("1737806") == PDD_NAMES
    assert fresh.urls == []
    assert edgar_resource.cached_company_names_for_ticker("pdd") == PDD_NAMES
    assert edgar_resource.cached_company_names_for_ticker("UBER") == []


def test_parent_name_hints_for_resolves_once_and_never_needs_network():
    class Fake:
        calls = 0

        def get_cik(self, ticker):
            return "0001737806"

        def get_company_names(self, cik):
            Fake.calls += 1
            assert cik == "0001737806"
            return list(PDD_NAMES)

    sec_13g.clear_parent_name_hints()
    assert parent_name_hints_for("PDD") == []
    assert parent_name_hints_for("PDD", edgar=Fake()) == PDD_NAMES
    assert parent_name_hints_for("pdd", edgar=Fake()) == PDD_NAMES
    assert Fake.calls == 1
    assert parent_name_hints_for("PDD") == PDD_NAMES
    sec_13g.clear_parent_name_hints()


def test_parent_name_hints_for_logs_and_returns_empty_on_edgar_error(caplog):
    class Broken:
        def get_cik(self, ticker):
            return "0000000001"

        def get_company_names(self, cik):
            raise RuntimeError("edgar down")

    sec_13g.clear_parent_name_hints()
    with caplog.at_level("WARNING"):
        assert parent_name_hints_for("XYZ", edgar=Broken()) == []
    assert "edgar down" in caplog.text and "XYZ" in caplog.text
    sec_13g.clear_parent_name_hints()


def test_validate_self_issuer_rows_fire_and_stay_silent():
    from hidden_stock.quirks.holdings.validate import (
        assert_no_self_issuer_rows,
        drop_self_issuer_rows,
        self_issuer_row_reason,
    )

    pdd_self = {"period_end": "2018-09-30", "investee_ticker": "PDD", "investee_name": "Pinduoduo Inc."}
    pdd_former = {"period_end": "2019-03-31", "investee_ticker": None, "investee_name": "Pinduoduo Inc."}
    real = {"period_end": "2024-12-31", "investee_ticker": "GRAB", "investee_name": "Grab Holdings Ltd"}
    assert self_issuer_row_reason(pdd_self, "PDD") == "investee_ticker=PDD is the parent"
    assert self_issuer_row_reason(pdd_former, "PDD") is None
    assert self_issuer_row_reason(pdd_former, "PDD", PDD_NAMES) == "investee_name='Pinduoduo Inc.' is a parent name"
    assert self_issuer_row_reason(real, "UBER", UBER_NAMES) is None
    kept, dropped = drop_self_issuer_rows([pdd_self, pdd_former, real], "PDD", parent_name_hints=PDD_NAMES)
    assert kept == [real] and dropped == [pdd_self, pdd_former]
    assert assert_no_self_issuer_rows([real], "PDD", parent_name_hints=PDD_NAMES) is None
    with pytest.raises(AssertionError, match=r"self_issuer rows for PDD \(hist\).*2018-09-30.*2019-03-31"):
        assert_no_self_issuer_rows([pdd_self, pdd_former, real], "PDD", parent_name_hints=PDD_NAMES, context="hist")


def test_known_parent_name_hints_falls_back_to_disk_cache(tmp_path, monkeypatch):
    from hidden_stock.quirks.holdings.sec_13g import known_parent_name_hints

    monkeypatch.setattr(edgar_resource, "COMPANY_NAMES_CACHE_DIR", tmp_path)
    edgar_resource._COMPANY_NAMES_CACHE.clear()
    sec_13g.clear_parent_name_hints()
    assert known_parent_name_hints("PDD") == []
    (tmp_path / "CIK0001737806.json").write_text(
        json.dumps({"cik": "0001737806", "names": PDD_NAMES, "tickers": ["PDD"]}), encoding="utf-8"
    )
    assert known_parent_name_hints("PDD") == PDD_NAMES
    assert known_parent_name_hints("UBER") == []
    edgar_resource._COMPANY_NAMES_CACHE.clear()
    sec_13g.clear_parent_name_hints()
