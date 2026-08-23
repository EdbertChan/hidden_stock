"""Parent ticker normalization (aliases + CIK overrides live in runner)."""

from __future__ import annotations

# Company / nickname → canonical parent ticker used in stock_data tables.
PARENT_ALIASES: dict[str, str] = {
    "tencent": "TCEHY",
    "tencent holdings": "TCEHY",
    "tctzf": "TCEHY",
    "0700": "TCEHY",
    "0700.hk": "TCEHY",
    "alibaba": "BABA",
    "alibaba group": "BABA",
    "berkshire": "BRK-B",
    "berkshire hathaway": "BRK-B",
    "brk.b": "BRK-B",
    "brkb": "BRK-B",
    "uber": "UBER",
    "uber technologies": "UBER",
}

# Parents with a dedicated HK annual + 13G path (no useful US 13F book).
HK_AGGREGATE_PARENTS: frozenset[str] = frozenset({"TCEHY", "TCTZF"})


def normalize_parent(name_or_ticker: str) -> str:
    """Map company name or ticker to canonical parent (e.g. tencent → TCEHY)."""
    raw = (name_or_ticker or "").strip()
    if not raw:
        raise ValueError("empty parent ticker")
    key = raw.lower().replace("_", " ")
    if key in PARENT_ALIASES:
        return PARENT_ALIASES[key]
    key2 = key.replace(" holdings", "").strip()
    if key2 in PARENT_ALIASES:
        return PARENT_ALIASES[key2]
    t = raw.upper().replace(".", "-")
    if t in {"BRK-B", "BRKB"}:
        return "BRK-B"
    if t in HK_AGGREGATE_PARENTS or t == "0700":
        return "TCEHY"
    return t


def uses_hk_aggregates(parent: str) -> bool:
    return normalize_parent(parent) in HK_AGGREGATE_PARENTS


def history_strategy(parent: str) -> str:
    """Legacy label for export/logging: sources always fan out; HK parents skip empty 13F grid."""
    if uses_hk_aggregates(parent):
        return "fanout_13g_hk"
    return "fanout_13f_13g_notes"


def default_lookback_years(parent: str) -> int:
    """Match CLI export: HK 13G/Note22 parents need a longer sparse window."""
    return 8 if history_strategy(parent) == "fanout_13g_hk" else 5
