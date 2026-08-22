"""Unit tests for QoQ holdings history diffs (13F + notes)."""

from hidden_stock.quirks.holdings.history import (
    classify_action,
    diff_snapshots,
    note_as_position,
    _merge_period_rows,
    _notes_as_of,
)


def test_classify_action():
    assert classify_action(0, 100) == "new"
    assert classify_action(100, 0) == "exit"
    assert classify_action(100, 150) == "buy"
    assert classify_action(150, 100) == "sell"
    assert classify_action(100, 100) == "hold"


def test_diff_snapshots_exit_and_sell():
    p1 = (
        "2023-12-31",
        "2024-02-14",
        "acc1",
        [
            {
                "investee_name": "BILIBILI INC",
                "investee_ticker": "BILI",
                "shares_held": 10_000_000,
                "market_value_usd": 100.0,
                "ownership_pct": 10.2,
                "_cusip": "090040106",
            },
            {
                "investee_name": "PERFECT CORP",
                "investee_ticker": "PERF",
                "shares_held": 10_887_904,
                "market_value_usd": 50.0,
                "_cusip": "G7006A109",
            },
            {
                "investee_name": "XPENG INC",
                "investee_ticker": "XPEV",
                "shares_held": 6_650_000,
                "market_value_usd": 80.0,
                "_cusip": "98422D105",
            },
        ],
    )
    p2 = (
        "2024-03-31",
        "2024-05-13",
        "acc2",
        [
            {
                "investee_name": "PERFECT CORP",
                "investee_ticker": "PERF",
                "shares_held": 4_419_823,
                "market_value_usd": 20.0,
                "_cusip": "G7006A109",
            },
            {
                "investee_name": "XPENG INC",
                "investee_ticker": "XPEV",
                "shares_held": 6_650_000,
                "market_value_usd": 70.0,
                "_cusip": "98422D105",
            },
        ],
    )
    hist = diff_snapshots("BABA", [p1, p2])
    by = {(r["period_end"], r["investee_ticker"]): r for r in hist}
    assert by[("2024-03-31", "BILI")]["action"] == "exit"
    assert by[("2024-03-31", "BILI")]["shares_delta"] == -10_000_000
    assert by[("2024-03-31", "BILI")]["ownership_pct"] == 0.0
    assert by[("2024-03-31", "PERF")]["action"] == "sell"
    assert by[("2024-03-31", "PERF")]["shares_delta"] == 4_419_823 - 10_887_904
    assert by[("2024-03-31", "XPEV")]["action"] == "hold"
    assert by[("2023-12-31", "BILI")]["action"] == "new"


def test_note_as_position_moonshot_cost_is_not_market_value():
    """Narrative amount invested / equity-method carrying must not chart as FV."""
    pos = note_as_position(
        {
            "investee_name": "Moonshot AI Ltd",
            "ownership_pct": 36.0,
            "carrying_usd": 800_000_000,
            "_source": "20f_note",
            "note": "source=20f_note short=Moonshot",
        }
    )
    # Private note names: PRIV_<slug> identity (not an exchange ticker).
    assert pos["investee_ticker"] == "PRIV_MOONSHOT_AI_LTD"
    assert pos["shares_held"] is None
    assert pos["ownership_pct"] == 36.0
    assert pos["market_value_usd"] is None
    assert "qoq_continuity=ownership_pct" in str(pos["note"])
    assert "ticker=private_note" in str(pos["note"])


def test_note_as_position_investments_table_sets_fv():
    pos = note_as_position(
        {
            "investee_name": "Didi Global",
            "investee_ticker": "DIDIY",
            "fair_value_disclosed_usd": 1_900_000_000,
            "_source": "10q_investments_table",
            "note": "source=10q_investments_table",
        }
    )
    assert pos["market_value_usd"] == 1_900_000_000


def test_notes_forward_fill_into_qoq():
    notes = [
        (
            "2024-06-01",
            "2024-06-01",
            "ann1",
            [
                note_as_position(
                    {
                        "investee_name": "Moonshot AI Ltd",
                        "ownership_pct": 36,
                        "carrying_usd": 8e8,
                        "_source": "20f_note",
                        "note": "source=20f_note",
                    }
                )
            ],
        )
    ]
    assert _notes_as_of(notes, "2024-05-01") == []
    filled = _notes_as_of(notes, "2024-09-01")
    assert len(filled) == 1
    assert filled[0]["investee_ticker"] == "PRIV_MOONSHOT_AI_LTD"
    assert filled[0]["ownership_pct"] == 36

    p1 = (
        "2024-06-30",
        "2024-08-14",
        "13f1",
        [
            {
                "investee_name": "XPENG INC",
                "investee_ticker": "XPEV",
                "shares_held": 1e6,
                "market_value_usd": 10.0,
                "_cusip": "98422D105",
            }
        ],
    )
    merged = _merge_period_rows(p1[3], filled)
    hist = diff_snapshots("BABA", [(p1[0], p1[1], p1[2], merged)])
    tickers = {r["investee_ticker"] for r in hist}
    assert "XPEV" in tickers
    moon = next(r for r in hist if str(r.get("investee_ticker") or "").startswith("PRIV_"))
    assert moon["investee_ticker"] == "PRIV_MOONSHOT_AI_LTD"
    assert moon["shares_held"] is None
    assert moon["ownership_pct"] == 36
    assert moon["market_value_usd"] is None  # cost ≠ Investments-table FV
    assert "20f_note" in moon["note"]
