"""Tests for parents + synthetic 13G QoQ diffs."""

from hidden_stock.quirks.holdings.history import diff_snapshots
from hidden_stock.quirks.holdings.parents import history_strategy, normalize_parent, uses_hk_aggregates
from hidden_stock.quirks.holdings.tencent import _13g_raw_to_position


def test_normalize_parent_aliases():
    assert normalize_parent("tencent") == "TCEHY"
    assert normalize_parent("0700") == "TCEHY"
    assert normalize_parent("alibaba") == "BABA"
    assert normalize_parent("brk.b") == "BRK-B"
    assert normalize_parent("BABA") == "BABA"


def test_history_strategy_fanout_labels():
    assert history_strategy("TCEHY") == "fanout_13g_hk"
    assert history_strategy("tencent") == "fanout_13g_hk"
    assert uses_hk_aggregates("TCEHY")
    assert history_strategy("BABA") == "fanout_13f_13g_notes"
    assert history_strategy("BRK-B") == "fanout_13f_13g_notes"
    assert history_strategy("UBER") == "fanout_13f_13g_notes"
    assert not uses_hk_aggregates("UBER")


def test_13g_raw_to_position_and_diff():
    p1 = _13g_raw_to_position(
        {"issuer_name": "PDD Holdings", "ticker": "PDD", "ownership_pct": 15.0, "cusip": "ABC"},
        form="SC 13G",
        acc="a1",
        filing_date="2023-01-01",
        cik="0001293451",
    )
    p2a = _13g_raw_to_position(
        {"issuer_name": "PDD Holdings", "ticker": "PDD", "ownership_pct": 16.5, "cusip": "ABC"},
        form="SC 13G/A",
        acc="a2",
        filing_date="2024-01-01",
        cik="0001293451",
    )
    p2b = _13g_raw_to_position(
        {"issuer_name": "Sea Limited", "ticker": "SE", "ownership_pct": 5.0, "cusip": "DEF"},
        form="SC 13G",
        acc="a3",
        filing_date="2024-01-01",
        cik="0001293451",
    )
    assert p1 is not None and p2a is not None and p2b is not None
    hist = diff_snapshots(
        "TCEHY",
        [
            ("2023-01-01", "2023-01-01", "a1", [p1]),
            ("2024-01-01", "2024-01-01", "a2", [p2a, p2b]),
        ],
    )
    by = {(r["period_end"], r["investee_ticker"]): r for r in hist}
    assert by[("2023-01-01", "PDD")]["action"] == "new"
    assert by[("2024-01-01", "PDD")]["action"] == "buy"
    assert by[("2024-01-01", "SE")]["action"] == "new"
    assert "13g" in by[("2024-01-01", "PDD")]["note"]
