"""Shared test helpers (tests/ is on sys.path under pytest's default import mode)."""

from __future__ import annotations

from hidden_stock.resources.edgar_resource import EdgarResource

# history.collect_note_snapshots hands parsers ``edgar.get_filing_text(html,
# max_chars=1_200_000)`` — flattened text, tables gone. Fixtures written as HTML
# only prove the HTML branch; run every parser test through this too.
PRODUCTION_MAX_CHARS = 1_200_000

INPUT_SHAPES = ("html", "production_text")


def as_production_text(html: str, *, max_chars: int = PRODUCTION_MAX_CHARS) -> str:
    """Apply exactly the transformation production applies before parsing."""
    return EdgarResource(user_agent="tests").get_filing_text(html, max_chars=max_chars)


def shaped(html: str, shape: str) -> str:
    """Return the fixture in the requested input shape ("html" or "production_text")."""
    if shape == "html":
        return html
    if shape == "production_text":
        return as_production_text(html)
    raise ValueError(f"unknown input shape {shape!r}; expected one of {INPUT_SHAPES}")
