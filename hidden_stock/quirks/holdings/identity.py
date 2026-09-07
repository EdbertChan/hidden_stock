"""Shared investee identity helpers (not a form parser).

CUSIP maps, issuer-name cleanup, ticker resolution, and QoQ holding keys.
Form parsers (13F / 13G / notes / HK annual) import from here — never the reverse.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

# Seed CUSIP → ticker for known names (never invent at runtime).
_DEFAULT_CUSIP_TICKERS = {
    "98422D105": "XPEV",  # XPeng ADS
    "948596101": "WB",  # Weibo ADR
}

# Shared issuer name → ticker hints (Tencent + common US strategic stakes).
ISSUER_TICKER_HINTS: dict[str, str | None] = {
    "kanzhun": "BZ",
    "ke holdings": "BEKE",
    "reddit": "RDDT",
    "cango": "CANG",
    "horizon quantum": "HQ",
    "horizon quantum holdings": "HQ",
    "pinduoduo": "PDD",
    "pdd holdings": "PDD",
    "sea limited": "SE",
    "spotify": "SPOT",
    "meituan": "3690.HK",
    "vipshop": "VIPS",
    "nio": "NIO",
    "bilibili": "BILI",
    "tencent music": "TME",
    "huya": "HUYA",
    "cheetah": "CMCM",
    "global blue": "GB",
    "cheche": "CCG",
    "bitauto": "BITA",
    "58.com": "WUBA",
    "58 com": "WUBA",
    "didi": "DIDIY",
    "didi global": "DIDIY",
    "aurora innovation": "AUR",
    "grab holdings": "GRAB",
    "lucid group": "LCID",
    "weride": "WRD",
    "serve robotics": "SERV",
    "neutron holdings": None,  # Lime — private
    "xpeng": "XPEV",
    "perfect corp": "PERF",
    "baozun": "BZUN",
    "momo": "MOMO",
    "hello group": "MOMO",
    "groupon": "GRPN",
    "smart share": "EM",
    "mariadb": "MRDB",
    "best inc": "BEST",
    "joby aviation": "JOBY",
    "marqeta": "MQ",
    "rivian": "RIVN",
    "jd.com": "JD",
    "jd com": "JD",
    "tuya": "TUYA",
    "waterdrop": "WDH",
    "douyu": "DOYU",
    "lilium": "LILM",
    "zhihu": "ZH",
    "zenvia": "ZENV",
    "missfresh": "MF",
    "farfetch": "FTCH",
    "qutoutiao": "QTT",
    "th international": "THCH",
    "mogu": "MOGU",
    "zkh": "ZKH",
    "yxt.com": "YXT",
    "yxt.com group": "YXT",
    "nu holdings": "NU",
    "sogou": "SOGO",
    "glu mobile": "GLUU",
    "amer sports": "AS",
    "futu": "FUTU",
    "futu holdings": "FUTU",
    "warner music": "WMG",
    "warner music group": "WMG",
}

_BOILERPLATE_PREFIX = re.compile(
    r"(?is)^(?:.*?(?:securities exchange act of 1934|schedule 13[dg](?:/a)?|"
    r"s and exchange commission(?:.*?20549)?)\s*)+"
)


def assert_pct_domain(value, *, field: str, context: str) -> float | None:
    """Ownership % must be in (0, 100]; None passes through (not disclosed).

    Shared by every parser that emits ``ownership_pct`` (13G, broker SOTP,
    notes). A peer-comp table once shipped a 612% "stake" (83a0535) and a
    narrative comma-number a 1,952% Dietz period (ecdb97b): domain checks
    live here, not per parser. Raises ValueError with field/context so the
    caller can skip and log the single bad row instead of crashing the run.
    """
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{context}: {field}={value!r} is not numeric") from None
    if v != v or not (0.0 < v <= 100.0):
        raise ValueError(f"{context}: {field}={v!r} outside (0, 100]")
    return v


def load_cusip_tickers(path: Path | None = None) -> dict[str, str]:
    out = dict(_DEFAULT_CUSIP_TICKERS)
    if path is None:
        path = Path(__file__).resolve().parent / "cusip_tickers.yaml"
    if path.exists():
        data = yaml.safe_load(path.read_text()) or {}
        for cusip, ticker in (data.get("cusips") or {}).items():
            if cusip and ticker:
                out[str(cusip).strip().upper()] = str(ticker).strip().upper()
    return out


_CORP_SUFFIX_WORDS = frozenset(
    {
        "inc",
        "incorporated",
        "ltd",
        "limited",
        "holdings",
        "holding",
        "group",
        "co",
        "corp",
        "corporation",
        "company",
        "plc",
        "llc",
        "lp",
        "sa",
        "nv",
        "ag",
        "se",
    }
)

_GENERIC_TAIL_WORDS = frozenset(
    {
        "technologies",
        "technology",
        "tech",
        "global",
        "international",
        "worldwide",
        "enterprises",
        "industries",
        "systems",
        "networks",
        "platforms",
        "labs",
        "com",
    }
)


def company_name_stem(name: str | None) -> str:
    """Lowercase name with punctuation and trailing corporate suffixes removed.

    ``"PDD Holdings Inc."`` → ``"pdd"``; ``"Uber Technologies, Inc."`` →
    ``"uber technologies"`` (descriptors stay part of the stem, only the legal
    suffix tail is dropped). Empty input → ``""``.
    """
    words = re.sub(r"[^a-z0-9]+", " ", str(name or "").lower()).split()
    if words and words[0] == "the":
        words = words[1:]
    while words and words[-1] in _CORP_SUFFIX_WORDS:
        words.pop()
    return " ".join(words)


def name_matches_parent_hints(name: str | None, hints: list[str] | tuple[str, ...]) -> bool:
    """True when ``name`` is one of the parent's names (current, former, or ticker).

    A hint matches when its stem equals the name's stem, or when both share a
    first word of >= 4 chars and every other word on either side is a generic
    descriptor (``"uber"`` ~ ``"uber technologies"``). A brand followed by a
    real word is a different company: ``"tencent"`` must not claim
    ``"tencent music entertainment"``.
    """
    stem = company_name_stem(clean_issuer_name(name) or name)
    if not stem:
        return False
    words = stem.split()
    for hint in hints or ():
        hint_stem = company_name_stem(hint)
        if not hint_stem:
            continue
        if hint_stem == stem:
            return True
        hint_words = hint_stem.split()
        if (
            len(hint_words[0]) >= 4
            and hint_words[0] == words[0]
            and all(w in _GENERIC_TAIL_WORDS for w in hint_words[1:])
            and all(w in _GENERIC_TAIL_WORDS for w in words[1:])
        ):
            return True
    return False


def clean_issuer_name(name: str | None) -> str | None:
    """Strip SEC cover-page boilerplate glued onto issuer names."""
    if not name:
        return None
    n = str(name).strip()
    cleaned = _BOILERPLATE_PREFIX.sub("", n).strip(" ,;-")
    if cleaned and cleaned.lower() not in {"inc.", "inc", "ltd.", "ltd", "limited"}:
        return cleaned
    return n


def resolve_issuer_ticker(
    name: str | None,
    ticker: str | None,
    *,
    cusip: str | None = None,
) -> str | None:
    if ticker:
        return str(ticker).strip().upper()
    if cusip:
        mapped = load_cusip_tickers().get(str(cusip).strip().upper())
        if mapped:
            return str(mapped).strip().upper()
    cleaned = clean_issuer_name(name)
    if not cleaned:
        return None
    from .extract import load_investee_aliases, resolve_investee_ticker

    resolved = resolve_investee_ticker(cleaned, None, load_investee_aliases())
    if resolved:
        return resolved
    key = cleaned.strip().lower()
    for alias, sym in ISSUER_TICKER_HINTS.items():
        if alias in key:
            if sym is None:
                slug = re.sub(r"[^a-z0-9]+", "_", key).strip("_")
                slug = re.sub(r"_+", "_", slug)[:48].upper()
                return f"PRIV_{slug}" if slug else "PRIV_UNKNOWN"
            return sym
    return None


def holding_key(row: dict) -> str:
    """Identity for QoQ diffs / live merge: ticker first, then CUSIP, then name.

    Ticker-first prevents 13F drop + continuing 13G from emitting
    exit(c:CUSIP)+new(t:TICKER) for the same name.
    """
    from .lookback import normalize_ticker

    t = normalize_ticker(row.get("investee_ticker")) or ""
    if t:
        return f"t:{t}"
    c = (row.get("_cusip") or row.get("cusip") or "").strip().upper()
    if c:
        return f"c:{c}"
    name = re.sub(r"[^a-z0-9]+", "", (row.get("investee_name") or "").lower())
    return f"n:{name}"
