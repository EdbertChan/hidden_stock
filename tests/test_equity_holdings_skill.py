"""Structural + incident backtests for equity-holdings-sheets skill.

The skill is prose; these tests lock the valuation rules that failed when
DIDIY was marked via share×price (~$482M) instead of Uber 10-Q Investments
FV ($1,900M). Behavioral enforcement lives in test_mtm_no_invent.py and
test_holdings_ci_regressions.py — this file guards the skill text itself.
"""

from __future__ import annotations

import re
from pathlib import Path

SKILL_PATH = (
    Path(__file__).resolve().parents[1]
    / ".cursor"
    / "skills"
    / "equity-holdings-sheets"
    / "SKILL.md"
)


def _skill() -> str:
    return SKILL_PATH.read_text(encoding="utf-8")


def test_skill_file_exists():
    assert SKILL_PATH.is_file()


def test_skill_valuation_order_encoded():
    text = _skill()
    assert "## Valuation" in text or "Valuation (do not invent" in text
    # Order: 13F → Investments table → null; 13G never primary $
    assert re.search(r"(?i)13F.*market_value|Form \*\*13F\*\*", text)
    assert re.search(r"(?i)Investments table", text)
    assert re.search(r"(?i)null", text)
    assert re.search(r"(?i)13D/G.*never.*(?:primary|dollar)", text)


def test_skill_bans_otc_eodhd_invent_with_didi_incident():
    text = _skill()
    assert re.search(r"(?i)never.*invent.*(?:OTC|EODHD|shares)", text)
    assert re.search(r"1,?900", text)
    assert re.search(r"(?i)482", text)  # wrong mark must stay as cautionary


def test_skill_how_you_check_covers_sheet_and_one_aur():
    text = _skill()
    assert re.search(r"(?i)How you check", text)
    assert re.search(r"(?i)portfolio_by_period|chart_data", text)
    assert re.search(r"(?i)one row per ticker|one AUR|one row/ticker", text)
    assert "pytest" in text.lower()


def test_skill_null_is_ok_until_filing_fv():
    text = _skill()
    assert re.search(r"(?i)null.*(?:OK|ok|omit)|leave.*null", text)
