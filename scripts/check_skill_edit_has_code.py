#!/usr/bin/env python3
"""Gate: a rule/principle edit under ``.cursor/`` must ship with code or a test.

Why: principles were repeatedly written into ``.cursor/skills/*/SKILL.md`` and
``.cursor/rules/*.mdc`` with nothing enforcing them (5ab00d0 added the
"same commit" rule in a doc-only commit). Rules said "never invent" while
nothing checked "never drop". This script makes that drift mechanical.

Usage::

    scripts/check_skill_edit_has_code.py [RANGE]      (default HEAD~1..HEAD)
    scripts/check_skill_edit_has_code.py --staged     (pre-commit hook mode)
    scripts/check_skill_edit_has_code.py --allow-doc-only RANGE

Exit 0 when:
  * no rule-doc path changed, or
  * changed rule-doc lines are prose only. A line counts as a rule when it
    is a bullet or numbered item containing must / never / always / assert /
    refuse / do not, when it contains an invariant arrow (⇒ or =>) anywhere,
    or when it opens with one of those directive words (after markdown
    emphasis). A typo fix in plain prose does not trip it, or
  * at least one ``hidden_stock/**/*.py``, ``scripts/**/*.py`` or
    ``tests/**/*.py`` file changed in the same range, or
  * the bypass is explicit: ``--allow-doc-only``, env
    ``SKILL_EDIT_ALLOW_DOC_ONLY=1``, or a ``[doc-only]`` tag in a commit
    message inside RANGE. Every bypass is logged to stderr.

Exit 1 otherwise, naming the rule lines.

Stdlib only so the git hook can run it with a bare ``python3``.
``fnmatch`` ``*`` spans ``/``, so ``scripts/*.py`` also covers nested dirs.
A root commit has no ``HEAD~1``; the range is then diffed against the
empty tree. ``A...B`` diffs from the merge-base and the ``[doc-only]`` scan
covers merge-base..B only, so a tag on the base branch cannot bypass.
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import re
import subprocess
import sys

EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

RULE_DOC_GLOBS = (".cursor/skills/*/SKILL.md", ".cursor/rules/*.mdc")
CODE_GLOBS = ("hidden_stock/*.py", "scripts/*.py", "tests/*.py")

_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_DIRECTIVE_WORDS = r"(?:must|never|always|assert|refuse|do not|don'?t)"
_DIRECTIVE_RE = re.compile(r"(?i)\b" + _DIRECTIVE_WORDS + r"\b")
_ARROW_RE = re.compile(r"⇒|=>")
_OPENS_WITH_DIRECTIVE_RE = re.compile(r"(?i)^\s*[*_`>#\s]*" + _DIRECTIVE_WORDS + r"\b")
DOC_ONLY_TAG = "[doc-only]"


def _git(args: list[str], cwd: str | None) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def _matches(path: str, globs: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatch(path, g) for g in globs)


def is_rule_line(line: str) -> bool:
    """True when ``line`` (without the diff '+') looks like a rule/principle."""
    if _ARROW_RE.search(line):
        return True
    if _OPENS_WITH_DIRECTIVE_RE.match(line):
        return True
    return bool(_BULLET_RE.match(line)) and bool(_DIRECTIVE_RE.search(line))


def _resolve_range(range_spec: str, cwd: str | None) -> tuple[list[str], str]:
    """Return (diff args, log range) for a git range spec."""
    if "..." in range_spec:
        a, b = range_spec.split("...", 1)
        mb = _git(["merge-base", a or "HEAD", b or "HEAD"], cwd).strip()
        return [range_spec], f"{mb}..{b or 'HEAD'}"
    if ".." in range_spec:
        a, b = range_spec.split("..", 1)
        a = a or "HEAD"
        b = b or "HEAD"
        probe = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"{a}^{{commit}}"],
            cwd=cwd, capture_output=True, text=True, check=False,
        )
        if probe.returncode != 0:
            return [EMPTY_TREE, b], b
        return [f"{a}..{b}"], f"{a}..{b}"
    return [range_spec], range_spec


def changed_files(diff_args: list[str], cwd: str | None) -> list[str]:
    out = _git(["diff", "--name-only", *diff_args], cwd)
    return [p for p in out.splitlines() if p.strip()]


def added_rule_lines(diff_args: list[str], path: str, cwd: str | None) -> list[tuple[int, str]]:
    """Added/changed lines in ``path`` that look like rules, with new-file line numbers."""
    out = _git(["diff", "-U0", "--no-color", *diff_args, "--", path], cwd)
    hits: list[tuple[int, str]] = []
    new_line = 0
    for raw in out.splitlines():
        if raw.startswith("@@"):
            m = re.search(r"\+(\d+)", raw)
            new_line = int(m.group(1)) if m else 0
            continue
        if raw.startswith("+++") or raw.startswith("---"):
            continue
        if raw.startswith("+"):
            text = raw[1:]
            if is_rule_line(text):
                hits.append((new_line, text.rstrip()))
            new_line += 1
    return hits


def commit_messages_have_tag(log_range: str, cwd: str | None) -> bool:
    try:
        body = _git(["log", "--format=%B", log_range], cwd)
    except RuntimeError:
        return False
    return DOC_ONLY_TAG in body


def run(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    ap.add_argument("range", nargs="?", default="HEAD~1..HEAD",
                    help="git range to inspect (default HEAD~1..HEAD)")
    ap.add_argument("--staged", action="store_true",
                    help="inspect the index instead of a commit range (pre-commit)")
    ap.add_argument("--allow-doc-only", action="store_true",
                    help="explicit bypass; logged")
    ap.add_argument("-C", "--repo", default=None, help="repo directory (default cwd)")
    ns = ap.parse_args(argv)
    cwd = ns.repo

    if ns.staged:
        diff_args = ["--cached"]
        log_range = None
    else:
        diff_args, log_range = _resolve_range(ns.range, cwd)

    files = changed_files(diff_args, cwd)
    rule_docs = [f for f in files if _matches(f, RULE_DOC_GLOBS)]
    if not rule_docs:
        return 0

    findings: dict[str, list[tuple[int, str]]] = {}
    for path in rule_docs:
        hits = added_rule_lines(diff_args, path, cwd)
        if hits:
            findings[path] = hits
    if not findings:
        return 0

    code_files = [f for f in files if _matches(f, CODE_GLOBS)]
    if code_files:
        return 0

    bypass = None
    if ns.allow_doc_only:
        bypass = "--allow-doc-only flag"
    elif os.environ.get("SKILL_EDIT_ALLOW_DOC_ONLY") == "1":
        bypass = "SKILL_EDIT_ALLOW_DOC_ONLY=1"
    elif log_range and commit_messages_have_tag(log_range, cwd):
        bypass = f"{DOC_ONLY_TAG} tag in commit message"

    n_rules = sum(len(v) for v in findings.values())
    if bypass:
        print(
            f"check_skill_edit_has_code: BYPASS via {bypass}: {n_rules} rule line(s) in "
            f"{', '.join(findings)} shipped without a code/test hunk.",
            file=sys.stderr,
        )
        return 0

    print("check_skill_edit_has_code: FAIL: rule/principle lines changed with no code or test hunk.",
          file=sys.stderr)
    for path, hits in findings.items():
        for lineno, text in hits:
            print(f"  {path}:{lineno}: {text.strip()}", file=sys.stderr)
    print(
        "\nA rule that nothing enforces is drift. In the same commit/PR, add a test "
        "(tests/**/*.py) or code (hidden_stock/**/*.py, scripts/**/*.py) that "
        "checks it.\nExplicit bypass (logged): --allow-doc-only, "
        "SKILL_EDIT_ALLOW_DOC_ONLY=1, or a [doc-only] tag in the commit message.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(run())
