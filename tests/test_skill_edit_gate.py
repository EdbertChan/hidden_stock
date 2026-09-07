"""scripts/check_skill_edit_has_code.py against throwaway git repos.

The gate exists because rules kept landing in .cursor/skills/*/SKILL.md with
nothing enforcing them (5ab00d0). Each test builds a tiny repo in tmp_path,
commits a change, and asserts the exit code the gate returns for it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_skill_edit_has_code.py"
SKILL_REL = Path(".cursor/skills/demo-skill/SKILL.md")
RULE_REL = Path(".cursor/rules/demo.mdc")

BASE_SKILL = "\n".join([
    "# Demo skill",
    "",
    "Some intro prose that explains teh purpose.",
    "",
    "## Principles",
    "",
    "1. **Never invent** - no fake shares.",
    "",
])


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout


def _write(repo: Path, rel: Path | str, text: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _commit(repo: Path, msg: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)


def _gate(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=repo, capture_output=True, text=True, check=False,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "t")
    _git(r, "config", "commit.gpgsign", "false")
    _write(r, SKILL_REL, BASE_SKILL)
    _write(r, "hidden_stock/__init__.py", "")
    _write(r, "tests/test_x.py", "def test_x():\n    assert True\n")
    _commit(r, "base")
    return r


def test_doc_only_rule_edit_fails(repo: Path):
    _write(repo, SKILL_REL, BASE_SKILL + "2. **Never drop** - a sale must always appear.\n")
    _commit(repo, "principle: never drop")
    res = _gate(repo)
    assert res.returncode == 1, res.stderr
    assert "SKILL.md:8" in res.stderr
    assert "Never drop" in res.stderr
    assert "[doc-only]" in res.stderr


def test_rule_edit_with_test_file_passes(repo: Path):
    _write(repo, SKILL_REL, BASE_SKILL + "2. **Never drop** - a sale must always appear.\n")
    _write(repo, "tests/test_never_drop.py", "def test_never_drop():\n    assert True\n")
    _commit(repo, "principle + test")
    res = _gate(repo)
    assert res.returncode == 0, res.stderr


def test_rule_edit_with_code_file_passes(repo: Path):
    _write(repo, SKILL_REL, BASE_SKILL + "- Refuse to ship unseen tickers.\n")
    _write(repo, "hidden_stock/gate.py", "def refuse():\n    return True\n")
    _commit(repo, "principle + code")
    res = _gate(repo)
    assert res.returncode == 0, res.stderr


def test_prose_typo_fix_passes(repo: Path):
    _write(repo, SKILL_REL, BASE_SKILL.replace("teh purpose", "the purpose"))
    _commit(repo, "docs: typo")
    res = _gate(repo)
    assert res.returncode == 0, res.stderr


def test_doc_only_tag_bypasses_and_logs(repo: Path):
    _write(repo, SKILL_REL, BASE_SKILL + "2. **Never drop** - a sale must always appear.\n")
    _commit(repo, "principle: never drop [doc-only]")
    res = _gate(repo)
    assert res.returncode == 0, res.stderr
    assert "BYPASS" in res.stderr
    assert "[doc-only]" in res.stderr


def test_allow_doc_only_flag_bypasses_and_logs(repo: Path):
    _write(repo, SKILL_REL, BASE_SKILL + "2. **Never drop** - a sale must always appear.\n")
    _commit(repo, "principle: never drop")
    res = _gate(repo, "--allow-doc-only")
    assert res.returncode == 0, res.stderr
    assert "BYPASS via --allow-doc-only" in res.stderr


def test_rules_mdc_arrow_line_fails(repo: Path):
    _write(repo, RULE_REL, "---\nalwaysApply: true\n---\n13F exit ⇒ market_value_usd = 0.0\n")
    _commit(repo, "rule: arrow")
    res = _gate(repo)
    assert res.returncode == 1, res.stderr
    assert "demo.mdc:4" in res.stderr


def test_staged_mode_pre_commit(repo: Path):
    _write(repo, SKILL_REL, BASE_SKILL + "- Always cite the filing.\n")
    _git(repo, "add", "-A")
    res = _gate(repo, "--staged")
    assert res.returncode == 1, res.stderr
    _write(repo, "tests/test_cite.py", "def test_cite():\n    assert True\n")
    _git(repo, "add", "-A")
    res = _gate(repo, "--staged")
    assert res.returncode == 0, res.stderr


def test_non_rule_doc_change_ignored(repo: Path):
    _write(repo, "README.md", "- You must never do X.\n")
    _commit(repo, "readme")
    res = _gate(repo)
    assert res.returncode == 0, res.stderr


def test_merge_base_range_ignores_tag_on_base(repo: Path):
    _git(repo, "checkout", "-q", "-b", "feature")
    _write(repo, SKILL_REL, BASE_SKILL + "- Never guess.\n")
    _commit(repo, "principle: never guess")
    _git(repo, "checkout", "-q", "main")
    _write(repo, "README.md", "x\n")
    _commit(repo, "base-side change [doc-only]")
    res = _gate(repo, "main...feature")
    assert res.returncode == 1, res.stderr


def test_root_commit_range_uses_empty_tree(repo: Path):
    res = _gate(repo, "HEAD~1..HEAD")
    assert res.returncode == 0, res.stderr
    _git(repo, "rm", "-q", "-r", "hidden_stock", "tests")
    _git(repo, "commit", "-q", "--amend", "--no-edit")
    res = _gate(repo, "HEAD~1..HEAD")
    assert res.returncode == 1, res.stderr
    assert "Never invent" in res.stderr


def test_gate_fails_on_real_offender_5ab00d0():
    repo = SCRIPT.parents[1]
    probe = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", "5ab00d0^{commit}"],
        cwd=repo, capture_output=True, text=True, check=False,
    )
    if probe.returncode != 0:
        pytest.skip("5ab00d0 not present in this checkout")
    res = _gate(repo, "5ab00d0~1..5ab00d0")
    assert res.returncode == 1, res.stderr
    assert "principle-assert-invariants-not-last-bug/SKILL.md" in res.stderr


import pytest as _pytest


@_pytest.mark.parametrize(
    "rel",
    [
        ".claude/skills/holdings/SKILL.md",
        "AGENTS.md",
        "CLAUDE.md",
        ".codex/rules/holdings.md",
        "skills/holdings/SKILL.md",
    ],
)
def test_gate_is_harness_agnostic(repo: Path, rel: str):
    """Claude Code, Codex, and bare SKILL.md surfaces are gated like .cursor/."""
    _write(repo, rel, "# Rules\n\n1. **Never drop** - a sale must always appear.\n")
    _commit(repo, f"principle in {rel}")
    res = _gate(repo)
    assert res.returncode == 1, res.stdout + res.stderr
    assert rel in res.stdout + res.stderr
