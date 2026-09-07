"""CI grep: numeric regexes under hidden_stock/ must be digit-anchored.

"[\\d,]+" accepted a bare comma ("Didi, $501 million" became a $501M FV row,
parse_notes.py@400c6ed:61-62). Any "[\\d,]" quantified with +, * or {n,} must
be preceded by "\\d" (i.e. "\\d[\\d,]*"). Comment lines are skipped.
"""

from __future__ import annotations

import re
from pathlib import Path

PKG = Path(__file__).resolve().parents[1] / "hidden_stock"

# "[\d,]" followed by a quantifier, not preceded by a literal "\d".
UNANCHORED = re.compile(r"(?<!\\d)\[\\d,\](?:\+|\*|\{)")


def unanchored_numeric_regex_hits(root: Path = PKG) -> list[str]:
    hits: list[str] = []
    for path in sorted(root.rglob("*.py")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.strip().startswith("#"):
                continue
            if UNANCHORED.search(line):
                hits.append(f"{path.relative_to(root.parent)}:{lineno}: {line.strip()}")
    return hits


def test_no_unanchored_numeric_regex_under_hidden_stock():
    hits = unanchored_numeric_regex_hits()
    assert not hits, "digit-anchor these (\\d[\\d,]*):\n" + "\n".join(hits)


def test_lint_pattern_catches_and_allows_expected_shapes():
    assert UNANCHORED.search(r"(?P<sh>[\d,]+)")
    assert UNANCHORED.search(r"([\d,]{4,})")
    assert UNANCHORED.search(r"x[\d,]*")
    assert not UNANCHORED.search(r"\d[\d,]*(?:\.\d+)?(?![\d,])")
    assert not UNANCHORED.search(r"\d[\d,]{3,}")
