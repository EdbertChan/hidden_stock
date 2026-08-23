"""Tencent/13G continuity: no mixed-unit deltas; exit keeps prior accession."""

from __future__ import annotations

from hidden_stock.quirks.holdings.history import diff_snapshots
from hidden_stock.quirks.holdings.sec_13g import clean_issuer_name, resolve_issuer_ticker


def test_clean_issuer_name_strips_boilerplate():
    assert clean_issuer_name("Under the Securities Exchange Act of 1934 MOGU Inc.") == "MOGU Inc."
    assert "Amer Sports" in (
        clean_issuer_name(
            "S AND EXCHANGE COMMISSION Washington, D.C. 20549 SCHEDULE 13D "
            "UNDER THE SECURITIES EXCHANGE ACT OF 1934 Amer Sports, Inc."
        )
        or ""
    )


def test_resolve_tencent_blank_names():
    assert resolve_issuer_ticker("Tuya Inc.", None) == "TUYA"
    assert resolve_issuer_ticker("JD.com, Inc.", None, cusip="47215P106") == "JD"
    assert resolve_issuer_ticker("Nu Holdings Ltd.", None, cusip="G6683N103") == "NU"


def test_mixed_unit_no_fabricated_share_sell():
    """%-only amendment after a share count must not invent shares_delta sell."""
    periods = [
        (
            "2025-05-22",
            "2025-05-22",
            "acc-shares",
            [
                {
                    "investee_name": "Tencent Music Entertainment Group",
                    "investee_ticker": "TME",
                    "shares_held": 1_659_038_412.0,
                    "ownership_pct": 50.0,
                    "as_of_accession_no": "acc-shares",
                    "as_of_date": "2025-05-22",
                    "note": "source=13g form=SC 13G/A",
                }
            ],
        ),
        (
            "2026-08-14",
            "2026-08-14",
            "acc-pct-only",
            [
                {
                    "investee_name": "Tencent Music Entertainment Group",
                    "investee_ticker": "TME",
                    "shares_held": None,
                    "ownership_pct": 50.6,
                    "as_of_accession_no": "acc-pct-only",
                    "as_of_date": "2026-08-14",
                    "note": "source=13g form=SC 13G/A; qoq_continuity=ownership_pct",
                }
            ],
        ),
    ]
    hist = diff_snapshots("TCEHY", periods)
    row = next(r for r in hist if r["period_end"] == "2026-08-14")
    assert row["action"] == "buy"  # % rose 50.0 → 50.6
    assert row["shares_delta"] is None
    assert row.get("shares_held") in (None, "")


def test_13g_exit_keeps_prior_accession_not_sibling_filing():
    """Same-day multi-issuer 13G bucket must not assign ZKH acc to DIDIY exit."""
    periods = [
        (
            "2022-02-10",
            "2022-02-10",
            "acc-didi",
            [
                {
                    "investee_name": "DiDi Global Inc.",
                    "investee_ticker": "DIDIY",
                    "shares_held": 78_986_858.0,
                    "ownership_pct": 4.0,
                    "as_of_accession_no": "acc-didi",
                    "as_of_date": "2022-02-10",
                    "note": "source=13g form=SC 13G/A",
                }
            ],
        ),
        (
            "2024-02-01",
            "2024-02-01",
            "acc-zkh-new",
            [
                {
                    "investee_name": "ZKH Group Limited",
                    "investee_ticker": "ZKH",
                    "shares_held": 526_845_143.0,
                    "ownership_pct": 11.8,
                    "as_of_accession_no": "acc-zkh-new",
                    "as_of_date": "2024-02-01",
                    "note": "source=13g form=SC 13G",
                }
            ],
        ),
    ]
    hist = diff_snapshots("TCEHY", periods)
    didi_exit = next(
        r for r in hist if r.get("investee_ticker") == "DIDIY" and r["action"] == "exit"
    )
    assert didi_exit["accession_no"] == "acc-didi"
    assert didi_exit["filing_date"] == "2022-02-10"  # matches prior accession as_of
    assert "exit_inferred=missing_from_period" in str(didi_exit.get("note") or "")
    assert didi_exit["ownership_pct"] == 0.0


def test_13g_explicit_exit_uses_exit_filing_accession():
    """SOGO-class: zeroing SC 13D/A must stamp that accession + matching as_of."""
    periods = [
        (
            "2021-07-21",
            "2021-07-21",
            "acc-sogo-hold",
            [
                {
                    "investee_name": "Sogou Inc.",
                    "investee_ticker": "SOGO",
                    "shares_held": 151_557_875.0,
                    "ownership_pct": 58.2,
                    "as_of_accession_no": "acc-sogo-hold",
                    "as_of_date": "2021-07-21",
                    "note": "source=13g form=SC 13D/A",
                }
            ],
        ),
        (
            "2021-09-27",
            "2021-09-27",
            "acc-other-same-day",
            [],  # popped from running after exit filing
        ),
    ]
    hist = diff_snapshots(
        "TCEHY",
        periods,
        exit_events={
            "SOGO": {
                "accession": "acc-sogo-exit",
                "filing_date": "2021-09-27",
            }
        },
    )
    sogo_exit = next(
        r for r in hist if r.get("investee_ticker") == "SOGO" and r["action"] == "exit"
    )
    assert sogo_exit["accession_no"] == "acc-sogo-exit"
    assert sogo_exit["filing_date"] == "2021-09-27"
    assert "13g_exit=1" in str(sogo_exit.get("note") or "")
    assert "exit_inferred=missing_from_period" not in str(sogo_exit.get("note") or "")


def test_resolve_futu_warner():
    assert resolve_issuer_ticker("Futu Holdings Limited", None) == "FUTU"
    assert resolve_issuer_ticker("Warner Music Group Corp.", None) == "WMG"


def test_mna_cutoff_stamps_period_grid_exit():
    from hidden_stock.quirks.holdings.tencent import apply_mna_exit_cutoffs

    ordered = [
        (
            "2021-04-20",
            "2021-04-20",
            "acc-pre",
            [
                {
                    "investee_name": "Glu Mobile Inc.",
                    "investee_ticker": "GLUU",
                    "shares_held": 21_000_000.0,
                    "ownership_pct": 12.2,
                    "as_of_accession_no": "acc-gluu-hold",
                    "as_of_date": "2021-02-10",
                    "note": "source=13g form=SC 13D/A",
                }
            ],
        ),
        (
            "2021-05-17",
            "2021-05-17",
            "acc-period-grid",
            [
                {
                    "investee_name": "Glu Mobile Inc.",
                    "investee_ticker": "GLUU",
                    "shares_held": 21_000_000.0,
                    "ownership_pct": 12.2,
                    "as_of_accession_no": "acc-gluu-hold",
                    "as_of_date": "2021-02-10",
                    "note": "source=13g form=SC 13D/A",
                },
                {
                    "investee_name": "Waterdrop Inc.",
                    "investee_ticker": "WDH",
                    "shares_held": 1.0,
                    "ownership_pct": 1.0,
                    "as_of_accession_no": "acc-period-grid",
                    "as_of_date": "2021-05-17",
                    "note": "source=13g form=SC 13G",
                },
            ],
        ),
    ]
    trimmed, events = apply_mna_exit_cutoffs(ordered)
    assert all(r["investee_ticker"] != "GLUU" for r in trimmed[1][3])
    assert events["GLUU"]["accession"] == "acc-period-grid"
    assert events["GLUU"]["filing_date"] == "2021-05-17"
    hist = diff_snapshots("TCEHY", trimmed, exit_events=events)
    gluu_exit = next(r for r in hist if r.get("investee_ticker") == "GLUU" and r["action"] == "exit")
    assert gluu_exit["accession_no"] == "acc-period-grid"
    assert gluu_exit["filing_date"] == "2021-05-17"
    assert gluu_exit.get("market_value_usd") is None
    assert "exit_reason=mna_acquired" in str(gluu_exit.get("note") or "")
