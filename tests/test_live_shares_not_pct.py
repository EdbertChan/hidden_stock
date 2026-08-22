"""Live shares_held must not be ownership_% (Neutron-class)."""

import pytest

from hidden_stock.quirks.holdings.validate import (
    assert_live_shares_held_sane,
    looks_like_ownership_pct_as_shares,
    scrub_live_pct_as_shares,
)


def test_neutron_pct_detected_and_scrubbed():
    assert looks_like_ownership_pct_as_shares(22.87, 22.87)
    assert not looks_like_ownership_pct_as_shares(143_911_749, 11.8)

    rows = scrub_live_pct_as_shares(
        [
            {
                "investee_name": "Neutron Holdings, Inc.",
                "ownership_pct": 22.87,
                "shares_held": 22.87,
                "note": "source=13g",
            },
            {
                "investee_name": "DiDi",
                "ownership_pct": 11.8,
                "shares_held": 143_911_749,
                "note": "source=13g",
            },
        ]
    )
    by = {r["investee_name"]: r for r in rows}
    assert by["Neutron Holdings, Inc."]["shares_held"] is None
    assert "scrubbed_shares_eq_ownership_pct" in by["Neutron Holdings, Inc."]["note"]
    assert by["DiDi"]["shares_held"] == 143_911_749
    assert_live_shares_held_sane(rows)


def test_assert_fails_before_scrub():
    with pytest.raises(ValueError, match="Neutron-class"):
        assert_live_shares_held_sane(
            [{"investee_name": "Neutron", "ownership_pct": 22.87, "shares_held": 22.87}]
        )
